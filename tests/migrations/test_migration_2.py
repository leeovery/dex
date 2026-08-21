"""Migration 2: the identity re-key + rerun seed for pre-rewrite state."""

import datetime
import json

import pytest

from dex_engine.drivers.x import XDriver
from dex_engine.drivers.youtube import YouTubeDriver
from dex_engine.migrations import MigrationError
from dex_engine.migrations.migration_2 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.types import Kind, LedgerEntry, Status
from dex_engine.pipeline.urls import work_hash
from tests.conftest import FakeDriver
from tests.drivers.conftest import FakeTransport, json_response
from tests.pipeline.test_run import ITEM, make_ctx, write_item

ENGINE = "0.5.0"
TODAY = datetime.date(2026, 8, 20)


def fixed_today() -> datetime.date:
    return TODAY


@pytest.fixture
def migration():
    return build(today=fixed_today, engine_version=ENGINE)


class TestBuildGuard:
    def test_refuses_the_pre_rewrite_marker_version(self):
        # Seeds stamped 0.0.1 would satisfy this migration's own membership
        # test — a mis-built engine must be refused loudly, never seeded.
        with pytest.raises(MigrationError, match="pre-rewrite marker"):
            build(today=fixed_today, engine_version="0.0.1")

    def test_refuses_the_v_prefixed_marker_too(self):
        with pytest.raises(MigrationError, match="pre-rewrite marker"):
            build(today=fixed_today, engine_version="v0.0.1")

    def test_accepts_rewrite_versions(self):
        assert build(today=fixed_today, engine_version="0.1.0").number == 2


def url_of(tag):
    return f"https://a.test/{tag}"


def entry(  # noqa: PLR0913 — one keyword per exercised schema slot
    tag, *, kind, status=Status.DONE, engine="0.0.1", reason=None, error=None
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
        path=path,
        reason=reason,
        error=error,
    )


def write_entries(root, entries):
    path = root / "state" / "enrichment-ledger.jsonl"
    for one in entries:
        ledger.append(path, one)
    return path


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


def _refuse_probe(url):
    raise AssertionError(f"unexpected probe of {url!r}")


class TestSeedDedupe:
    def test_corpus_seeding_dedupes_against_the_rekeyed_seed(self, instance, migration):
        # The rollout property: after migration 2, the next run must NOT
        # raise the same x post fresh under the new hash while also
        # draining the rerun — one queued unit, one fetch.
        ledger.append(instance.ledger_path, stored(OLD_X_URL, kind=Kind.X, item=ITEM))
        migration.apply(instance.root)
        write_item(instance, urls=[OLD_X_URL])
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
