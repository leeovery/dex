"""Tests for migration 6: split pass records holding a pasted list of ids."""

import datetime
import json

import pytest

from dex_engine.migrations.migration_6 import build

TODAY = datetime.date(2026, 8, 28)
NOW = datetime.datetime(2026, 8, 28, 9, 0, 0, 500000, tzinfo=datetime.UTC)

FIRST = "2026-08-27-first-aaaaaa"
SECOND = "2026-08-27-second-bbbbbb"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.0")


def write_item(root, item):
    path = root / "corpus" / item[:4] / f"{item}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nid: {item}\n---\n")


def passes_path(root):
    path = root / "state" / "passes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def records(root):
    return [
        json.loads(line)
        for line in passes_path(root).read_text().split("\n")
        if line.strip() and not line.startswith("{not")
    ]


class TestApply:
    def test_a_blob_splits_into_per_item_records(self, tmp_path, migration):
        # The field defect: 23 ids pasted as one argument landed as one
        # record whose item field holds the whole blob — claimable by
        # nothing, while the session read the stage as covered.
        write_item(tmp_path, FIRST)
        write_item(tmp_path, SECOND)
        blob = {"stage": "digest", "item": f"{FIRST}\n{SECOND}", "date": "2026-08-27"}
        passes_path(tmp_path).write_text(json.dumps(blob) + "\n")
        report = migration.apply(tmp_path)
        assert records(tmp_path) == [
            {"stage": "digest", "item": FIRST, "date": "2026-08-27"},
            {"stage": "digest", "item": SECOND, "date": "2026-08-27"},
        ]
        assert len(report.actions) == 1
        assert "2-id" in report.actions[0]
        assert "into 2 per-item" in report.actions[0]

    def test_tokens_no_live_item_claims_are_dropped_and_named(self, tmp_path, migration):
        # The ghost leads the blob: a dropped token must not stop the live
        # tokens behind it from being re-emitted.
        write_item(tmp_path, FIRST)
        blob = {"stage": "digest", "item": f"2026-08-27-ghost-ffffff\n{FIRST}", "date": "x"}
        passes_path(tmp_path).write_text(json.dumps(blob) + "\n")
        report = migration.apply(tmp_path)
        assert [r["item"] for r in records(tmp_path)] == [FIRST]
        assert any("ghost" in s.what for s in report.skipped)
        assert any("re-record" in s.why for s in report.skipped)

    def test_a_renamed_items_old_id_still_claims_by_shortid(self, tmp_path, migration):
        # The token is written verbatim — it is what the verb should have
        # recorded that day, and every pass reader resolves it by shortid.
        write_item(tmp_path, "2026-08-27-new-slug-aaaaaa")
        blob = {"stage": "harvest", "item": f"{FIRST}\n{SECOND}", "date": "x", "rules": 1}
        write_item(tmp_path, SECOND)
        passes_path(tmp_path).write_text(json.dumps(blob) + "\n")
        report = migration.apply(tmp_path)
        assert [r["item"] for r in records(tmp_path)] == [FIRST, SECOND]
        assert all(r["rules"] == 1 for r in records(tmp_path))  # fields carried
        assert report.skipped == []

    def test_clean_rows_are_carried_byte_for_byte(self, tmp_path, migration):
        write_item(tmp_path, FIRST)
        write_item(tmp_path, SECOND)
        clean = json.dumps({"stage": "digest", "item": FIRST, "date": "2026-08-20"})
        torn = "{not json at all"
        blob = json.dumps({"stage": "digest", "item": f"{FIRST}\n{SECOND}", "date": "x"})
        passes_path(tmp_path).write_text(f"{clean}\n{torn}\n{blob}\n")
        migration.apply(tmp_path)
        text = passes_path(tmp_path).read_text()
        lines = text.split("\n")
        assert lines[0] == clean  # untouched, byte-identical
        assert lines[1] == torn  # a line the sweep cannot read is not one it may destroy
        assert json.loads(lines[2])["item"] == FIRST  # the blob behind them still split
        assert json.loads(lines[3])["item"] == SECOND
        assert text.endswith("\n")

    def test_a_file_with_no_blobs_is_not_rewritten(self, tmp_path, migration):
        write_item(tmp_path, FIRST)
        clean = json.dumps({"stage": "digest", "item": FIRST, "date": "2026-08-20"}) + "\n"
        path = passes_path(tmp_path)
        path.write_text(clean)
        before = path.stat().st_mtime_ns
        report = migration.apply(tmp_path)
        assert path.stat().st_mtime_ns == before
        assert report.actions == []

    def test_second_apply_is_a_clean_noop(self, tmp_path, migration):
        write_item(tmp_path, FIRST)
        blob = {"stage": "digest", "item": f"{FIRST}\n{SECOND}", "date": "x"}
        passes_path(tmp_path).write_text(json.dumps(blob) + "\n")
        migration.apply(tmp_path)
        snapshot = passes_path(tmp_path).read_bytes()
        report = migration.apply(tmp_path)
        assert passes_path(tmp_path).read_bytes() == snapshot
        assert report.actions == []

    def test_a_missing_passes_file_is_an_empty_report(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []
