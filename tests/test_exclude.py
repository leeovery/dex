"""Tests for exclude.py: purge + record, surviving re-normalization."""

import json

import pytest

from dex_engine.exclude import main, run_exclude
from dex_engine.normalize import load_exclusions
from dex_engine.pipeline.types import Instance

ITEM = "2026-08-19-example-55ad7b"


def write_item_stub(instance: Instance, item_id: str = ITEM) -> None:
    path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stub")


class TestRunExclude:
    def test_removes_item_enrichment_and_records_reason(self, instance):
        write_item_stub(instance)
        enrichment = instance.enrichment_dir / ITEM
        enrichment.mkdir(parents=True)
        (enrichment / "web-abc123.md").write_text("fetched")
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert summary == "excluded 1: removed 1 items (0 already gone)"
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
        assert summary == "excluded 1: removed 0 items (1 already gone)"

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
