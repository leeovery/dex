"""Tests for exclude.py: purge + record, surviving re-normalization."""

import datetime
import json

import pytest

from dex_engine.exclude import main, run_exclude
from dex_engine.normalize import load_exclusions
from dex_engine.pipeline import ledger
from dex_engine.pipeline.types import Instance, Kind, LedgerEntry, Status

ITEM = "2026-08-19-example-55ad7b"
OTHER = "2026-08-19-other-11ff22"


def write_item_stub(instance: Instance, item_id: str = ITEM) -> None:
    path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stub")


def ledger_entry(instance: Instance, unit_hash: str, item_id: str) -> None:
    ledger.append(
        instance.ledger_path,
        LedgerEntry(
            hash=unit_hash,
            url=f"https://example.test/{unit_hash}",
            item=item_id,
            kind=Kind.WEB,
            status=Status.QUEUED,
            engine="0.2.1",
            date=datetime.date(2026, 8, 20),
            at=datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.UTC),
        ),
    )


class TestRunExclude:
    def test_removes_item_enrichment_and_records_reason(self, instance):
        write_item_stub(instance)
        enrichment = instance.enrichment_dir / ITEM
        enrichment.mkdir(parents=True)
        (enrichment / "web-abc123.md").write_text("fetched")
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert summary == "excluded 1: removed 1 items (0 already gone), 0 ledger entries dropped"
        assert not (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert not enrichment.exists()
        recorded = (instance.state_dir / "exclusions.tsv").read_text()
        assert recorded == f"{ITEM}\tmeme thread\n"

    def test_missing_reason_defaults(self, instance):
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM}])
        assert "\tout of scope\n" in (instance.state_dir / "exclusions.tsv").read_text()

    def test_already_gone_items_counted_not_fatal(self, instance):
        summary = run_exclude(instance, [{"id": ITEM}])
        assert summary == "excluded 1: removed 0 items (1 already gone), 0 ledger entries dropped"

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
        assert f"{ITEM}\tout of scope\n" in (instance.state_dir / "exclusions.tsv").read_text()


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

    def test_an_unreadable_line_is_kept_never_purged_by_accident(self, instance):
        # A purge must not become incidental data loss: a line this code
        # cannot parse is not provably the excluded item's.
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        with instance.ledger_path.open("a", encoding="utf-8") as f:
            f.write("{torn\n")
        run_exclude(instance, [{"id": ITEM}])
        assert instance.ledger_path.read_text() == "{torn\n"


class TestCli:
    def test_main_excludes_from_a_file(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        write_item_stub(instance)
        payload = instance.root / "exclusions.json"
        payload.write_text(json.dumps([{"id": ITEM, "reason": "meme"}]))
        main([str(payload)])
        assert "excluded 1: removed 1 items" in capsys.readouterr().out

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
