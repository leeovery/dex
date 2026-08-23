"""Migration 2: the identity re-key + rerun seed for pre-rewrite state."""

import datetime
import json

import pytest

from dex_engine import corpus
from dex_engine.drivers.x import XDriver
from dex_engine.drivers.youtube import YouTubeDriver
from dex_engine.migrations import MigrationError
from dex_engine.migrations.migration_2 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.transcribe import read_enrichment
from dex_engine.pipeline.types import Kind, LedgerEntry, Need, Status
from dex_engine.pipeline.urls import work_hash
from tests.conftest import FakeDriver
from tests.drivers.conftest import FakeTransport, json_response
from tests.pipeline.test_run import ITEM, make_ctx, write_item

ENGINE = "0.5.0"
TODAY = datetime.date(2026, 8, 20)
NOW = datetime.datetime(2026, 8, 20, 8, 0, 0, 500000, tzinfo=datetime.UTC)


def fixed_today() -> datetime.date:
    return TODAY


def fixed_now() -> datetime.datetime:
    return NOW


@pytest.fixture
def migration():
    return build(today=fixed_today, now=fixed_now, engine_version=ENGINE)


class TestBuildGuard:
    def test_refuses_the_pre_rewrite_marker_version(self):
        # Seeds stamped 0.0.1 would satisfy this migration's own membership
        # test — a mis-built engine must be refused loudly, never seeded.
        with pytest.raises(MigrationError, match="pre-rewrite marker"):
            build(today=fixed_today, now=fixed_now, engine_version="0.0.1")

    def test_refuses_the_v_prefixed_marker_too(self):
        with pytest.raises(MigrationError, match="pre-rewrite marker"):
            build(today=fixed_today, now=fixed_now, engine_version="v0.0.1")

    def test_accepts_rewrite_versions(self):
        assert build(today=fixed_today, now=fixed_now, engine_version="0.1.0").number == 2


def url_of(tag):
    return f"https://a.test/{tag}"


def entry(  # noqa: PLR0913 — one keyword per exercised schema slot
    tag, *, kind, status=Status.DONE, engine="0.0.1", reason=None, error=None, at=None
):
    url = url_of(tag)
    unit_hash = work_hash(url)
    path = (
        f"enrichment/2026-05-01-item-a1b2c3/{kind.value}-{unit_hash[:6]}.md"
        if status is Status.DONE
        else None
    )
    return LedgerEntry(
        hash=unit_hash,
        url=url,
        item="2026-05-01-item-a1b2c3",
        kind=kind,
        status=status,
        engine=engine,
        date=datetime.date(2026, 5, 1),
        at=at,
        path=path,
        reason=reason,
        error=error,
    )


def write_entries(root, entries, *, items=True):
    """Write the ledger; by default give every named item a corpus file.

    Seeding requires the item to still exist, so the live-item case is the
    default and ``items=False`` is the purged one.
    """
    path = root / "state" / "enrichment-ledger.jsonl"
    for one in entries:
        ledger.append(path, one)
    if items:
        for item_id in {one.item for one in entries}:
            write_corpus_item(root, item_id)
    return path


def write_corpus_item(root, item_id, urls=()):
    path = root / "corpus" / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    listing = "".join(f"  - {url}\n" for url in urls)
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: manual\nchannel: inbox\nshared_by: alex\ndate: 2026-05-01\n"
        + (f"urls:\n{listing}" if listing else "")
        + "kinds: [web]\nstatus: raw\nenrichment: []\n---\n**alex**: note\n",
        encoding="utf-8",
    )
    return path


def write_exclusions(root, rows):
    path = root / "state" / "exclusions.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{item}\t{reason}\n" for item, reason in rows), encoding="utf-8")


class TestSeeding:
    def test_pre_rewrite_done_web_and_x_are_requeued(self, tmp_path, migration):
        path = write_entries(
            tmp_path,
            [
                entry("post-x", kind=Kind.X),
                entry("post-web", kind=Kind.WEB),
            ],
        )
        report = migration.apply(tmp_path)
        entries = ledger.load(path)
        for tag in ("post-x", "post-web"):
            seed = entries[work_hash(url_of(tag))]
            assert seed.status is Status.QUEUED
            assert seed.rerun is True
            assert seed.via == "migration-2"
            assert seed.engine == ENGINE
            assert seed.date == TODAY
            assert seed.path is None  # queued entries carry no outputs
        assert any(
            "1 x (thread walk-up), 1 web (link keeping)" in action for action in report.actions
        )

    def test_seed_reuses_the_url_keyed_work_unit(self, tmp_path, migration):
        original = entry("post-x", kind=Kind.X)
        path = write_entries(tmp_path, [original])
        migration.apply(tmp_path)
        seed = ledger.load(path)[original.hash]
        assert (seed.url, seed.item, seed.kind) == (original.url, original.item, original.kind)

    def test_other_kinds_and_statuses_stay_parked(self, tmp_path, migration):
        path = write_entries(
            tmp_path,
            [
                entry("a-video", kind=Kind.YOUTUBE),  # neither fix applies
                entry("gone", kind=Kind.X, status=Status.DEAD),
                entry("walled", kind=Kind.WEB, status=Status.MANUAL, reason="paywall"),
                # old error entries already retry under the new-engine rule
                entry("broken", kind=Kind.X, status=Status.ERROR, error="boom"),
            ],
        )
        report = migration.apply(tmp_path)
        entries = ledger.load(path)
        assert entries[work_hash(url_of("a-video"))].status is Status.DONE
        assert entries[work_hash(url_of("gone"))].status is Status.DEAD
        assert entries[work_hash(url_of("walled"))].status is Status.MANUAL
        assert entries[work_hash(url_of("broken"))].status is Status.ERROR
        assert report.actions == []

    def test_post_rewrite_done_entries_are_not_seeded(self, tmp_path, migration):
        # Entries written by the rewritten engine already have both fixes.
        path = write_entries(tmp_path, [entry("fresh", kind=Kind.WEB, engine="0.5.0")])
        migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(url_of("fresh"))].status is Status.DONE

    def test_membership_reads_the_latest_line_per_hash(self, tmp_path, migration):
        # A hash whose done line was superseded is not done any more.
        path = write_entries(
            tmp_path,
            [
                entry("superseded", kind=Kind.WEB),
                entry("superseded", kind=Kind.WEB, status=Status.DEAD, engine="0.5.0"),
            ],
        )
        migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(url_of("superseded"))].status is Status.DEAD

    def test_membership_resolves_a_hash_exactly_as_load_does(self, tmp_path, migration):
        # Re-application runs against state the engine has since written,
        # and a union merge can leave the OLDER line last in the file.
        # Resolving by file position would read the superseded pre-rewrite
        # `done` as live and requeue work the owner has since ruled on;
        # `resolution_key` is the one rule both sides ask.
        ruling = entry(
            "post-web",
            kind=Kind.WEB,
            status=Status.MANUAL,
            reason="owner ruled: read it myself",
            engine=ENGINE,
            at=datetime.datetime(2026, 8, 20, 7, 0, tzinfo=datetime.UTC),
        )
        superseded = entry(
            "post-web",
            kind=Kind.WEB,
            at=datetime.datetime(2026, 8, 20, 6, 0, tzinfo=datetime.UTC),
        )
        path = write_entries(tmp_path, [ruling, superseded])
        report = migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(url_of("post-web"))] == ruling
        assert not any("seeded" in action for action in report.actions)
        assert path.read_text(encoding="utf-8").count("\n") == 2

    def test_a_seed_carries_the_writers_own_write_instant(self, tmp_path, migration):
        # `at` is set by the writer seam and never hand-built at a call
        # site: it is what orders this line against another machine's.
        path = write_entries(tmp_path, [entry("post-web", kind=Kind.WEB)])
        migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(url_of("post-web"))].at == NOW

    def test_unparseable_engine_skipped_with_why(self, tmp_path, migration):
        url = "https://a.test/weird"
        record = {
            "hash": work_hash(url),
            "url": url,
            "item": "2026-05-01-item-a1b2c3",
            "kind": "web",
            "status": "done",
            "engine": "hand-healed",
            "date": "2026-05-01",
        }
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record) + "\n")
        report = migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(url)].status is Status.DONE
        assert any("unparseable engine" in s.why for s in report.skipped)

    def test_line_migration_1_left_untranslated_does_not_block_seeding(self, tmp_path, migration):
        good = entry("post-x", kind=Kind.X)
        bad_line = '{"hash": "bad", "status": "nocaptions"}'
        path = write_entries(tmp_path, [good])
        with path.open("a", encoding="utf-8") as f:
            f.write(bad_line + "\n")
        report = migration.apply(tmp_path)
        assert any("unreadable under the current schema" in s.why for s in report.skipped)
        lines = [line for line in path.read_text().split("\n") if line.strip()]
        assert bad_line in lines  # kept verbatim, nothing destroyed
        seeds = [line for line in lines if '"migration-2"' in line]
        assert len(seeds) == 1

    def test_missing_ledger_is_tolerated(self, tmp_path, migration):
        # Un-pulled repos are a supported input.
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []


class TestPurgedItemsAreNeverReseeded:
    """`dex exclude` deletes the item; a seed would fetch it right back."""

    def test_excluded_item_is_skipped_with_the_exclusion_named(self, tmp_path, migration):
        path = write_entries(tmp_path, [entry("post-web", kind=Kind.WEB)], items=False)
        write_exclusions(
            tmp_path, [("2026-05-01-item-a1b2c3", "out of scope — owner ruling 2026-08-20")]
        )
        report = migration.apply(tmp_path)
        assert '"migration-2"' not in path.read_text()
        assert report.actions == []
        (skip,) = report.skipped
        assert skip.what == "rerun seed for 2026-05-01-item-a1b2c3"
        assert "excluded, never reseeded" in skip.why
        assert "owner ruling 2026-08-20" in skip.why

    def test_end_to_end_the_next_run_does_not_resurrect_the_excluded_item(
        self, instance, migration
    ):
        # The whole point: exclude purges the item, the migration must not
        # queue it, and the following run must fetch nothing for it.
        ledger.append(instance.ledger_path, stored(OLD_X_URL, kind=Kind.X, item=ITEM))
        (instance.state_dir / "exclusions.tsv").parent.mkdir(parents=True, exist_ok=True)
        (instance.state_dir / "exclusions.tsv").write_text(
            f"{ITEM}\tout of scope: marketing content — owner ruling\n", encoding="utf-8"
        )
        report = migration.apply(instance.root)
        assert any("never reseeded" in s.why for s in report.skipped)
        transport = FakeTransport({})
        ctx = make_ctx(instance, FakeDriver(), drivers=[XDriver(transport=transport)])
        run_mod.run(ctx)
        assert transport.calls == []
        assert '"queued"' not in instance.ledger_path.read_text()

    def test_item_gone_without_an_exclusions_record_is_still_skipped(self, tmp_path, migration):
        path = write_entries(tmp_path, [entry("post-web", kind=Kind.WEB)], items=False)
        report = migration.apply(tmp_path)
        assert '"migration-2"' not in path.read_text()
        (skip,) = report.skipped
        assert "no state/exclusions.tsv record names the item" in skip.why

    def test_the_skip_states_what_was_checked_and_guesses_no_cause(self, tmp_path, migration):
        # Both checks are named, and nothing is asserted about how the item
        # went away — the migration never established that.
        write_entries(tmp_path, [entry("post-web", kind=Kind.WEB)], items=False)
        (skip,) = migration.apply(tmp_path).skipped
        assert "no live corpus item lists https://a.test/post-web" in skip.why
        assert "removed by hand" not in skip.why
        assert "purge was interrupted" not in skip.why

    def test_a_live_item_still_seeds(self, tmp_path, migration):
        path = write_entries(tmp_path, [entry("post-web", kind=Kind.WEB)])
        report = migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(url_of("post-web"))].status is Status.QUEUED
        assert report.skipped == []

    def test_only_the_seeded_cohort_is_item_checked(self, tmp_path, migration):
        # A purged item's parked/other-kind entries are none of this
        # migration's business: no seed, and no noise in the report.
        write_entries(
            tmp_path,
            [
                entry("a-video", kind=Kind.YOUTUBE),
                entry("walled", kind=Kind.WEB, status=Status.MANUAL, reason="paywall"),
            ],
            items=False,
        )
        report = migration.apply(tmp_path)
        assert report.skipped == []


class TestRenamedItemsAreReAttributed:
    """A rename keeps the shortid and takes a new slug — the work is live."""

    RENAMED = "2026-05-01-renamed-a1b2c3"

    def test_a_renamed_item_seeds_under_its_live_id(self, tmp_path, migration):
        one = entry("post-x", kind=Kind.X)
        path = write_entries(tmp_path, [one], items=False)
        write_corpus_item(tmp_path, self.RENAMED, urls=[url_of("post-x")])
        report = migration.apply(tmp_path)
        seed = ledger.load(path)[one.hash]
        assert seed.status is Status.QUEUED
        assert seed.rerun is True
        assert seed.item == self.RENAMED
        assert report.skipped == []
        assert any("1 re-attributed" in action for action in report.actions)

    def test_the_stale_done_line_keeps_its_own_attribution(self, tmp_path, migration):
        # History is what it was: only the seed names the live item.
        one = entry("post-x", kind=Kind.X)
        path = write_entries(tmp_path, [one], items=False)
        write_corpus_item(tmp_path, self.RENAMED, urls=[url_of("post-x")])
        migration.apply(tmp_path)
        lines = [json.loads(line) for line in path.read_text().split("\n") if line.strip()]
        assert lines[0]["item"] == "2026-05-01-item-a1b2c3"
        assert lines[0]["status"] == "done"
        assert lines[-1]["item"] == self.RENAMED

    def test_a_second_apply_re_attributes_nothing_more(self, tmp_path, migration):
        write_entries(tmp_path, [entry("post-x", kind=Kind.X)], items=False)
        write_corpus_item(tmp_path, self.RENAMED, urls=[url_of("post-x")])
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        migration.apply(tmp_path)
        first = path.read_text()
        second = migration.apply(tmp_path)
        assert path.read_text() == first
        assert second.actions == []
        assert second.skipped == []

    def test_the_url_is_matched_on_current_identity_not_spelling(self, tmp_path, migration):
        # The corpus lists the pre-rewrite spelling; the re-keyed entry
        # carries the id-keyed one. Same unit, so the claim holds.
        path = write_entries(tmp_path, [stored(OLD_X_URL, kind=Kind.X)], items=False)
        write_corpus_item(tmp_path, self.RENAMED, urls=[OLD_X_URL])
        migration.apply(tmp_path)
        seed = ledger.load(path)[work_hash(NEW_X_URL)]
        assert seed.status is Status.QUEUED
        assert seed.item == self.RENAMED

    def test_a_claim_by_a_live_item_outranks_a_stale_exclusions_row(self, tmp_path, migration):
        # The exclusions row names an id with no file; a live item listing
        # the URL is the owner's later word on the same work.
        one = entry("post-web", kind=Kind.WEB)
        path = write_entries(tmp_path, [one], items=False)
        write_exclusions(tmp_path, [("2026-05-01-item-a1b2c3", "excluded 2026-06-01")])
        write_corpus_item(tmp_path, self.RENAMED, urls=[url_of("post-web")])
        report = migration.apply(tmp_path)
        assert ledger.load(path)[one.hash].item == self.RENAMED
        assert report.skipped == []

    def test_an_excluded_item_no_live_url_claims_is_still_refused(self, tmp_path, migration):
        # The other corpus item is live but lists a different URL — the
        # exclusion stands, refused exactly as before.
        path = write_entries(tmp_path, [entry("post-web", kind=Kind.WEB)], items=False)
        write_exclusions(tmp_path, [("2026-05-01-item-a1b2c3", "out of scope")])
        write_corpus_item(tmp_path, self.RENAMED, urls=[url_of("other")])
        report = migration.apply(tmp_path)
        assert '"migration-2"' not in path.read_text()
        (skip,) = report.skipped
        assert "excluded, never reseeded" in skip.why


class TestIdempotency:
    def test_second_apply_seeds_nothing(self, tmp_path, migration):
        path = write_entries(tmp_path, [entry("post-x", kind=Kind.X)])
        migration.apply(tmp_path)
        first = path.read_text()
        second_report = migration.apply(tmp_path)
        assert path.read_text() == first
        assert second_report.actions == []


OLD_X_URL = "https://x.com/carol/status/300"  # the pre-rewrite canonical form
NEW_X_URL = "https://x.com/i/status/300"  # the id-keyed identity
LIVE_URL = "https://youtube.com/live/dQw4w9WgXcA"  # a pre-collapse video shape
WATCH_URL = "https://youtube.com/watch?v=dQw4w9WgXcA"  # its current identity


def stored(url, *, kind, status=Status.DONE, reason=None, item="2026-05-01-item-a1b2c3"):
    unit_hash = work_hash(url)
    path = f"enrichment/{item}/{kind.value}-{unit_hash[:6]}.md" if status is Status.DONE else None
    return LedgerEntry(
        hash=unit_hash,
        url=url,
        item=item,
        kind=kind,
        status=status,
        engine="0.0.1",
        date=datetime.date(2026, 5, 1),
        path=path,
        reason=reason,
    )


class TestIdentityRekey:
    """Every entry moves to the CURRENT identity first; statuses are sacred."""

    def test_pre_rewrite_x_entry_requeues_under_the_id_keyed_identity(self, tmp_path, migration):
        path = write_entries(tmp_path, [stored(OLD_X_URL, kind=Kind.X)])
        migration.apply(tmp_path)
        seed = ledger.load(path)[work_hash(NEW_X_URL)]
        assert seed.status is Status.QUEUED
        assert seed.url == NEW_X_URL
        assert seed.kind is Kind.X
        assert seed.rerun is True
        assert seed.via == "migration-2"

    def test_the_done_history_moves_under_the_new_hash(self, tmp_path, migration):
        path = write_entries(tmp_path, [stored(OLD_X_URL, kind=Kind.X)])
        report = migration.apply(tmp_path)
        lines = [json.loads(line) for line in path.read_text().split("\n") if line.strip()]
        assert [line["hash"] for line in lines] == [work_hash(NEW_X_URL)] * 2
        history, seed = lines
        assert (history["status"], history["engine"]) == ("done", "0.0.1")
        assert history["url"] == NEW_X_URL
        assert seed["status"] == "queued"
        assert any("re-keyed 1 entry" in action for action in report.actions)

    def test_manual_x_entry_is_rekeyed_with_its_verdict_preserved(self, tmp_path, migration):
        path = write_entries(
            tmp_path, [stored(OLD_X_URL, kind=Kind.X, status=Status.MANUAL, reason="parked")]
        )
        migration.apply(tmp_path)
        entries = ledger.load(path)
        assert set(entries) == {work_hash(NEW_X_URL)}  # the old key is gone
        moved = entries[work_hash(NEW_X_URL)]
        assert moved.status is Status.MANUAL  # the verdict survives, never requeued
        assert moved.reason == "parked"
        assert moved.engine == "0.0.1"
        assert '"queued"' not in path.read_text()

    def test_done_youtube_live_entry_rekeys_and_is_not_requeued(self, tmp_path, migration):
        path = write_entries(tmp_path, [stored(LIVE_URL, kind=Kind.YOUTUBE)])
        report = migration.apply(tmp_path)
        entries = ledger.load(path)
        assert set(entries) == {work_hash(WATCH_URL)}
        moved = entries[work_hash(WATCH_URL)]
        assert moved.status is Status.DONE  # youtube has no deficient cohort
        assert moved.url == WATCH_URL
        assert any("re-keyed 1 entry" in action for action in report.actions)
        assert '"queued"' not in path.read_text()

    def test_web_identity_is_unchanged_so_the_requeue_is_hash_stable(self, tmp_path, migration):
        url = "https://blog.example.test/post"
        path = write_entries(tmp_path, [stored(url, kind=Kind.WEB)])
        migration.apply(tmp_path)
        entries = ledger.load(path)
        assert set(entries) == {work_hash(url)}  # no second key was minted
        seed = entries[work_hash(url)]
        assert seed.status is Status.QUEUED
        assert seed.url == url

    def test_identity_unchanged_url_text_difference_still_seeds(self, tmp_path, migration):
        # The stored text differs from the canonical spelling, but the
        # stored hash IS the current identity — no re-key, and the rerun
        # must still seed.
        canonical = "https://a.test/slash"
        original = stored(canonical, kind=Kind.WEB)
        text_variant = LedgerEntry(
            hash=original.hash,
            url=canonical + "/",
            item=original.item,
            kind=original.kind,
            status=original.status,
            engine=original.engine,
            date=original.date,
            path=original.path,
        )
        path = write_entries(tmp_path, [text_variant])
        report = migration.apply(tmp_path)
        seed = ledger.load(path)[original.hash]
        assert seed.status is Status.QUEUED
        assert seed.rerun is True
        assert any("1 web (link keeping)" in action for action in report.actions)

    def test_two_old_spellings_of_one_post_collapse_and_seed_once(self, tmp_path, migration):
        # Pre-rewrite, x.com and twitter.com hashed separately; both re-key
        # to the one id-keyed identity, last-per-hash settles the pair, and
        # the drain fetches once.
        path = write_entries(
            tmp_path,
            [
                stored(OLD_X_URL, kind=Kind.X),
                stored("https://twitter.com/carol/status/300", kind=Kind.X),
            ],
        )
        report = migration.apply(tmp_path)
        entries = ledger.load(path)
        assert set(entries) == {work_hash(NEW_X_URL)}
        assert entries[work_hash(NEW_X_URL)].status is Status.QUEUED
        assert any(
            "re-keyed 2 entries" in action and "1 collapsed duplicate" in action
            for action in report.actions
        )
        assert any("1 x (thread walk-up)" in action for action in report.actions)

    def test_second_apply_is_a_no_op_after_the_rekey(self, tmp_path, migration):
        path = write_entries(tmp_path, [stored(OLD_X_URL, kind=Kind.X)])
        migration.apply(tmp_path)
        first = path.read_text()
        second_report = migration.apply(tmp_path)
        assert path.read_text() == first
        assert second_report.actions == []


SIGNED_MEDIA_URL = "https://cdn.a.test/clip.mp4?Expires=1&Signature=abc&ref=share"
ASSET_PATH = "enrichment/2026-05-01-item-a1b2c3/paper-asset-01.png"


def child(url, *, parent, via, kind=Kind.X):
    return LedgerEntry(
        hash=work_hash(url),
        url=url,
        item="2026-05-01-item-a1b2c3",
        kind=kind,
        status=Status.DONE,
        engine="0.4.0",
        date=datetime.date(2026, 5, 1),
        via=via,
        parent=parent,
        depth=1,
        path=f"enrichment/2026-05-01-item-a1b2c3/{kind.value}-{work_hash(url)[:6]}.md",
    )


def write_output(root, entry, *, body="the pre-rewrite view", kind=None, url=None):
    """The unit's enrichment file as the drain writes it: `<kind>-<hash6>.md`."""
    name = f"{(kind or entry.kind).value}-{entry.hash[:6]}.md"
    path = root / "enrichment" / entry.item / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nurl: {json.dumps(url or entry.url)}\nfetched: 2026-05-01\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


class TestOutputsFollowTheRekey:
    """`<kind>-<hash6>.md` names an identity, so a re-key has to move it too."""

    def test_the_rerun_replaces_the_stale_view_instead_of_landing_beside_it(
        self, instance, migration
    ):
        # The whole rollout, end to end: the file stayed on the OLD hash,
        # `_drop_superseded_outputs` builds its candidates from the NEW one
        # and could never see it, and the item ended up carrying two views
        # of one x post — both listed in engine-owned `enrichment:`.
        original = stored(OLD_X_URL, kind=Kind.X, item=ITEM)
        ledger.append(instance.ledger_path, original)
        write_item(instance, urls=[OLD_X_URL], kinds=["x"])
        write_output(instance.root, original)
        migration.apply(instance.root)
        tweet = {
            "id": "300",
            "text": "thread body " * 30,
            "created_at": "Thu Aug 20 10:00:00 +0000 2026",
            "author": {"name": "Carol Chen", "screen_name": "carol"},
            "replying_to": None,
            "replying_to_status": None,
        }
        transport = FakeTransport(
            {"https://api.fxtwitter.com/status/300": json_response({"tweet": tweet})}
        )
        ctx = make_ctx(instance, FakeDriver(), drivers=[XDriver(transport=transport)])
        run_mod.run(ctx)
        current = f"x-{work_hash(NEW_X_URL)[:6]}.md"
        assert sorted(p.name for p in (instance.enrichment_dir / ITEM).glob("*.md")) == [current]
        item = corpus.read_item(instance.corpus_dir / "2026" / f"{ITEM}.md")
        assert item.enrichment == [current]

    def test_the_stored_path_follows_the_file_it_names(self, tmp_path, migration):
        # A `path` left on the old name is a done line pointing at nothing
        # — the exact shape dex-lint reports as a missing output.
        original = stored(OLD_X_URL, kind=Kind.X)
        path = write_entries(tmp_path, [original])
        write_output(tmp_path, original)
        report = migration.apply(tmp_path)
        history = json.loads(path.read_text().split("\n")[0])
        current = f"enrichment/{original.item}/x-{work_hash(NEW_X_URL)[:6]}.md"
        assert history["path"] == current
        assert (tmp_path / history["path"]).is_file()
        assert any("output file(s) renamed" in action for action in report.actions)

    def test_a_youtube_parks_description_survives_into_the_transcribe_drain(
        self, tmp_path, migration
    ):
        # The silent-data-loss leg: a `nocaptions` park migration 1 turned
        # into waiting/transcribe carries its description in the file, and
        # the transcribe drain reads `youtube-<hash6>.md` off the CURRENT
        # hash. Left behind, the description is simply gone.
        park = LedgerEntry(
            hash=work_hash(LIVE_URL),
            url=LIVE_URL,
            item="2026-05-01-item-a1b2c3",
            kind=Kind.YOUTUBE,
            status=Status.WAITING,
            needs=Need.TRANSCRIBE,
            engine="0.0.1",
            date=datetime.date(2026, 5, 1),
        )
        ledger_path = write_entries(tmp_path, [park])
        write_output(tmp_path, park, body="the stream's stored description")
        migration.apply(tmp_path)
        moved = ledger.load(ledger_path)[work_hash(WATCH_URL)]
        drain_reads = tmp_path / "enrichment" / moved.item / f"youtube-{moved.hash[:6]}.md"
        _fields, body = read_enrichment(drain_reads)
        assert body == "the stream's stored description"

    def test_a_collapsed_pairs_second_view_is_reported_not_overwritten(self, tmp_path, migration):
        # Both old spellings of one post land on one identity, so the
        # second file's target already exists. Overwriting would destroy a
        # view; it stays, named on the report.
        first = stored(OLD_X_URL, kind=Kind.X)
        second = stored("https://twitter.com/carol/status/300", kind=Kind.X)
        write_entries(tmp_path, [first, second])
        write_output(tmp_path, first, body="the x.com view")
        write_output(tmp_path, second, body="the twitter.com view")
        report = migration.apply(tmp_path)
        item_dir = tmp_path / "enrichment" / first.item
        assert sorted(p.name for p in item_dir.glob("*.md")) == sorted(
            [f"x-{work_hash(NEW_X_URL)[:6]}.md", f"x-{second.hash[:6]}.md"]
        )
        assert (item_dir / f"x-{second.hash[:6]}.md").read_text().endswith("the twitter.com view\n")
        assert any("refusing to overwrite" in one.why for one in report.skipped)

    def test_a_hash6_neighbours_output_is_left_alone(self, tmp_path, migration):
        # Six hex digits collide, so the candidate must prove it belongs to
        # this unit by the URL it records — the drain's own drop guards the
        # same way. Moving a neighbour's file would strand THAT unit.
        original = stored(OLD_X_URL, kind=Kind.X)
        write_entries(tmp_path, [original])
        neighbour = write_output(
            tmp_path, original, body="another unit's view", url="https://a.test/neighbour"
        )
        migration.apply(tmp_path)
        assert neighbour.is_file()
        assert not (neighbour.parent / f"x-{work_hash(NEW_X_URL)[:6]}.md").exists()


class TestVerbatimKeysAreNotRekeyed:
    """Some work keys are the fetch string itself; canonicalizing breaks them."""

    def test_media_line_is_byte_identical_through_the_migration(self, tmp_path, migration):
        # Canonicalizing would strip `ref` from a signed URL and key the
        # entry away from the runtime's raw-hash discipline.
        parent = stored(OLD_X_URL, kind=Kind.X)
        media = child(SIGNED_MEDIA_URL, parent=parent.hash, via="media")
        path = write_entries(tmp_path, [parent, media])
        before = path.read_text().split("\n")[1]
        migration.apply(tmp_path)
        after = path.read_text().split("\n")[1]
        assert json.loads(after)["hash"] == work_hash(SIGNED_MEDIA_URL)
        assert json.loads(after)["url"] == SIGNED_MEDIA_URL
        # only the parent pointer moved — the rest of the line is untouched
        assert json.loads(before) | {"parent": work_hash(NEW_X_URL)} == json.loads(after)

    def test_media_line_under_a_stable_parent_is_untouched(self, tmp_path, migration):
        parent = stored("https://blog.example.test/post", kind=Kind.WEB)
        media = child(SIGNED_MEDIA_URL, parent=parent.hash, via="media", kind=Kind.WEB)
        path = write_entries(tmp_path, [parent, media])
        before = path.read_text()
        migration.apply(tmp_path)
        assert path.read_text().startswith(before)  # the seed appends; nothing was rewritten

    def test_extract_asset_repo_path_is_never_mangled_into_a_url(self, tmp_path, migration):
        parent = stored("https://blog.example.test/paper", kind=Kind.WEB)
        asset = child(ASSET_PATH, parent=parent.hash, via="extract-asset", kind=Kind.WEB)
        path = write_entries(tmp_path, [parent, asset])
        migration.apply(tmp_path)
        line = json.loads(path.read_text().split("\n")[1])
        assert line["url"] == ASSET_PATH
        assert line["hash"] == work_hash(ASSET_PATH)

    def test_a_non_http_work_key_keeps_its_identity(self, tmp_path, migration):
        # `file:<repo-path>` keys are verbatim for the same reason.
        key = "file:inbox/paper.pdf"
        entry = LedgerEntry(
            hash=work_hash(key),
            url=key,
            item="2026-05-01-item-a1b2c3",
            kind=Kind.FILE,
            status=Status.DONE,
            engine="0.4.0",
            date=datetime.date(2026, 5, 1),
            path="enrichment/2026-05-01-item-a1b2c3/file-abc123.md",
        )
        path = write_entries(tmp_path, [entry])
        before = path.read_text()
        migration.apply(tmp_path)
        assert path.read_text() == before

    def test_a_rekeyed_parent_takes_its_children_with_it(self, tmp_path, migration):
        # A promoted child is canonically keyed AND has a parent pointer:
        # both must land on current identities, or the chain dangles.
        parent = stored(OLD_X_URL, kind=Kind.X)
        reply = child("https://twitter.com/carol/status/301", parent=parent.hash, via="harvest")
        path = write_entries(tmp_path, [parent, reply])
        report = migration.apply(tmp_path)
        entries = ledger.load(path)
        moved = entries[work_hash("https://x.com/i/status/301")]
        assert moved.parent == work_hash(NEW_X_URL)
        assert moved.parent in entries  # the pointer resolves
        assert any("1 parent pointer(s) followed" in action for action in report.actions)

    def test_a_child_listed_before_its_parent_still_follows(self, tmp_path, migration):
        # File order is birth order in practice, never by contract.
        parent = stored(OLD_X_URL, kind=Kind.X)
        media = child(SIGNED_MEDIA_URL, parent=parent.hash, via="media")
        path = write_entries(tmp_path, [media, parent])
        migration.apply(tmp_path)
        assert json.loads(path.read_text().split("\n")[0])["parent"] == work_hash(NEW_X_URL)

    def test_a_parent_pointer_naming_nothing_is_left_alone(self, tmp_path, migration):
        orphan = child(SIGNED_MEDIA_URL, parent="0123456789", via="media")
        path = write_entries(tmp_path, [orphan])
        before = path.read_text()
        migration.apply(tmp_path)
        assert path.read_text() == before

    def test_second_apply_moves_nothing(self, tmp_path, migration):
        parent = stored(OLD_X_URL, kind=Kind.X)
        media = child(SIGNED_MEDIA_URL, parent=parent.hash, via="media")
        path = write_entries(tmp_path, [parent, media])
        migration.apply(tmp_path)
        first = path.read_text()
        report = migration.apply(tmp_path)
        assert path.read_text() == first
        assert report.actions == []


class TestCanonicalizationGuard:
    def test_one_unreadable_url_is_skipped_with_why_and_the_rest_migrate(self, tmp_path, migration):
        # urlsplit raises on an invalid IPv6 literal; a migration that dies
        # here leaves the chain half-applied.
        broken = LedgerEntry(
            hash="1111111111",
            url="https://[bad:ipv6/page",
            item="2026-05-01-item-a1b2c3",
            kind=Kind.WEB,
            status=Status.MANUAL,
            reason="hand-healed",
            engine="0.0.1",
            date=datetime.date(2026, 5, 1),
        )
        path = write_entries(tmp_path, [broken, stored(OLD_X_URL, kind=Kind.X)])
        report = migration.apply(tmp_path)
        assert json.loads(path.read_text().split("\n")[0])["hash"] == "1111111111"
        assert any("canonicalizing" in s.why and "ValueError" in s.why for s in report.skipped)
        assert work_hash(NEW_X_URL) in ledger.load(path)  # the healthy entry still moved


def _refuse_probe(url):
    raise AssertionError(f"unexpected probe of {url!r}")


class TestSeedDedupe:
    def test_corpus_seeding_dedupes_against_the_rekeyed_seed(self, instance, migration):
        # The rollout property: after migration 2, the next run must NOT
        # raise the same x post fresh under the new hash while also
        # draining the rerun — one queued unit, one fetch.
        ledger.append(instance.ledger_path, stored(OLD_X_URL, kind=Kind.X, item=ITEM))
        write_item(instance, urls=[OLD_X_URL])
        migration.apply(instance.root)
        tweet = {
            "id": "300",
            "text": "thread body " * 30,
            "created_at": "Thu Aug 20 10:00:00 +0000 2026",
            "author": {"name": "Carol Chen", "screen_name": "carol"},
            "replying_to": None,
            "replying_to_status": None,
        }
        transport = FakeTransport(
            {"https://api.fxtwitter.com/status/300": json_response({"tweet": tweet})}
        )
        ctx = make_ctx(instance, FakeDriver(), drivers=[XDriver(transport=transport)])
        run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        assert set(entries) == {work_hash(NEW_X_URL)}
        assert entries[work_hash(NEW_X_URL)].status is Status.DONE
        lines = [
            json.loads(line)
            for line in instance.ledger_path.read_text().split("\n")
            if line.strip()
        ]
        queued = [
            line
            for line in lines
            if line["hash"] == work_hash(NEW_X_URL) and line["status"] == "queued"
        ]
        assert len(queued) == 1  # the migration seed; corpus seeding minted no second unit
        assert transport.calls == [("GET", "https://api.fxtwitter.com/status/300")]

    def test_manual_x_verdict_is_not_resurrected_by_corpus_seeding(self, instance, migration):
        # Work the owner deliberately parked must stay parked: after the
        # re-key, corpus seeding finds the manual verdict under the current
        # hash and never mints fresh queued work — and nothing is fetched.
        ledger.append(
            instance.ledger_path,
            stored(OLD_X_URL, kind=Kind.X, status=Status.MANUAL, reason="parked", item=ITEM),
        )
        migration.apply(instance.root)
        write_item(instance, urls=[OLD_X_URL])
        transport = FakeTransport({})
        ctx = make_ctx(instance, FakeDriver(), drivers=[XDriver(transport=transport)])
        run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        assert set(entries) == {work_hash(NEW_X_URL)}
        assert entries[work_hash(NEW_X_URL)].status is Status.MANUAL
        assert transport.calls == []

    def test_done_youtube_live_is_not_double_enriched_on_the_next_run(self, instance, migration):
        # A live stream already enriched must not re-fetch (and re-transcribe)
        # under its collapsed watch identity.
        ledger.append(instance.ledger_path, stored(LIVE_URL, kind=Kind.YOUTUBE, item=ITEM))
        migration.apply(instance.root)
        write_item(instance, urls=[LIVE_URL], kinds=["youtube"])
        ctx = make_ctx(instance, FakeDriver(), drivers=[YouTubeDriver(probe=_refuse_probe)])
        run_mod.run(ctx)  # completes; _refuse_probe proves nothing re-fetched
        entries = ledger.load(instance.ledger_path)
        assert set(entries) == {work_hash(WATCH_URL)}
        assert entries[work_hash(WATCH_URL)].status is Status.DONE
