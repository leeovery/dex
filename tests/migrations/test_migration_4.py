"""Migration 4: sweeping ledger entries whose corpus item no longer exists."""

import dataclasses
import datetime

import pytest

from dex_engine.migrations import discover, run_pending
from dex_engine.migrations.migration_4 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline.types import Kind, LedgerEntry, Status

TODAY = datetime.date(2026, 8, 20)
NOW = datetime.datetime(2026, 8, 20, 8, 0, 0, 500000, tzinfo=datetime.UTC)
ENGINE = "0.5.0"

LIVE = "2026-05-01-live-a1b2c3"
PURGED = "2026-05-02-purged-d4e5f6"
VANISHED = "2026-05-03-vanished-99aabb"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version=ENGINE)


def write_item(root, item_id):
    path = root / "corpus" / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stub")


def entry(unit_hash: str, item_id: str, **overrides: object) -> LedgerEntry:
    base = LedgerEntry(
        hash=unit_hash,
        url=f"https://a.test/{unit_hash}",
        item=item_id,
        kind=Kind.WEB,
        status=Status.DONE,
        path=f"enrichment/{item_id}/web-{unit_hash[:6]}.md",
        engine="0.4.0",
        date=TODAY,
        at=NOW,
    )
    return dataclasses.replace(base, **overrides)


def write_ledger(root, entries):
    path = root / "state" / "enrichment-ledger.jsonl"
    for e in entries:
        ledger.append(path, e)
    return path


class TestSweep:
    def test_a_ghost_items_entries_go_and_the_rest_stay(self, tmp_path, migration):
        write_item(tmp_path, LIVE)
        path = write_ledger(
            tmp_path,
            [entry("aaaaaaaaaa", LIVE), entry("bbbbbbbbbb", PURGED)],
        )
        report = migration.apply(tmp_path)
        assert set(ledger.load(path)) == {"aaaaaaaaaa"}
        summary = next(a for a in report.actions if "swept" in a and "corpus item" in a)
        assert "1 entry swept" in summary
        assert "git history" in summary
        assert report.skipped == []

    def test_the_whole_hashs_history_goes_with_it(self, tmp_path, migration):
        # Superseded lines of a swept hash are its audit trail; leaving them
        # would keep the ghost visible to every raw grep of the file.
        path = write_ledger(
            tmp_path,
            [
                entry("bbbbbbbbbb", PURGED, status=Status.QUEUED, path=None),
                entry("bbbbbbbbbb", PURGED),
            ],
        )
        migration.apply(tmp_path)
        assert path.read_text() == ""

    def test_an_exclusions_record_is_named_in_the_report(self, tmp_path, migration):
        write_ledger(tmp_path, [entry("bbbbbbbbbb", PURGED)])
        (tmp_path / "state" / "exclusions.tsv").write_text(f"{PURGED}\tout of scope: memes\n")
        report = migration.apply(tmp_path)
        named = next(a for a in report.actions if a.startswith("ledger swept "))
        assert "bbbbbbbbbb" in named
        assert "out of scope: memes" in named
        assert "1 excluded on the record, 0 removed by hand" in report.actions[0]

    def test_an_item_removed_by_hand_is_told_apart(self, tmp_path, migration):
        write_ledger(tmp_path, [entry("cccccccccc", VANISHED)])
        report = migration.apply(tmp_path)
        assert "0 excluded on the record, 1 removed by hand" in report.actions[0]
        assert any("removed by hand or a purge was interrupted" in a for a in report.actions)

    def test_a_missing_output_file_is_never_grounds_for_sweeping(self, tmp_path, migration):
        # A different finding with a different repair: the item exists, the
        # work still belongs to it, and the fix is to re-fetch, not to forget.
        write_item(tmp_path, LIVE)
        path = write_ledger(tmp_path, [entry("aaaaaaaaaa", LIVE)])
        assert not (tmp_path / "enrichment" / LIVE).exists()
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert set(ledger.load(path)) == {"aaaaaaaaaa"}

    def test_a_hash_whose_live_line_moved_to_a_real_item_is_kept_whole(self, tmp_path, migration):
        # The latest line decides. An older line naming a purged item is a
        # re-parenting's history, and retires at compact like any other.
        write_item(tmp_path, LIVE)
        path = write_ledger(
            tmp_path,
            [
                entry("aaaaaaaaaa", PURGED, at=NOW - datetime.timedelta(hours=1)),
                entry("aaaaaaaaaa", LIVE),
            ],
        )
        before = path.read_text()
        migration.apply(tmp_path)
        assert path.read_text() == before

    def test_the_live_line_is_chosen_by_write_time_not_file_order(self, tmp_path, migration):
        # A union merge can put the older line last; the sweep must judge the
        # entry the loader resolves to, not whichever line git appended.
        write_item(tmp_path, LIVE)
        path = write_ledger(
            tmp_path,
            [
                entry("aaaaaaaaaa", LIVE),
                entry("aaaaaaaaaa", PURGED, at=NOW - datetime.timedelta(hours=1)),
            ],
        )
        before = path.read_text()
        migration.apply(tmp_path)
        assert path.read_text() == before

    def test_the_named_list_is_capped_with_the_remainder_counted(self, tmp_path, migration):
        write_ledger(tmp_path, [entry(f"{index:010d}", PURGED) for index in range(9)])
        report = migration.apply(tmp_path)
        assert "9 entries swept" in report.actions[0]
        assert len([a for a in report.actions if a.startswith("ledger swept ")]) == 5
        assert any("and 4 further swept entries" in a for a in report.actions)

    def test_an_unreadable_line_is_kept_and_skipped_with_why(self, tmp_path, migration):
        # A sweep must never remove what it cannot read.
        path = write_ledger(tmp_path, [entry("bbbbbbbbbb", PURGED)])
        with path.open("a", encoding="utf-8") as f:
            f.write("{torn\n")
        report = migration.apply(tmp_path)
        assert path.read_text() == "{torn\n"
        assert any("unreadable under the current schema" in s.why for s in report.skipped)


class TestIdempotency:
    def test_second_apply_is_a_clean_noop(self, tmp_path, migration):
        write_item(tmp_path, LIVE)
        path = write_ledger(
            tmp_path,
            [entry("aaaaaaaaaa", LIVE), entry("bbbbbbbbbb", PURGED)],
        )
        migration.apply(tmp_path)
        after = path.read_text()
        second = migration.apply(tmp_path)
        assert second.actions == []
        assert second.skipped == []
        assert path.read_text() == after

    def test_missing_ledger_is_tolerated(self, tmp_path, migration):
        # Un-pulled repos are a supported input.
        report = migration.apply(tmp_path)
        assert (report.actions, report.skipped) == ([], [])


class TestFramework:
    def test_discovered_as_number_four(self):
        migrations = discover(today=lambda: TODAY, now=lambda: NOW, engine_version=ENGINE)
        assert [m.number for m in migrations] == [1, 2, 3, 4]

    def test_runs_after_the_reseed_so_it_sweeps_what_migration_2_declined(self, tmp_path):
        # Order matters: migration 2 explains why it did not reseed a purged
        # item, then migration 4 removes the entry it declined to seed.
        # Pre-rewrite engine: the exact cohort migration 2 would reseed.
        write_ledger(tmp_path, [entry("bbbbbbbbbb", PURGED, engine="0.0.1")])
        applied = run_pending(
            tmp_path, today=lambda: TODAY, now=lambda: NOW, engine_version=ENGINE
        )
        assert [a.number for a in applied] == [1, 2, 3, 4]
        declined = [s for s in applied[1].report.skipped if PURGED in s.what]
        assert declined  # migration 2 refused to reseed it
        assert any("1 entry swept" in a for a in applied[3].report.actions)
        assert ledger.load(tmp_path / "state" / "enrichment-ledger.jsonl") == {}
