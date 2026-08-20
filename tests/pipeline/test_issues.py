"""Tests for the issue filer: sanitized, dedup'd, rate-limited, non-fatal."""

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
        "errno": None,
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
        assert event.error_class == "ValueError"
        assert event.errno is None  # errno is OSError-family only

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
    def test_stable_across_errno_and_hash(self):
        a = fingerprint(_event(errno=2, unit_hash="73bd784849"))
        b = fingerprint(_event(errno=28, unit_hash="a1b2c3d4e5"))
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
        # The local memory answers before gh is ever consulted.
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
        # The instance's engine predates the fix — sync cures it.
        gh = FakeGh(search_results=[])
        _report([_event()], gh, tmp_path, engine="0.1.0")
        gh2 = FakeGh(search_results=[{"number": 7, "state": "CLOSED"}])
        # Same fingerprint erroring again at 0.1.0 via a different URL: the
        # memory says 0.1.0 already reported, so nothing happens at all.
        outcome = _report([_event(unit_hash="a1b2c3d4e5")], gh2, tmp_path, engine="0.1.0")
        assert outcome.filed == 0
        assert gh2.calls == []


def _raised(exc: BaseException) -> BaseException:
    """Raise-and-catch so a real traceback (with an engine frame) is attached."""
    try:
        try:
            from_line("not json")  # puts a dex_engine frame on the stack
        except ValueError:
            raise exc from None
    except BaseException as caught:  # noqa: BLE001 — the probe needs the exact instance back
        return caught
    raise AssertionError("unreachable")


class TestSanitization:
    """The probe scenarios: what would actually reach the public repo.

    Free text is dropped wholesale (ruled): every scenario asserts the
    exception MESSAGE is absent from the body, not merely scrubbed clean.
    """

    def _body(self, exc, tmp_path, *, kind=Kind.WEB, unit_format=None) -> str:
        gh = FakeGh()
        event = error_event(
            unit_hash="deadbeefca", kind=kind, unit_format=unit_format, exc=_raised(exc)
        )
        _report([event], gh, tmp_path)
        creates = gh.of("create")
        assert len(creates) == 1
        args = creates[0]
        return args[args.index("--title") + 1] + "\n" + args[args.index("--body") + 1]

    def test_oserror_sends_class_and_errno_only(self, tmp_path):
        exc = OSError(
            28,
            "No space left on device",
            "/Users/leeovery/Code/dex/dex-health/enrichment/"
            "2026-08-18-lee-mri-results-and-diagnosis-abc123/web-9f2c11.md",
        )
        body = self._body(exc, tmp_path)
        assert "- errno: 28" in body
        assert "- error: OSError" in body
        assert "message:" not in body
        assert "No space left" not in body  # the message is gone, not scrubbed
        for leak in ("leeovery", "dex-health", "mri", "diagnosis", "web-9f2c11"):
            assert leak not in body

    def test_relative_instance_paths_never_leak(self, tmp_path):
        exc = ValueError(
            "cannot write enrichment/2026-08-19-private-family-dispute-notes-9a1b2c/"
            "x-112233.md: name too long"
        )
        body = self._body(exc, tmp_path, kind=Kind.X)
        assert "private-family-dispute" not in body
        assert "9a1b2c" not in body
        assert "name too long" not in body  # no residue of the message at all
        assert "<path>" not in body  # nothing to redact — the field is gone

    def test_urls_and_emails_never_leak(self, tmp_path):
        exc = RuntimeError(
            "fetch of https://secret-internal.example.com/med/records?id=42 "
            "failed for me@leeovery.com"
        )
        body = self._body(exc, tmp_path)
        assert "secret-internal" not in body
        assert "leeovery" not in body
        assert "failed for" not in body  # the message never made it in

    def test_windows_home_paths_never_leak(self, tmp_path):
        exc = ValueError(r"bad path C:\Users\leeovery\dex-health\inbox\therapy.md")
        body = self._body(exc, tmp_path, kind=Kind.FILE, unit_format=Format.PDF)
        for leak in ("leeovery", "dex-health", "therapy", "bad path"):
            assert leak not in body

    def test_instance_repo_names_never_leak(self, tmp_path):
        body = self._body(ValueError("repo dex-health unreachable"), tmp_path)
        assert "dex-health" not in body
        assert "unreachable" not in body

    def test_bare_item_ids_never_leak(self, tmp_path):
        body = self._body(
            ValueError("unknown corpus item 2026-08-19-embarrassing-note-ab12cd"), tmp_path
        )
        assert "embarrassing-note" not in body
        assert "unknown corpus item" not in body

    def test_engine_frames_survive_sanitization(self, tmp_path):
        # The traceback is the diagnostic payload — sanitization must not
        # eat the package-relative frame paths.
        gh = FakeGh()
        event = _capture(lambda: from_line("not json"))  # raised INSIDE engine code
        _report([event], gh, tmp_path)
        args = gh.of("create")[0]
        body = args[args.index("--body") + 1]
        assert "dex_engine/pipeline/ledger.py" in body
        assert "in from_line" in body
        assert "not json" not in body  # ...but the message still never appears


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

    def test_torn_but_valid_json_memory_line_is_never_fatal(self, tmp_path):
        # A record can be valid JSON yet miss fields (union merges, hand
        # damage) — that must degrade to a note, never a KeyError that
        # kills the run and loses the report.
        (tmp_path / "issue-reports.jsonl").write_text('{"note": "torn record"}\n')
        gh = FakeGh()
        outcome = _report([_event()], gh, tmp_path)
        assert outcome.filed == 0
        note = next(n for n in outcome.notes if "issue filing failed" in n)
        assert "issue-reports.jsonl:1" in note
        assert "missing field(s)" in note

    def test_unparseable_memory_line_is_never_fatal(self, tmp_path):
        (tmp_path / "issue-reports.jsonl").write_text('{"fingerprint": "ab\n')
        outcome = _report([_event()], FakeGh(), tmp_path)
        assert any("issue filing failed" in n for n in outcome.notes)

    def test_notes_are_scrubbed(self, tmp_path):
        def leaky(_args):
            raise OSError("/Users/someone/.config/gh token missing")

        outcome = _report([_event()], leaky, tmp_path)
        note = next(n for n in outcome.notes if "issue filing failed" in n)
        assert "/Users/" not in note
