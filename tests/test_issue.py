"""Tests for issue.py: the thin CLI — parse, build, call, print."""

import json

import pytest

from dex_engine.issue import build_parser, main
from dex_engine.pipeline import issues


class FakeGh:
    """A minimal gh seam for CLI runs: files everything, records calls."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return "[]"
        if args[:2] == ["issue", "create"]:
            return "https://github.com/leeovery/dex/issues/9\n"
        raise AssertionError(f"unexpected gh call: {args}")


def _payload(instance, **overrides):
    data = {
        "verb": "enrich run",
        "expected": "the ledger resolves the unit to the newest line",
        "observed": "the report lists the unit as done",
    }
    data.update(overrides)
    path = instance.cache_dir / "issue.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestParser:
    def test_takes_a_payload_file(self):
        args = build_parser().parse_args(["--file", "cache/issue.json"])
        assert str(args.file) == "cache/issue.json"

    def test_the_payload_file_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])


class TestMain:
    def test_files_and_records_from_the_instance_at_cwd(self, instance, monkeypatch, capsys):
        gh = FakeGh()
        monkeypatch.setattr(issues, "gh_runner", gh)
        monkeypatch.chdir(instance.root)
        _payload(instance)
        main(["--file", "cache/issue.json"])
        out = capsys.readouterr().out
        assert "filed engine issue #9: observed report on enrich run" in out
        record = json.loads((instance.state_dir / "issue-reports.jsonl").read_text().split("\n")[0])
        assert record["action"] == "filed"
        assert record["issue"] == 9

    def test_a_rejected_payload_exits_naming_the_field(self, instance, monkeypatch):
        monkeypatch.setattr(issues, "gh_runner", FakeGh())
        monkeypatch.chdir(instance.root)
        _payload(instance, observed="the fetch of https://example.com/x lands as done")
        with pytest.raises(SystemExit) as err:
            main(["--file", "cache/issue.json"])
        assert "dex-issue:" in str(err.value.code)
        assert "observed" in str(err.value.code)
        assert not (instance.state_dir / "issue-reports.jsonl").exists()

    def test_report_issues_false_gates_the_verb_off(self, instance, monkeypatch, capsys):
        def never(_args):
            raise AssertionError("gh must not be called when reporting is off")

        monkeypatch.setattr(issues, "gh_runner", never)
        monkeypatch.chdir(instance.root)
        instance.config_path.write_text(json.dumps({"report_issues": False}))
        _payload(instance)
        main(["--file", "cache/issue.json"])
        out = capsys.readouterr().out
        assert "report_issues" in out
        assert not (instance.state_dir / "issue-reports.jsonl").exists()

    def test_a_missing_payload_file_exits_stated_not_raised(self, instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        with pytest.raises(SystemExit) as err:
            main(["--file", "cache/absent.json"])
        assert "dex-issue:" in str(err.value.code)
