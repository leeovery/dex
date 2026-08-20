"""Tests for enrich.py: the thin CLI — parse, build, call, print."""

import json

import pytest

from dex_engine.enrich import build_parser, main
from dex_engine.pipeline.types import Instance


class TestParser:
    def test_run_accepts_limit(self):
        args = build_parser().parse_args(["run", "--limit", "5"])
        assert (args.command, args.limit) == ("run", 5)

    def test_fetch_takes_item_urls_parent_force(self):
        args = build_parser().parse_args(
            ["fetch", "2026-08-19-x-55ad7b", "https://a.test", "https://b.test", "--force"]
        )
        assert args.item == "2026-08-19-x-55ad7b"
        assert args.urls == ["https://a.test", "https://b.test"]
        assert args.parent is None
        assert args.force is True

    def test_mark_takes_status_reason_path(self):
        args = build_parser().parse_args(
            ["mark", "https://a.test", "manual", "--reason", "thin-extraction"]
        )
        assert (args.status, args.reason, args.path) == ("manual", "thin-extraction", None)

    def test_typoed_flags_are_loud_not_ignored(self):
        # The old hand-rolled parser silently ignored typo'd flags (§14).
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "--limt", "5"])

    def test_unknown_status_is_loud(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["mark", "https://a.test", "finished"])

    def test_transcribe_is_absent_not_stubbed(self):
        # It arrives with the capability providers (phase 3).
        with pytest.raises(SystemExit):
            build_parser().parse_args(["transcribe"])

    def test_a_command_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])


class TestMain:
    def test_run_on_an_empty_instance_prints_the_report(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        main(["run"])
        out = capsys.readouterr().out
        assert out.startswith("enrich run — 0 units processed")
        assert "parked — none" in out

    def test_status_prints_the_surface(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        main(["status"])
        assert capsys.readouterr().out.startswith("ledger — 0 entries")

    def test_pass_records_a_stage(self, instance: Instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        main(["pass", "2026-08-19-x-55ad7b", "--stage", "harvest"])
        assert "recorded harvest pass" in capsys.readouterr().out
        record = json.loads(instance.passes_path.read_text().split("\n")[0])
        assert record["stage"] == "harvest"
        assert record["rules"] == 1

    def test_compact_prints_the_result(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        main(["compact"])
        assert "compacted: 0 superseded lines removed" in capsys.readouterr().out

    def test_pipeline_errors_exit_cleanly(self, instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        with pytest.raises(SystemExit) as excinfo:
            main(["mark", "https://never.seen.test/x", "dead"])
        assert "no ledger entry" in str(excinfo.value)

    def test_malformed_config_is_loud(self, instance: Instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        instance.config_path.write_text('{"media_fech": "lead"}')
        with pytest.raises(SystemExit) as excinfo:
            main(["status"])
        assert "media_fech" in str(excinfo.value)
