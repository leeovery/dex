"""Tests for exclude.py: purge + record, surviving re-normalization."""

import datetime
import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from dex_engine.exclude import main, run_exclude
from dex_engine.instance_map import compile_map, serialize_map
from dex_engine.normalize import load_exclusions
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.ownership import work_identity
from dex_engine.pipeline.registry import default_drivers
from dex_engine.pipeline.types import Config, Instance, Kind, LedgerEntry, Status
from tests.conftest import FakeDriver
from tests.drivers.conftest import FakeTransport

DRIVERS = default_drivers()

ITEM = "2026-08-19-example-55ad7b"
OTHER = "2026-08-19-other-11ff22"
SHARED_URL = "https://example.test/shared-by-two"


def write_item_stub(
    instance: Instance,
    item_id: str = ITEM,
    *,
    urls: tuple[str, ...] = (),
    media: tuple[str, ...] = (),
) -> None:
    path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    url_lines = "".join(f"  - {url}\n" for url in urls)
    media_lines = "".join(f"  - {repo_path}\n" for repo_path in media)
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: manual\nchannel: inbox\nshared_by: alex\ndate: 2026-08-19\n"
        + (f"urls:\n{url_lines}" if url_lines else "")
        + (f"media:\n{media_lines}" if media_lines else "")
        + "kinds: [web]\nstatus: raw\nenrichment: []\n---\n**alex**: note\n",
        encoding="utf-8",
    )


def write_media(instance: Instance, *repo_paths: str) -> list[Path]:
    """Real files at each repo path, the way `dex inbox` lands a capture's binary."""
    files = []
    for repo_path in repo_paths:
        path = instance.root / repo_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG not really")
        files.append(path)
    return files


def ledger_entry(  # noqa: PLR0913 — one keyword per ledger identity slot
    instance: Instance,
    unit_hash: str,
    item_id: str,
    *,
    url: str | None = None,
    parent: str | None = None,
    at: datetime.datetime = datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.UTC),
) -> None:
    ledger.append(
        instance.ledger_path,
        LedgerEntry(
            hash=unit_hash,
            url=url or f"https://example.test/{unit_hash}",
            item=item_id,
            kind=Kind.WEB,
            status=Status.QUEUED,
            engine="0.2.1",
            date=datetime.date(2026, 8, 20),
            at=at,
            parent=parent,
            depth=None if parent is None else 1,
        ),
    )


class TestRunExclude:
    def test_removes_item_enrichment_and_records_reason(self, instance):
        write_item_stub(instance)
        enrichment = instance.enrichment_dir / ITEM
        enrichment.mkdir(parents=True)
        (enrichment / "web-abc123.md").write_text("fetched")
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert summary == (
            "excluded 1: removed 1 items (0 already gone), 0 digests, 0 media files, 0 pass "
            "records, 0 ledger entries dropped, 0 kept (work another live corpus item still "
            "claims); map and index recompiled"
        )
        assert not (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert not enrichment.exists()
        recorded = (instance.state_dir / "exclusions.tsv").read_text()
        assert recorded == f"{ITEM}\tmeme thread\n"

    def test_the_digest_goes_with_the_item(self, instance):
        # An excluded item that had been digested left `state/digests/<id>.md`
        # behind: a permanent fact index over content ruled to yield nothing,
        # still feeding query and wiki, and lint's digest check is
        # shape-only so nothing ever named it.
        write_item_stub(instance)
        digest = instance.digests_dir / f"{ITEM}.md"
        digest.parent.mkdir(parents=True, exist_ok=True)
        digest.write_text("# facts\n", encoding="utf-8")
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert not digest.exists()
        assert "1 digests" in summary

    def test_a_digest_symlinked_out_of_the_instance_is_refused(self, instance, tmp_path_factory):
        # The digest deletion obeys the same containment check as the other
        # two: the id is well formed, the file it names is not inside.
        outside = tmp_path_factory.mktemp("outside") / "keepsake.md"
        outside.write_text("not this instance's to delete")
        write_item_stub(instance)
        instance.digests_dir.mkdir(parents=True, exist_ok=True)
        (instance.digests_dir / f"{ITEM}.md").symlink_to(outside)
        with pytest.raises(ValueError, match="outside the instance"):
            run_exclude(instance, [{"id": ITEM, "reason": "out of scope"}])
        assert outside.exists()
        assert (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()

    def test_missing_reason_defaults(self, instance):
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM}])
        recorded = (instance.state_dir / "exclusions.tsv").read_text()
        assert recorded == f"{ITEM}\tyields nothing through this instance's lens\n"

    def test_already_gone_items_counted_not_fatal(self, instance):
        summary = run_exclude(instance, [{"id": ITEM}])
        assert summary == (
            "excluded 1: removed 0 items (1 already gone), 0 digests, 0 media files, 0 pass "
            "records, 0 ledger entries dropped, 0 kept (work another live corpus item still "
            "claims); map and index recompiled"
        )

    def test_re_excluding_never_duplicates_the_record(self, instance):
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        lines = (instance.state_dir / "exclusions.tsv").read_text().splitlines()
        assert lines == [f"{ITEM}\tmeme"]

    def test_entry_without_id_is_loud(self, instance):
        with pytest.raises(ValueError, match="no id"):
            run_exclude(instance, [{"reason": "meme"}])

    def test_normalize_reads_the_record_back(self, instance):
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM}])
        assert load_exclusions(instance.state_dir / "exclusions.tsv") == {"55ad7b"}

    def test_reason_whitespace_collapses_so_the_tsv_stays_parseable(self, instance):
        # A tab or newline in the LLM-authored reason would shear the TSV:
        # a stray line computes a bogus shortid and can silently suppress
        # an unrelated cluster's regeneration.
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM, "reason": "meme\tthread —\nno technical content"}])
        recorded = (instance.state_dir / "exclusions.tsv").read_text()
        assert recorded == f"{ITEM}\tmeme thread — no technical content\n"
        assert load_exclusions(instance.state_dir / "exclusions.tsv") == {"55ad7b"}

    def test_whitespace_only_reason_falls_back_to_the_default(self, instance):
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM, "reason": " \n\t "}])
        recorded = (instance.state_dir / "exclusions.tsv").read_text()
        assert recorded == f"{ITEM}\tyields nothing through this instance's lens\n"


class TestLedgerPurge:
    def test_only_the_excluded_items_entries_go(self, instance):
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        ledger_entry(instance, "bbbbbbbbbb", ITEM)
        ledger_entry(instance, "cccccccccc", OTHER)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert "2 ledger entries dropped" in summary
        assert set(ledger.load(instance.ledger_path)) == {"cccccccccc"}

    def test_the_exclusions_record_is_still_written(self, instance):
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        recorded = (instance.state_dir / "exclusions.tsv").read_text()
        assert recorded == f"{ITEM}\tmeme thread\n"

    def test_re_running_is_idempotent(self, instance):
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        ledger_entry(instance, "cccccccccc", OTHER)
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        after = instance.ledger_path.read_text()
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert "0 ledger entries dropped" in summary
        assert instance.ledger_path.read_text() == after

    def test_a_missing_ledger_is_not_an_error(self, instance):
        write_item_stub(instance)
        assert "0 ledger entries dropped" in run_exclude(instance, [{"id": ITEM}])
        assert not instance.ledger_path.exists()

    def test_work_another_live_item_still_claims_survives(self, instance):
        # A work unit is keyed by URL hash, not by item: two corpus items
        # listing one URL share one entry, and it names only one of them.
        # Purging on the name deletes the other item's history too.
        shared = work_identity(SHARED_URL, DRIVERS)
        write_item_stub(instance, ITEM, urls=(SHARED_URL,))
        write_item_stub(instance, OTHER, urls=(SHARED_URL,))
        ledger_entry(instance, shared, ITEM, url=SHARED_URL)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert set(ledger.load(instance.ledger_path)) == {shared}
        assert "0 ledger entries dropped" in summary
        assert "1 kept" in summary

    def test_the_purged_item_cannot_claim_its_own_work(self, instance):
        # The corpus is scanned after the deletions: reading it first would
        # find the item about to go still listing the URL, and the purge
        # would veto itself.
        url = "https://example.test/only-this-item"
        write_item_stub(instance, ITEM, urls=(url,))
        ledger_entry(instance, work_identity(url, DRIVERS), ITEM, url=url)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert ledger.load(instance.ledger_path) == {}
        assert "1 ledger entries dropped, 0 kept" in summary

    def test_a_superseded_line_naming_another_item_goes_with_its_hash(self, instance):
        # The unit going is the hash, audit trail included — leaving an older
        # line behind keeps the ghost visible to every raw grep of the file.
        write_item_stub(instance, OTHER)
        ledger_entry(instance, "aaaaaaaaaa", OTHER)
        ledger_entry(
            instance,
            "aaaaaaaaaa",
            ITEM,
            at=datetime.datetime(2026, 8, 20, 10, 0, tzinfo=datetime.UTC),
        )
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert instance.ledger_path.read_text() == ""

    def test_the_whole_hashs_history_goes_when_nothing_claims_it(self, instance):
        # The audit trail belongs to the hash: leaving superseded lines keeps
        # the ghost visible to every raw grep of the file.
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        ledger_entry(
            instance,
            "aaaaaaaaaa",
            ITEM,
            at=datetime.datetime(2026, 8, 20, 10, 0, tzinfo=datetime.UTC),
        )
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert instance.ledger_path.read_text() == ""

    def test_a_hash_re_parented_to_a_live_item_is_kept_whole(self, instance):
        # The live line decides: an older line naming the excluded item is a
        # re-parenting's history, and it retires at `compact` like any
        # superseded line.
        write_item_stub(instance)
        write_item_stub(instance, OTHER)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        ledger_entry(
            instance,
            "aaaaaaaaaa",
            OTHER,
            at=datetime.datetime(2026, 8, 20, 10, 0, tzinfo=datetime.UTC),
        )
        before = instance.ledger_path.read_text()
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert instance.ledger_path.read_text() == before

    def test_a_promoted_child_survives_on_the_co_claimants_chain(self, instance):
        # Two items list one URL; the unit was seeded under the one about
        # to be purged, and `enrich fetch` promoted a child under it whose
        # line carries the shared unit's hash as `parent`. The child is in
        # no frontmatter — its only corpus answer travels the chain — and a
        # frontmatter-only veto purged it even though the chain resolves to
        # the co-claimant, a live item: work another live corpus item still
        # claims, deleted with its history.
        shared = work_identity(SHARED_URL, DRIVERS)
        write_item_stub(instance, ITEM, urls=(SHARED_URL,))
        write_item_stub(instance, OTHER, urls=(SHARED_URL,))
        ledger_entry(instance, shared, ITEM, url=SHARED_URL)
        ledger_entry(instance, "cccccccccc", ITEM, parent=shared)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert set(ledger.load(instance.ledger_path)) == {shared, "cccccccccc"}
        assert "0 ledger entries dropped" in summary
        assert "2 kept" in summary

    def test_a_child_whose_whole_chain_dies_with_the_item_is_purged(self, instance):
        # Only the excluded item lists the URL: the chain ends at work no
        # live item claims, so the child goes with its parent — there is
        # no survivor to keep either line for.
        url = "https://example.test/only-this-item"
        unit = work_identity(url, DRIVERS)
        write_item_stub(instance, ITEM, urls=(url,))
        ledger_entry(instance, unit, ITEM, url=url)
        ledger_entry(instance, "cccccccccc", ITEM, parent=unit)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert ledger.load(instance.ledger_path) == {}
        assert "2 ledger entries dropped, 0 kept" in summary

    def test_an_unreadable_line_is_kept_never_purged_by_accident(self, instance):
        # A purge must not become incidental data loss: a line this code
        # cannot parse is not provably the excluded item's.
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        with instance.ledger_path.open("a", encoding="utf-8") as f:
            f.write("{torn\n")
        run_exclude(instance, [{"id": ITEM}])
        assert instance.ledger_path.read_text() == "{torn\n"


class TestSequentialExclusions:
    """The purge judges the whole exclusions record, never the batch alone.

    A shared unit's line kept on a survivor's claim goes on naming the item
    it was seeded under — lines are never rewritten in place. The batch
    that later excludes the survivor holds no id matching that stored
    string, so judged against the batch alone the hash was never vetoed
    (nothing live claims it) and never purged (the batch does not name it):
    unpurgeable by any batch forever, refetched by the next run, and the
    report resurrected the excluded item under "Needs writing up".
    """

    def _seed_shared(self, instance) -> str:
        """Two items on one URL; the queued line names the first of them."""
        shared = work_identity(SHARED_URL, DRIVERS)
        write_item_stub(instance, ITEM, urls=(SHARED_URL,))
        write_item_stub(instance, OTHER, urls=(SHARED_URL,))
        ledger_entry(instance, shared, ITEM, url=SHARED_URL)
        return shared

    def _ctx(self, instance, driver) -> run_mod.RunContext:
        def refuse_gh(args: Sequence[str]) -> str:
            raise OSError(f"no gh in tests (called with {args[:2]})")

        return run_mod.RunContext(
            instance=instance,
            config=Config(),
            drivers=[driver],
            today=lambda: datetime.date(2026, 8, 22),
            now=lambda: datetime.datetime(2026, 8, 22, 12, 0, tzinfo=datetime.UTC),
            engine_version="0.4.0",
            transport=FakeTransport({}),
            sleep=lambda _seconds: None,
            gh=refuse_gh,
        )

    def test_excluding_the_survivor_sweeps_the_line_naming_the_first(self, instance):
        self._seed_shared(instance)
        first = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert "0 ledger entries dropped, 1 kept" in first
        second = run_exclude(instance, [{"id": OTHER, "reason": "meme"}])
        assert "1 ledger entries dropped, 0 kept" in second
        assert instance.ledger_path.read_text() == ""

    def test_the_next_run_refetches_nothing_and_resurrects_neither(self, instance):
        # Under the batch-only judgement the queued line survived both
        # exclusions, so the next run fetched it, recreated
        # enrichment/<first>/ and reported the excluded item as new
        # material.
        self._seed_shared(instance)
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        run_exclude(instance, [{"id": OTHER, "reason": "meme"}])
        driver = FakeDriver()
        report = run_mod.run(self._ctx(instance, driver))
        assert driver.fetched == []
        assert ITEM not in report
        assert OTHER not in report
        assert not (instance.enrichment_dir / ITEM).exists()

    def test_re_excluding_an_already_gone_id_sweeps_residue_idempotently(self, instance):
        # An instance that lived through the batch-only judgement holds the
        # orphan already: both ids on record, no corpus file left, the line
        # still naming the first. Re-running the last batch (its item long
        # gone) must sweep the residue, and a further re-run drop nothing.
        (instance.state_dir / "exclusions.tsv").write_text(
            f"{ITEM}\tmeme\n{OTHER}\tmeme\n", encoding="utf-8"
        )
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        summary = run_exclude(instance, [{"id": OTHER, "reason": "meme"}])
        assert "removed 0 items (1 already gone)" in summary
        assert "1 ledger entries dropped, 0 kept" in summary
        assert instance.ledger_path.read_text() == ""
        again = run_exclude(instance, [{"id": OTHER, "reason": "meme"}])
        assert "0 ledger entries dropped, 0 kept" in again

    def test_a_hash_a_live_item_claims_survives_the_record(self, instance):
        # No over-purging: the stored string is on the record, but OTHER
        # still lists the URL — an unrelated later batch keeps the line on
        # the live claim, and counts it kept.
        shared = self._seed_shared(instance)
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        third = "2026-08-19-third-33dd44"
        write_item_stub(instance, third)
        summary = run_exclude(instance, [{"id": third, "reason": "ads"}])
        assert "0 ledger entries dropped, 1 kept" in summary
        assert set(ledger.load(instance.ledger_path)) == {shared}


class TestStrandedLandings:
    """The kept line's product went with the item that produced it."""

    TODAY = datetime.date(2026, 8, 21)
    NOW = datetime.datetime(2026, 8, 21, 11, 0, tzinfo=datetime.UTC)

    def _shared_landing(self, instance) -> str:
        """Two items on one URL; the line names the one about to be purged."""
        shared = work_identity(SHARED_URL, DRIVERS)
        write_item_stub(instance, ITEM, urls=(SHARED_URL,))
        write_item_stub(instance, OTHER, urls=(SHARED_URL,))
        output = instance.enrichment_dir / ITEM / "web-73bd78.md"
        output.parent.mkdir(parents=True)
        output.write_text("the fetched page\n")
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=shared,
                url=SHARED_URL,
                item=ITEM,
                kind=Kind.WEB,
                status=Status.DONE,
                engine="0.2.1",
                date=datetime.date(2026, 8, 20),
                at=datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.UTC),
                path=f"enrichment/{ITEM}/web-73bd78.md",
                title="a page",
            ),
        )
        return shared

    def _exclude(self, instance) -> str:
        return run_exclude(
            instance,
            [{"id": ITEM, "reason": "meme thread"}],
            today=lambda: self.TODAY,
            now=lambda: self.NOW,
            version=lambda: "0.4.0",
        )

    def test_a_kept_landing_whose_output_was_deleted_goes_back_to_queued(self, instance):
        # The line survives on the survivor's claim, but its enrichment left
        # with the item that produced it — and seeding's already-a-unit
        # short-circuit means no run would ever fetch it again. The survivor
        # would then derive `enriched` off a unit with nothing on disk.
        shared = self._shared_landing(instance)
        summary = self._exclude(instance)
        entry = ledger.load(instance.ledger_path)[shared]
        assert entry.status is Status.QUEUED
        assert entry.item == OTHER  # the item that claims the work, not the purged one
        assert entry.path is None
        assert entry.title is None
        # The clause rides the purge summary; it must never replace it.
        assert "excluded 1" in summary
        assert "1 re-queued (enrichment went with the item that produced it)" in summary

    def test_the_re_queue_is_this_commands_verdict_and_stamps(self, instance):
        # "the output is gone, fetch it again" is a call THIS command made,
        # not one carried from the line it supersedes.
        shared = self._shared_landing(instance)
        self._exclude(instance)
        entry = ledger.load(instance.ledger_path)[shared]
        assert entry.engine == "0.4.0"
        assert entry.date == self.TODAY
        assert entry.at == self.NOW

    def test_a_stranded_promoted_child_goes_back_queued_for_the_survivor(self, instance):
        # The child's line survives on the co-claimant's chain, but its
        # output lived in enrichment/<purged>/ and went with the item that
        # produced it: back to queued, named for the survivor, lineage kept
        # so ownership still resolves through the chain.
        shared = self._shared_landing(instance)
        child_output = instance.enrichment_dir / ITEM / "web-cccccc.md"
        child_output.write_text("the promoted child page\n")
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash="cccccccccc",
                url="https://example.test/promoted-child",
                item=ITEM,
                kind=Kind.WEB,
                status=Status.DONE,
                engine="0.2.1",
                date=datetime.date(2026, 8, 20),
                at=datetime.datetime(2026, 8, 20, 9, 30, tzinfo=datetime.UTC),
                parent=shared,
                depth=1,
                path=f"enrichment/{ITEM}/web-cccccc.md",
                title="a child page",
            ),
        )
        summary = self._exclude(instance)
        child = ledger.load(instance.ledger_path)["cccccccccc"]
        assert child.status is Status.QUEUED
        assert child.item == OTHER
        assert child.parent == shared
        assert child.depth == 1
        assert child.path is None
        assert child.title is None
        assert "2 re-queued" in summary

    def test_a_landing_filed_elsewhere_is_left_alone(self, instance):
        # Only a product in the directory this purge deleted is stranded.
        # A landing under the survivor's own id is untouched, and re-queuing
        # it would spend a fetch on a file that is right there.
        shared = work_identity(SHARED_URL, DRIVERS)
        write_item_stub(instance, ITEM, urls=(SHARED_URL,))
        write_item_stub(instance, OTHER, urls=(SHARED_URL,))
        output = instance.enrichment_dir / OTHER / "web-73bd78.md"
        output.parent.mkdir(parents=True)
        output.write_text("the fetched page\n")
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=shared,
                url=SHARED_URL,
                item=ITEM,
                kind=Kind.WEB,
                status=Status.DONE,
                engine="0.2.1",
                date=datetime.date(2026, 8, 20),
                path=f"enrichment/{OTHER}/web-73bd78.md",
            ),
        )
        summary = self._exclude(instance)
        assert ledger.load(instance.ledger_path)[shared].status is Status.DONE
        assert "re-queued" not in summary

    def test_a_purged_hash_is_dropped_rather_than_re_queued(self, instance):
        # Nothing claims it, so the line goes with the item — there is no
        # survivor to fetch it for.
        write_item_stub(instance, ITEM, urls=(SHARED_URL,))
        output = instance.enrichment_dir / ITEM / "web-73bd78.md"
        output.parent.mkdir(parents=True)
        output.write_text("the fetched page\n")
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_identity(SHARED_URL, DRIVERS),
                url=SHARED_URL,
                item=ITEM,
                kind=Kind.WEB,
                status=Status.DONE,
                engine="0.2.1",
                date=datetime.date(2026, 8, 20),
                path=f"enrichment/{ITEM}/web-73bd78.md",
            ),
        )
        summary = self._exclude(instance)
        assert ledger.load(instance.ledger_path) == {}
        assert "re-queued" not in summary


class TestUnionMergedRecord:
    """Two machines' appends union-merge; the readers tolerate the shape.

    git's union driver concatenates ours-then-theirs, so a merged
    ``state/exclusions.tsv`` holds the sides' rulings out of chronological
    order and an item both sides excluded twice, each with the reason its
    machine wrote. Every reader collapses the file to a set, which is what
    licenses the template's ``merge=union``.
    """

    MERGED = (
        f"{ITEM}\tout of scope\n"
        f"{OTHER}\tads\n"
        f"{ITEM}\tmeme thread\n"  # the other machine's copy of the ruling
        "2026-08-19-third-33dd44\tspam\n"
    )

    def test_re_excluding_a_twice_recorded_id_appends_no_third_copy(self, instance):
        (instance.state_dir / "exclusions.tsv").write_text(self.MERGED, encoding="utf-8")
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert (instance.state_dir / "exclusions.tsv").read_text() == self.MERGED

    def test_normalize_reads_every_ruling_once(self, instance):
        (instance.state_dir / "exclusions.tsv").write_text(self.MERGED, encoding="utf-8")
        assert load_exclusions(instance.state_dir / "exclusions.tsv") == {
            "55ad7b",
            "11ff22",
            "33dd44",
        }


class TestBadIdsAreRefused:
    """An id is a corpus item id, never a path — `exclude` deletes recursively."""

    def victim(self, tmp_path_factory) -> Path:
        outside = tmp_path_factory.mktemp("outside") / "VICTIM"
        outside.mkdir()
        (outside / "keepsake.txt").write_text("not this instance's to delete")
        return outside

    def test_an_absolute_path_id_is_refused_and_removes_nothing(self, instance, tmp_path_factory):
        # `Path / "/abs/path"` discards the left operand: the id would have
        # been rmtree'd where it points, and recorded verbatim in the TSV.
        victim = self.victim(tmp_path_factory)
        with pytest.raises(ValueError, match="not a corpus item id"):
            run_exclude(instance, [{"id": str(victim), "reason": "out of scope"}])
        assert (victim / "keepsake.txt").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()

    def test_a_traversal_id_is_refused_and_removes_nothing(self, instance, tmp_path_factory):
        victim = self.victim(tmp_path_factory)
        climb = os.path.relpath(victim, instance.enrichment_dir)
        with pytest.raises(ValueError, match="not a corpus item id"):
            run_exclude(instance, [{"id": climb, "reason": "out of scope"}])
        assert (victim / "keepsake.txt").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()

    def test_an_enrichment_symlink_out_of_the_instance_is_refused(self, instance, tmp_path_factory):
        # The id is a perfectly well-formed one; the directory it names is
        # not inside the instance. Resolving both sides is what sees that.
        victim = self.victim(tmp_path_factory)
        write_item_stub(instance)
        (instance.enrichment_dir / ITEM).symlink_to(victim, target_is_directory=True)
        with pytest.raises(ValueError, match="outside the instance"):
            run_exclude(instance, [{"id": ITEM, "reason": "out of scope"}])
        assert (victim / "keepsake.txt").exists()
        assert (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()

    def test_a_well_formed_id_still_removes_its_enrichment(self, instance):
        write_item_stub(instance)
        enrichment = instance.enrichment_dir / ITEM
        enrichment.mkdir(parents=True)
        (enrichment / "web-abc123.md").write_text("fetched")
        run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert not enrichment.exists()
        assert not (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()


class TestADuplicateIdInsideOneBatch:
    """The same id twice is one exclusion, and the counts say so."""

    def test_the_permanent_record_is_written_once(self, instance):
        # `state/exclusions.tsv` is a permanent record: the second copy of
        # an id appended an identical row because the existing-ids set was
        # read before the loop and never updated inside it.
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}, {"id": ITEM, "reason": "meme"}])
        lines = (instance.state_dir / "exclusions.tsv").read_text().splitlines()
        assert lines == [f"{ITEM}\tmeme"]

    def test_the_counts_tell_the_truth_about_what_was_gone(self, instance):
        # The summary is the batch's only signal, and it read "1 already
        # gone" for an item the same batch had just removed: the second
        # copy found the file the first copy had deleted.
        write_item_stub(instance)
        summary = run_exclude(instance, [{"id": ITEM}, {"id": ITEM}])
        assert summary == (
            "excluded 1 (1 duplicate id(s) collapsed): removed 1 items (0 already gone), "
            "0 digests, 0 media files, 0 pass records, 0 ledger entries dropped, 0 kept "
            "(work another live corpus item still claims); map and index recompiled"
        )

    def test_a_genuinely_absent_item_is_still_counted_gone(self, instance):
        # The other half: collapsing copies must not stop `already gone`
        # meaning what it says for an id whose file really is missing.
        write_item_stub(instance)
        summary = run_exclude(instance, [{"id": ITEM}, {"id": OTHER}, {"id": OTHER}])
        assert summary.startswith(
            "excluded 2 (1 duplicate id(s) collapsed): removed 1 items (1 already gone), "
        )


class TestTheCountsAddUp:
    """Every count is a tally across the batch, never the last item's alone."""

    def test_two_removed_items_and_their_digests(self, instance):
        for item_id in (ITEM, OTHER):
            write_item_stub(instance, item_id)
            instance.digests_dir.mkdir(parents=True, exist_ok=True)
            (instance.digests_dir / f"{item_id}.md").write_text("# facts\n", encoding="utf-8")
        summary = run_exclude(instance, [{"id": ITEM}, {"id": OTHER}])
        assert summary.startswith("excluded 2: removed 2 items (0 already gone), 2 digests, ")

    def test_two_items_already_gone(self, instance):
        summary = run_exclude(instance, [{"id": ITEM}, {"id": OTHER}])
        assert summary.startswith("excluded 2: removed 0 items (2 already gone), 0 digests, ")


class TestTheBatchIsRefusedWhole:
    """One bad entry refuses the batch: a half-applied one is worse than none."""

    def test_a_wrongly_typed_id_mid_batch_leaves_no_trace(self, instance):
        # Entry 1's TSV record was permanent while its ledger purge never
        # ran (that comes after the loop), and entry 3 was lost silently.
        write_item_stub(instance)
        write_item_stub(instance, OTHER)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        before = instance.ledger_path.read_text()
        with pytest.raises(ValueError, match="999"):
            run_exclude(
                instance,
                [
                    {"id": ITEM, "reason": "meme thread"},
                    {"id": 999, "reason": "meme thread"},
                    {"id": OTHER, "reason": "meme thread"},
                ],
            )
        assert (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert (instance.corpus_dir / "2026" / f"{OTHER}.md").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()
        assert instance.ledger_path.read_text() == before

    @pytest.mark.parametrize("reason", [None, 7, ["meme thread"]])
    def test_a_non_string_reason_refuses_the_batch(self, instance, reason):
        write_item_stub(instance)
        with pytest.raises(ValueError, match="must be a string"):
            run_exclude(instance, [{"id": ITEM, "reason": reason}])
        assert (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()

    def test_an_entry_that_is_not_an_object_is_refused(self, instance):
        # Unreachable through the CLI, which parses the file first, but
        # `run_exclude` is called directly too and deletes either way.
        with pytest.raises(ValueError, match="not an object"):
            run_exclude(instance, [ITEM])  # ty: ignore[invalid-argument-type]
        assert not (instance.state_dir / "exclusions.tsv").exists()


class TestUnreadableCorpusFiles:
    def test_an_unreadable_corpus_file_holds_the_purge_and_says_so(self, instance):
        # The claimed-hash veto is asked of the corpus, and a file that
        # will not parse claims nothing: the shared hash's whole history
        # would go, reported as a clean drop with no mention of the file.
        shared = work_identity(SHARED_URL, DRIVERS)
        write_item_stub(instance, ITEM, urls=(SHARED_URL,))
        write_item_stub(instance, OTHER, urls=(SHARED_URL,))
        (instance.corpus_dir / "2026" / f"{OTHER}.md").write_text("no frontmatter here\n")
        ledger_entry(instance, shared, ITEM, url=SHARED_URL)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert set(ledger.load(instance.ledger_path)) == {shared}
        assert "0 ledger entries dropped, 0 kept" in summary
        # Named, because nothing else names it: lint never reads a corpus
        # file's frontmatter whole.
        assert f"1 corpus file(s) could not be read (corpus/2026/{OTHER}.md)" in summary

    def test_a_readable_corpus_still_purges(self, instance):
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert "1 ledger entries dropped" in summary
        assert "could not be read" not in summary


PHOTO = "media/55ad7b/photo.jpg"
SCAN = "media/55ad7b/scan.png"
SIBLING_MEDIA = "media/11ff22/sibling.png"
# `dex inbox` keys a capture's directory by its file's name, so two captures
# of one `screenshot.png` land on, and both list, this one file.
SHARED_MEDIA = "media/9c1d3e/screenshot.png"


class TestMediaPurge:
    """The media a dead item carried goes with it, unless a survivor lists it."""

    def test_the_items_media_and_its_emptied_directory_go(self, instance):
        write_item_stub(instance, media=(PHOTO, SCAN))
        write_media(instance, PHOTO, SCAN)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert not (instance.root / "media" / "55ad7b").exists()
        assert (instance.root / "media").is_dir()
        assert "2 media files, " in summary

    def test_a_sibling_items_media_is_untouched(self, instance):
        write_item_stub(instance, media=(PHOTO,))
        write_item_stub(instance, OTHER, media=(SIBLING_MEDIA,))
        [photo, sibling] = write_media(instance, PHOTO, SIBLING_MEDIA)
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert not photo.exists()
        assert sibling.read_bytes() == b"\x89PNG not really"

    def test_a_file_another_live_item_lists_is_kept(self, instance):
        write_item_stub(instance, media=(SHARED_MEDIA,))
        write_item_stub(instance, OTHER, media=(SHARED_MEDIA,))
        [shared] = write_media(instance, SHARED_MEDIA)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert shared.exists()
        assert "0 media files (1 kept, listed by another live corpus item), " in summary

    def test_a_file_only_the_batch_lists_goes_once(self, instance):
        # Both claimants are excluded together, so nothing survives to keep it.
        write_item_stub(instance, media=(SHARED_MEDIA,))
        write_item_stub(instance, OTHER, media=(SHARED_MEDIA,))
        [shared] = write_media(instance, SHARED_MEDIA)
        summary = run_exclude(instance, [{"id": ITEM}, {"id": OTHER}])
        assert not shared.exists()
        assert "1 media files, " in summary

    def test_a_directory_still_holding_a_file_stays(self, instance):
        write_item_stub(instance, media=(PHOTO,))
        [photo, unlisted] = write_media(instance, PHOTO, SCAN)
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert not photo.exists()
        assert unlisted.exists()

    def test_a_kept_file_never_stops_the_items_other_media_going(self, instance):
        write_item_stub(instance, media=(SHARED_MEDIA, PHOTO))
        write_item_stub(instance, OTHER, media=(SHARED_MEDIA,))
        [shared, photo] = write_media(instance, SHARED_MEDIA, PHOTO)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert shared.exists()
        assert not photo.exists()
        assert "1 media files (1 kept, listed by another live corpus item), " in summary

    def test_a_file_already_gone_still_clears_its_empty_directory(self, instance):
        # What an interruption between the unlink and the rmdir leaves: the
        # re-run reads the same list and finishes the directory.
        write_item_stub(instance, media=(PHOTO,))
        (instance.root / "media" / "55ad7b").mkdir(parents=True)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert not (instance.root / "media" / "55ad7b").exists()
        assert "0 media files, " in summary

    def test_a_file_directly_under_media_never_takes_media_with_it(self, instance):
        write_item_stub(instance, media=("media/photo.jpg",))
        [photo] = write_media(instance, "media/photo.jpg")
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert not photo.exists()
        assert (instance.root / "media").is_dir()

    def test_a_stated_path_outside_media_is_never_deleted(self, instance, tmp_path_factory):
        # Frontmatter is owner-editable: a path that climbs out of media/, or
        # names another part of the instance, or media/ itself, is not media.
        outside = tmp_path_factory.mktemp("outside") / "keepsake.png"
        outside.write_bytes(b"not this instance's to delete")
        climb = os.path.relpath(outside, instance.root)
        write_item_stub(instance, media=(climb, "state/config.json", "media", "media/../media"))
        (instance.state_dir / "config.json").write_text("{}\n")
        (instance.root / "media").mkdir()
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert outside.exists()
        assert (instance.state_dir / "config.json").exists()
        assert (instance.root / "media").is_dir()
        assert "0 media files, " in summary

    def test_the_summary_states_every_count(self, instance):
        write_item_stub(instance, media=(PHOTO, SCAN))
        write_media(instance, PHOTO, SCAN)
        write_passes(instance, {"stage": "harvest", "item": ITEM, "date": "2026-08-20", "rules": 2})
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert summary == (
            "excluded 1: removed 1 items (0 already gone), 0 digests, 2 media files, 1 pass "
            "records, 0 ledger entries dropped, 0 kept (work another live corpus item still "
            "claims); map and index recompiled"
        )


class TestUnsettledMedia:
    """Only a readable corpus says which media a batch may delete."""

    def test_an_unreadable_survivor_refuses_a_batch_carrying_media(self, instance):
        # The survivor may list the file, and deferring to a re-run is no
        # answer: the batch's corpus file, the only list of its media, goes.
        write_item_stub(instance, media=(PHOTO,))
        write_item_stub(instance, OTHER)
        (instance.corpus_dir / "2026" / f"{OTHER}.md").write_text("no frontmatter here\n")
        [photo] = write_media(instance, PHOTO)
        with pytest.raises(ValueError, match="nothing was excluded") as excinfo:
            run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert f"corpus/2026/{OTHER}.md" in str(excinfo.value)
        assert photo.exists()
        assert (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()
        assert not instance.map_path.exists()

    def test_every_unreadable_file_is_read_past_and_named(self, instance):
        # The two unreadable survivors sort before the batch item, so the
        # scan has to read on past them to learn that the batch carries media.
        first, second = "2026-08-19-aaa-000001", "2026-08-19-bbb-000002"
        for broken in (first, second):
            write_item_stub(instance, broken)
            (instance.corpus_dir / "2026" / f"{broken}.md").write_text("no frontmatter\n")
        write_item_stub(instance, media=(PHOTO,))
        write_media(instance, PHOTO)
        with pytest.raises(ValueError, match="nothing was excluded") as excinfo:
            run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert f"(corpus/2026/{first}.md, corpus/2026/{second}.md)" in str(excinfo.value)

    def test_the_batch_items_own_unreadable_file_refuses_the_batch(self, instance):
        # Its media cannot be read off it, and nothing else would ever
        # find that media again once the file is gone.
        write_item_stub(instance)
        (instance.corpus_dir / "2026" / f"{ITEM}.md").write_bytes(b"\xff\xfe not text")
        with pytest.raises(ValueError, match=f"corpus/2026/{ITEM}.md"):
            run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()

    def test_an_unreadable_survivor_leaves_a_batch_without_media_to_the_ledger_veto(self, instance):
        write_item_stub(instance)
        write_item_stub(instance, OTHER)
        (instance.corpus_dir / "2026" / f"{OTHER}.md").write_text("no frontmatter here\n")
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert not (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert summary.startswith("excluded 1: removed 1 items")


def write_passes(instance: Instance, *records: dict[str, object] | str) -> None:
    """``state/passes.jsonl`` as given: a dict is a record, a string a raw line."""
    lines = [record if isinstance(record, str) else json.dumps(record) for record in records]
    instance.passes_path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def pass_items(instance: Instance) -> list[str]:
    return [json.loads(line)["item"] for line in instance.passes_path.read_text().splitlines()]


class TestPassPurge:
    """A dead item's pass records go; every other line stays as it was."""

    def test_the_items_records_go_and_anothers_stay(self, instance):
        write_item_stub(instance)
        write_item_stub(instance, OTHER)
        write_passes(
            instance,
            {"stage": "harvest", "item": ITEM, "date": "2026-08-20", "rules": 2},
            {"stage": "harvest", "item": OTHER, "date": "2026-08-20", "rules": 2},
            {"stage": "digest", "item": ITEM, "date": "2026-08-21"},
        )
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert pass_items(instance) == [OTHER]
        assert "2 pass records, " in summary

    def test_a_record_from_before_a_rename_goes_with_the_item(self, instance):
        # A rename keeps the shortid, and the pass readers resolve the old
        # id by it: with the item gone, nothing live carries it.
        write_item_stub(instance)
        write_passes(instance, {"stage": "harvest", "item": "2026-08-01-old-slug-55ad7b"})
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert pass_items(instance) == []

    def test_a_shortid_a_live_item_carries_keeps_its_old_records(self, instance):
        # Two captures of one file name share a shortid. The excluded
        # item's own record goes by its id; a dead id the survivor still
        # answers for, by shortid, is the survivor's history.
        survivor = "2026-09-01-sibling-55ad7b"
        write_item_stub(instance)
        write_item_stub(instance, survivor)
        write_passes(
            instance,
            {"stage": "harvest", "item": ITEM},
            {"stage": "harvest", "item": "2026-07-01-sibling-old-55ad7b"},
            {"stage": "digest", "item": survivor},
        )
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert pass_items(instance) == ["2026-07-01-sibling-old-55ad7b", survivor]

    def test_a_live_id_on_the_record_keeps_its_records(self, instance):
        # Re-created after an old exclusion: the id is on the record, and
        # it is live again, so its records are its own.
        write_item_stub(instance)
        write_item_stub(instance, OTHER)
        (instance.state_dir / "exclusions.tsv").write_text(f"{ITEM}\told ruling\n")
        write_passes(instance, {"stage": "harvest", "item": ITEM})
        run_exclude(instance, [{"id": OTHER, "reason": "meme"}])
        assert pass_items(instance) == [ITEM]

    def test_an_earlier_exclusions_records_are_swept_too(self, instance):
        # Judged against the whole record, like the ledger purge: records an
        # exclusion before this purge existed left behind go with any batch.
        write_item_stub(instance)
        (instance.state_dir / "exclusions.tsv").write_text(f"{OTHER}\tspam\n")
        write_passes(instance, {"stage": "harvest", "item": OTHER})
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert pass_items(instance) == []
        assert "1 pass records, " in summary

    def test_lines_that_name_no_item_are_kept_verbatim(self, instance):
        torn = '{"stage": "harvest", "item": "2026-08-19-exa'
        write_item_stub(instance)
        write_passes(
            instance,
            {"stage": "harvest", "item": ITEM},
            torn,
            "[1, 2]",
            {"stage": "harvest"},
            {"stage": "harvest", "item": 7},
        )
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert instance.passes_path.read_text().splitlines() == [
            torn,
            "[1, 2]",
            '{"stage": "harvest"}',
            '{"stage": "harvest", "item": 7}',
        ]

    def test_a_file_with_nothing_to_drop_is_not_rewritten(self, instance):
        write_item_stub(instance)
        write_item_stub(instance, OTHER)
        write_passes(instance, {"stage": "harvest", "item": OTHER})
        before = instance.passes_path.stat().st_ino
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert instance.passes_path.stat().st_ino == before
        assert "0 pass records, " in summary

    def test_an_id_with_no_shortid_to_split_off_is_judged_whole(self, instance):
        # An id is any single path component, so one with no dash is legal;
        # its trailing "shortid" is the whole id, never an index error.
        write_passes(
            instance, {"stage": "harvest", "item": "loose"}, {"stage": "harvest", "item": "kept"}
        )
        summary = run_exclude(instance, [{"id": "loose", "reason": "meme"}])
        assert pass_items(instance) == ["kept"]
        assert "1 pass records, " in summary

    def test_a_missing_passes_file_is_not_an_error(self, instance):
        write_item_stub(instance)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert "0 pass records, " in summary
        assert not instance.passes_path.exists()


@pytest.mark.usefixtures("no_directives_pending")
class TestCli:
    def test_main_excludes_from_a_file(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        write_item_stub(instance)
        payload = instance.root / "exclusions.json"
        payload.write_text(json.dumps([{"id": ITEM, "reason": "meme"}]))
        main([str(payload)])
        assert "excluded 1: removed 1 items" in capsys.readouterr().out

    def test_main_hands_the_entries_to_the_instance_at_cwd_and_prints_the_summary(
        self, instance, monkeypatch, capsys
    ):
        given: list[tuple[Instance, list[dict[str, object]]]] = []

        def excluded(inst: Instance, entries: list[dict[str, object]]) -> str:
            given.append((inst, entries))
            return "the summary"

        monkeypatch.setattr("dex_engine.exclude.run_exclude", excluded)
        monkeypatch.chdir(instance.root)
        payload = instance.root / "exclusions.json"
        payload.write_text(json.dumps([{"id": ITEM, "reason": "meme"}]))
        main([str(payload)])
        assert capsys.readouterr().out == "the summary\n"
        [(inst, entries)] = given
        assert (inst.root, entries) == (instance.root, [{"id": ITEM, "reason": "meme"}])

    def test_missing_file_is_loud(self, instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        with pytest.raises(SystemExit) as excinfo:
            main(["absent.json"])
        assert "dex-exclude" in str(excinfo.value)

    def test_non_list_payload_is_loud(self, instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        payload = instance.root / "exclusions.json"
        payload.write_text('{"id": "x"}')
        with pytest.raises(SystemExit) as excinfo:
            main([str(payload)])
        assert "JSON list" in str(excinfo.value)

    def test_file_argument_is_required(self):
        with pytest.raises(SystemExit):
            main([])


class TestMapRecompile:
    """The purge changes the map's inputs, so a successful one recompiles it."""

    def _taxonomy(self, instance, items):
        instance.taxonomy_path.write_text(
            json.dumps({"topics": {"brewing": {"description": "d", "items": items}}})
        )

    def test_a_purge_leaves_a_fresh_map(self, instance):
        write_item_stub(instance)
        self._taxonomy(instance, [ITEM])
        instance.map_path.write_text("stale")
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert summary.endswith("; map and index recompiled")
        assert instance.map_path.read_text(encoding="utf-8") == (
            serialize_map(compile_map(instance).payload)
        )

    def test_every_summary_path_recompiles(self, instance):
        # The unreadable-corpus early exit still deleted the item, so the
        # map's inputs changed on that path too.
        write_item_stub(instance)
        write_item_stub(instance, OTHER)
        (instance.corpus_dir / OTHER[:4] / f"{OTHER}.md").write_text("no frontmatter")
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert "could not be read" in summary
        assert summary.endswith("; map and index recompiled")
        assert instance.map_path.exists()

    def test_a_failed_recompile_reports_but_the_purge_stands(self, instance):
        # A malformed taxonomy is a real trigger here: the purge never reads
        # the file, so nothing refused earlier — and the deletions that
        # landed must not be un-reported by the compile refusing.
        write_item_stub(instance)
        instance.taxonomy_path.write_text("{not json")
        with pytest.raises(ValueError, match="did not recompile") as excinfo:
            run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert "excluded 1: removed 1 items" in str(excinfo.value)
        assert not (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert f"{ITEM}\tmeme\n" in (instance.state_dir / "exclusions.tsv").read_text()
        assert not instance.map_path.exists()

    def test_a_refused_batch_compiles_nothing(self, instance):
        with pytest.raises(ValueError, match="no id"):
            run_exclude(instance, [{"reason": "meme"}])
        assert not instance.map_path.exists()
