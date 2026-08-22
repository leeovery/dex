"""Migration 5: typing the ledger's cap fires with the bound that refused."""

import datetime
import json

import pytest

from dex_engine.migrations.migration_5 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline.types import Cap, Kind, LedgerEntry, Status

TODAY = datetime.date(2026, 8, 20)
NOW = datetime.datetime(2026, 8, 20, 8, 0, 0, 500000, tzinfo=datetime.UTC)
ENGINE = "0.5.0"
ITEM = "2026-05-01-item-a1b2c3"

DEPTH_REASON = "depth cap (4) reached"
URL_REASON = "url cap (12 per item) reached"
REQUESTED_REASON = "url cap (12 per item) reached — rerun with --force to exceed"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version=ENGINE)


def pre_typing_line(unit_hash: str, reason: str, **extra: object) -> str:
    """A cap-fire line as the engine wrote it before the bound was typed."""
    record = {
        "hash": unit_hash,
        "url": f"https://a.test/{unit_hash}",
        "item": ITEM,
        "kind": "web",
        "status": "skipped",
        "capped": True,
        "engine": "0.4.0",
        "date": TODAY.isoformat(),
        "via": "harvest",
        "parent": "0000000000",
        "depth": 5,
        "reason": reason,
    }
    return json.dumps(record | extra, ensure_ascii=False)


def write_lines(root, lines):
    path = root / "state" / "enrichment-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


class TestTyping:
    def test_each_bound_is_typed_from_the_wording_that_named_it(self, tmp_path, migration):
        path = write_lines(
            tmp_path,
            [
                pre_typing_line("aaaaaaaaaa", DEPTH_REASON),
                pre_typing_line("bbbbbbbbbb", URL_REASON),
                pre_typing_line("cccccccccc", REQUESTED_REASON),
            ],
        )
        report = migration.apply(tmp_path)
        entries = ledger.load(path)  # the file conforms to the current schema now
        assert entries["aaaaaaaaaa"].cap is Cap.DEPTH
        assert entries["bbbbbbbbbb"].cap is Cap.URL
        assert entries["cccccccccc"].cap is Cap.URL_REQUESTED
        assert "capped" not in path.read_text()
        summary = next(a for a in report.actions if "cap fires typed" in a)
        assert "depth: 1, url: 1, url-requested: 1" in summary
        assert report.skipped == []

    def test_the_stated_reason_is_kept_as_the_audit_trail(self, tmp_path, migration):
        path = write_lines(tmp_path, [pre_typing_line("aaaaaaaaaa", REQUESTED_REASON)])
        migration.apply(tmp_path)
        assert ledger.load(path)["aaaaaaaaaa"].reason == REQUESTED_REASON

    def test_a_fire_naming_no_known_bound_is_left_alone_and_named(self, tmp_path, migration):
        # Typing it with a guess would put a made-up reading in front of the
        # owner; the line stays as it is and says so.
        original = pre_typing_line("aaaaaaaaaa", "twelve fetches is plenty for one item")
        path = write_lines(tmp_path, [original])
        report = migration.apply(tmp_path)
        assert path.read_text() == original + "\n"
        assert len(report.skipped) == 1
        assert "names no bound" in report.skipped[0].why

    def test_lines_that_never_fired_a_cap_survive_byte_for_byte(self, tmp_path, migration):
        done = ledger.to_line(
            LedgerEntry(
                hash="dddddddddd",
                url="https://a.test/done",
                item=ITEM,
                kind=Kind.WEB,
                status=Status.DONE,
                path=f"enrichment/{ITEM}/web-dddddd.md",
                engine="0.4.0",
                date=TODAY,
                at=NOW,
            )
        )
        fire = pre_typing_line("aaaaaaaaaa", URL_REASON)
        path = write_lines(tmp_path, [done, "{ not json", fire])
        migration.apply(tmp_path)
        lines = path.read_text().split("\n")
        assert lines[0] == done
        assert lines[1] == "{ not json"

    def test_an_explicit_capped_false_is_dropped(self, tmp_path, migration):
        line = json.loads(pre_typing_line("aaaaaaaaaa", "paywalled"))
        line["capped"] = False
        path = write_lines(tmp_path, [json.dumps(line)])
        report = migration.apply(tmp_path)
        assert "capped" not in path.read_text()
        assert ledger.load(path)["aaaaaaaaaa"].cap is None
        assert report.skipped == []


class TestIdempotencyAndEdges:
    def test_a_second_apply_finds_nothing_to_do(self, tmp_path, migration):
        path = write_lines(tmp_path, [pre_typing_line("aaaaaaaaaa", URL_REASON)])
        migration.apply(tmp_path)
        typed = path.read_text()
        report = migration.apply(tmp_path)
        assert path.read_text() == typed
        assert report.actions == []

    def test_a_missing_ledger_is_a_noop(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []
        assert not (tmp_path / "state" / "enrichment-ledger.jsonl").exists()
