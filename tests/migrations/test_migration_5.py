"""Tests for migration 5: drop superseded outputs the closed-name drop missed."""

import datetime

import pytest

from dex_engine.migrations.migration_5 import build
from dex_engine.pipeline.ledger import to_line
from dex_engine.pipeline.types import Job, Kind, LedgerEntry, Status
from dex_engine.pipeline.urls import work_hash

TODAY = datetime.date(2026, 8, 28)
NOW = datetime.datetime(2026, 8, 28, 9, 0, 0, 500000, tzinfo=datetime.UTC)

ITEM = "2026-08-27-example-55ad7b"
URL = "https://example.test/post"
UNIT_HASH = work_hash(URL)
KEPT = f"enrichment/{ITEM}/web-{UNIT_HASH[:6]}.md"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.0")


def write_file(root, name, *, url=URL, body="the view\n", item=ITEM):
    path = root / "enrichment" / item / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nurl: "{url}"\nfetched: 2026-08-27\n---\n\n{body}')
    return path


def write_ledger(root, *entries):
    path = root / "state" / "enrichment-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(to_line(e) + "\n" for e in entries))


def done_entry(*, url=URL, path=KEPT, job=None, status=Status.DONE, at=NOW):
    return LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=ITEM,
        kind=Kind.WEB,
        status=status,
        engine="0.1.5",
        date=TODAY,
        at=at,
        job=job,
        path=path,
    )


class TestApply:
    def test_a_hand_named_twin_leaves_and_the_kept_file_stands(self, tmp_path, migration):
        # The field defect: a unit healed by hand pre-rename kept its file
        # when the migration-seeded rerun landed the engine-named one
        # beside it, and the item listed one unit twice.
        kept = write_file(tmp_path, f"web-{UNIT_HASH[:6]}.md")
        stale = write_file(tmp_path, "healed-by-hand.md", body="the old view\n")
        write_ledger(tmp_path, done_entry())
        report = migration.apply(tmp_path)
        assert not stale.exists()
        assert kept.exists()
        assert len(report.actions) == 1
        assert "healed-by-hand.md" in report.actions[0]
        assert KEPT in report.actions[0]

    def test_a_neighbours_file_stays_whatever_it_is_named(self, tmp_path, migration):
        write_file(tmp_path, f"web-{UNIT_HASH[:6]}.md")
        neighbour = write_file(tmp_path, "web-aaaaaa.md", url="https://elsewhere.test/page")
        write_ledger(tmp_path, done_entry())
        report = migration.apply(tmp_path)
        assert neighbour.exists()
        assert report.actions == []

    def test_an_unreadable_candidate_stays_and_is_named(self, tmp_path, migration):
        write_file(tmp_path, f"web-{UNIT_HASH[:6]}.md")
        garbled = tmp_path / "enrichment" / ITEM / "garbled.md"
        garbled.write_bytes(b"\xff\xfe broken bytes")
        write_ledger(tmp_path, done_entry())
        report = migration.apply(tmp_path)
        assert garbled.exists()
        assert any("garbled.md" in s.what for s in report.skipped)

    def test_a_missing_kept_file_anchors_nothing(self, tmp_path, migration):
        # With no kept file there is no "superseded": which orphaned view
        # is current is session judgment, not mechanics.
        orphan = write_file(tmp_path, "healed-by-hand.md")
        write_ledger(tmp_path, done_entry())  # path names a file not on disk
        report = migration.apply(tmp_path)
        assert orphan.exists()
        assert report.actions == []

    def test_only_done_page_units_anchor(self, tmp_path, migration):
        write_file(tmp_path, f"web-{UNIT_HASH[:6]}.md")
        stale = write_file(tmp_path, "healed-by-hand.md")
        write_ledger(tmp_path, done_entry(job=Job.MEDIA))
        migration.apply(tmp_path)
        assert stale.exists()

    def test_second_apply_is_a_clean_noop(self, tmp_path, migration):
        write_file(tmp_path, f"web-{UNIT_HASH[:6]}.md")
        write_file(tmp_path, "healed-by-hand.md")
        write_ledger(tmp_path, done_entry())
        migration.apply(tmp_path)
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []

    def test_non_anchor_entries_never_stop_the_sweep(self, tmp_path, migration):
        # Every skip is per-entry, per-candidate: a job unit, a done line
        # with no path, a done line whose file left the disk, and an
        # unreadable candidate all come BEFORE the entry and twin that
        # need repairing, and the repair still lands.
        kept = write_file(tmp_path, f"web-{UNIT_HASH[:6]}.md")
        garbled = tmp_path / "enrichment" / ITEM / "a-garbled.md"
        garbled.write_bytes(b"\xff\xfe broken bytes")
        twin = write_file(tmp_path, "z-healed-by-hand.md", body="the old view\n")
        write_ledger(
            tmp_path,
            done_entry(url="https://media.test/a.png", job=Job.MEDIA, path=None),
            done_entry(url="https://example.test/no-file-written", path=None),
            done_entry(url="https://example.test/file-gone", path="enrichment/x/gone.md"),
            done_entry(),
        )
        report = migration.apply(tmp_path)
        assert not twin.exists()  # the sweep reached past every skip
        assert garbled.exists()
        assert kept.exists()
        assert len(report.actions) == 1

    def test_the_hashes_live_line_decides_not_the_files_last_line(self, tmp_path, migration):
        # The ledger resolves by write instant, not file order: a stale
        # queued line union-merged BELOW the done line must not demote the
        # unit, or its twin survives the sweep.
        kept = write_file(tmp_path, f"web-{UNIT_HASH[:6]}.md")
        twin = write_file(tmp_path, "healed-by-hand.md")
        earlier = NOW - datetime.timedelta(hours=1)
        write_ledger(
            tmp_path,
            done_entry(at=NOW),
            done_entry(status=Status.QUEUED, path=None, at=earlier),
        )
        migration.apply(tmp_path)
        assert not twin.exists()
        assert kept.exists()

    def test_a_leading_blank_ledger_line_hides_nothing(self, tmp_path, migration):
        # Union merges leave blank lines; everything after one must still
        # be read.
        write_file(tmp_path, f"web-{UNIT_HASH[:6]}.md")
        twin = write_file(tmp_path, "healed-by-hand.md")
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n" + to_line(done_entry()) + "\n")
        migration.apply(tmp_path)
        assert not twin.exists()

    def test_a_missing_ledger_is_an_empty_report(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []

    def test_an_unparseable_ledger_line_is_skipped_not_fatal(self, tmp_path, migration):
        kept = write_file(tmp_path, f"web-{UNIT_HASH[:6]}.md")
        stale = write_file(tmp_path, "healed-by-hand.md")
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json\n" + to_line(done_entry()) + "\n")
        report = migration.apply(tmp_path)
        assert not stale.exists()  # the readable line still repaired
        assert kept.exists()
        assert any("ledger line 1" in s.what for s in report.skipped)
