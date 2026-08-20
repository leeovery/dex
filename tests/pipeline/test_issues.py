"""Tests for the issue filer (§13): sanitized, dedup'd, rate-limited, non-fatal."""

import datetime
import json
import subprocess

from dex_engine.pipeline.issues import (
    MAX_NEW_ISSUES_PER_RUN,
    ErrorEvent,
    error_event,
    fingerprint,
    report_errors,
)
from dex_engine.pipeline.ledger import from_line
from dex_engine.pipeline.types import Format, Kind

TODAY = datetime.date(2026, 8, 20)


def _event(**overrides) -> ErrorEvent:
    base = {
        "unit_hash": "73bd784849",
        "kind": Kind.WEB,
        "format": None,
        "error_class": "ValueError",
        "function": "_write_output",
        "message": "ValueError: boom",
        "frames": "dex_engine/pipeline/run.py:600 in _write_output",
    }
    base.update(overrides)
    return ErrorEvent(**base)


class FakeGh:
    """A scriptable gh seam: canned search results, recorded calls."""

    def __init__(self, search_results=None, create_url="https://github.com/leeovery/dex/issues/7"):
        self.search_results = search_results or []
        self.create_url = create_url
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.search_results)
        if args[:2] == ["issue", "create"]:
            return self.create_url + "\n"
        if args[:2] == ["issue", "comment"]:
            return ""
        raise AssertionError(f"unexpected gh call: {args}")

    def of(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["issue", verb]]


def _report(events, gh, state_dir, *, engine="0.1.0", enabled=True):
    return report_errors(
        events,
        enabled=enabled,
        state_dir=state_dir,
        engine_version=engine,
        command="enrich run",
        today=lambda: TODAY,
        gh=gh,
    )


def _capture(fn, *, kind=Kind.WEB, unit_format=None) -> ErrorEvent:
    try:
        fn()
    except ValueError as e:
        return error_event(unit_hash="73bd784849", kind=kind, unit_format=unit_format, exc=e)
    raise AssertionError("expected fn to raise ValueError")


class TestErrorEvent:
    def test_captures_engine_frames_and_innermost_function(self):
        def inner():
            raise ValueError("kaput https://secret.example/x")

        event = _capture(inner)
        # This test file is not an engine frame — no dex_engine frames exist.
        assert event.function == "unknown"
        assert event.frames == "no engine frames"
        assert "https://" not in event.message  # scrubbed
        assert "<url>" in event.message
        assert event.error_class == "ValueError"

    def test_engine_frames_are_package_relative(self):
        # Drive a real engine function into raising so the traceback holds
        # dex_engine frames with absolute paths.
        event = _capture(
            lambda: from_line("not json"), kind=Kind.FILE, unit_format=Format.PDF
        )
        assert event.function == "from_line"
        assert "dex_engine/pipeline/ledger.py" in event.frames
        assert "/Users/" not in event.frames
        assert "/home/" not in event.frames


class TestFingerprint:
    def test_stable_across_message_and_hash(self):
        a = fingerprint(_event(message="ValueError: a", unit_hash="73bd784849"))
        b = fingerprint(_event(message="ValueError: b", unit_hash="a1b2c3d4e5"))
        assert a == b

    def test_varies_by_class_function_kind_format(self):
        base = fingerprint(_event())
        assert fingerprint(_event(error_class="KeyError")) != base
        assert fingerprint(_event(function="other")) != base
        assert fingerprint(_event(kind=Kind.X)) != base
        assert fingerprint(_event(kind=Kind.FILE, format=Format.PDF)) != base


class TestFiling:
    def test_files_a_new_issue_with_allowlisted_body(self, tmp_path):
        gh = FakeGh()
        outcome = _report([_event()], gh, tmp_path)
        assert outcome.filed == 1
        creates = gh.of("create")
        assert len(creates) == 1
        args = creates[0]
        title = args[args.index("--title") + 1]
        body = args[args.index("--body") + 1]
        assert f"fp-{fingerprint(_event())}" in title
        assert "ValueError in _write_output (web)" in title
        assert "engine: 0.1.0" in body
        assert "command: enrich run" in body
        assert "kind: web" in body
        assert "url hash: 73bd784849" in body
        assert "https://" not in body  # nothing but the allowlist leaves the instance
        assert any("filed engine issue #7" in note for note in outcome.notes)

    def test_search_happens_before_filing(self, tmp_path):
        gh = FakeGh()
        _report([_event()], gh, tmp_path)
        assert gh.calls[0][:2] == ["issue", "list"]
        search = gh.calls[0]
        assert f"fp-{fingerprint(_event())} in:title" in search
        assert "all" in search  # --state all: closed issues drive dedup too

    def test_memory_records_the_filing(self, tmp_path):
        gh = FakeGh()
        _report([_event()], gh, tmp_path)
        lines = (tmp_path / "issue-reports.jsonl").read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["action"] == "filed"
        assert record["engine"] == "0.1.0"
        assert record["issue"] == 7
        assert record["fingerprint"] == fingerprint(_event())

    def test_duplicate_fingerprints_in_one_run_file_once(self, tmp_path):
        gh = FakeGh()
        outcome = _report([_event(), _event(unit_hash="a1b2c3d4e5")], gh, tmp_path)
        assert outcome.filed == 1
        assert len(gh.of("create")) == 1

    def test_rate_limit_caps_new_issues_per_run(self, tmp_path):
        gh = FakeGh()
        events = [_event(function=f"fn_{i}") for i in range(MAX_NEW_ISSUES_PER_RUN + 2)]
        outcome = _report(events, gh, tmp_path)
        assert outcome.filed == MAX_NEW_ISSUES_PER_RUN
        assert len(gh.of("create")) == MAX_NEW_ISSUES_PER_RUN

    def test_disabled_config_makes_no_gh_calls(self, tmp_path):
        gh = FakeGh()
        outcome = _report([_event()], gh, tmp_path, enabled=False)
        assert outcome.filed == 0
        assert gh.calls == []

    def test_no_events_makes_no_gh_calls(self, tmp_path):
        gh = FakeGh()
        assert _report([], gh, tmp_path).filed == 0
        assert gh.calls == []


class TestDedup:
    def test_open_issue_gets_one_seen_again_comment(self, tmp_path):
        gh = FakeGh(search_results=[{"number": 12, "state": "OPEN"}])
        outcome = _report([_event()], gh, tmp_path)
        assert outcome.filed == 0
        comments = gh.of("comment")
        assert len(comments) == 1
        assert comments[0][2] == "12"
        body = comments[0][comments[0].index("--body") + 1]
        assert body == "seen again (engine 0.1.0, 2026-08-20)"

    def test_same_engine_never_re_reports(self, tmp_path):
        gh = FakeGh(search_results=[{"number": 12, "state": "OPEN"}])
        _report([_event()], gh, tmp_path)
        gh2 = FakeGh(search_results=[{"number": 12, "state": "OPEN"}])
        outcome = _report([_event()], gh2, tmp_path)
        # The local memory answers before gh is ever consulted (§13).
        assert gh2.calls == []
        assert outcome.filed == 0

    def test_newer_engine_comments_again_on_open_issue(self, tmp_path):
        gh = FakeGh(search_results=[{"number": 12, "state": "OPEN"}])
        _report([_event()], gh, tmp_path, engine="0.1.0")
        gh2 = FakeGh(search_results=[{"number": 12, "state": "OPEN"}])
        _report([_event()], gh2, tmp_path, engine="0.2.0")
        assert len(gh2.of("comment")) == 1  # one comment per engine version per instance

    def test_closed_issue_with_no_local_baseline_stays_silent(self, tmp_path):
        gh = FakeGh(search_results=[{"number": 12, "state": "CLOSED"}])
        outcome = _report([_event()], gh, tmp_path)
        assert outcome.filed == 0
        assert gh.of("create") == []
        assert gh.of("comment") == []

    def test_closed_issue_recurring_on_newer_engine_files_regression(self, tmp_path):
        gh = FakeGh(search_results=[])
        _report([_event()], gh, tmp_path, engine="0.1.0")  # original filing
        gh2 = FakeGh(search_results=[{"number": 7, "state": "CLOSED"}])
        outcome = _report([_event()], gh2, tmp_path, engine="0.2.0")
        assert outcome.filed == 1
        body = gh2.of("create")[0]
        assert "Recurrence of #7" in body[body.index("--body") + 1]

    def test_closed_issue_on_same_engine_stays_silent(self, tmp_path):
        # The instance's engine predates the fix — sync cures it (§13).
        gh = FakeGh(search_results=[])
        _report([_event()], gh, tmp_path, engine="0.1.0")
        gh2 = FakeGh(search_results=[{"number": 7, "state": "CLOSED"}])
        # Same fingerprint erroring again at 0.1.0 via a different URL: the
        # memory says 0.1.0 already reported, so nothing happens at all.
        outcome = _report([_event(unit_hash="a1b2c3d4e5")], gh2, tmp_path, engine="0.1.0")
        assert outcome.filed == 0
        assert gh2.calls == []


class TestSoftEdge:
    def test_gh_failure_is_a_note_never_fatal(self, tmp_path):
        def broken(_args):
            raise subprocess.SubprocessError("gh exploded")

        outcome = _report([_event()], broken, tmp_path)
        assert outcome.filed == 0
        assert any("issue filing failed" in note for note in outcome.notes)

    def test_missing_gh_is_a_note_never_fatal(self, tmp_path):
        def missing(_args):
            raise FileNotFoundError("gh")

        outcome = _report([_event()], missing, tmp_path)
        assert any("issue filing failed" in note for note in outcome.notes)

    def test_notes_are_scrubbed(self, tmp_path):
        def leaky(_args):
            raise OSError("/Users/someone/.config/gh token missing")

        outcome = _report([_event()], leaky, tmp_path)
        note = next(n for n in outcome.notes if "issue filing failed" in n)
        assert "/Users/" not in note
