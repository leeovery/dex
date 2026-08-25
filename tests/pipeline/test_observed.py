"""Tests for the observed-report producer: rejection-first, one shared mechanism."""

import argparse
import datetime
import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from dex_engine.enrich import build_parser as enrich_parser
from dex_engine.pipeline.classify import scrub
from dex_engine.pipeline.observed import (
    KNOWN_VERBS,
    MAX_CLAUSE_CHARS,
    MAX_NOTE_CHARS,
    MAX_STEP_CHARS,
    MAX_STEPS,
    IssuePayloadError,
    observed_fingerprint,
    report_observed,
)

TODAY = datetime.date(2026, 8, 20)
ROOT = Path(__file__).resolve().parents[2]

EXPECTED = "the ledger resolves the unit to the newest line"
OBSERVED = "the report lists the unit as done"
STEPS = ["queue one unit that errors", "run the enrich drain", "read the done section"]


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


def _write(tmp_path, **overrides) -> Path:
    data: dict[str, object] = {"verb": "enrich run", "expected": EXPECTED, "observed": OBSERVED}
    data.update(overrides)
    path = tmp_path / "issue.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _report(path, gh, state_dir, *, enabled=True, engine="0.1.0"):
    return report_observed(
        path,
        enabled=enabled,
        state_dir=state_dir,
        engine_version=engine,
        today=lambda: TODAY,
        gh=gh,
    )


def _records(state_dir: Path) -> list[dict]:
    text = (state_dir / "issue-reports.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.split("\n") if line.strip()]


FP = observed_fingerprint(verb="enrich run", expected=EXPECTED, observed=OBSERVED)


class TestFiling:
    def test_files_the_exact_title_and_body(self, tmp_path):
        gh = FakeGh()
        output = _report(_write(tmp_path, steps=STEPS), gh, tmp_path / "state")
        creates = gh.of("create")
        assert len(creates) == 1
        args = creates[0]
        assert args[args.index("--repo") + 1] == "leeovery/dex"
        assert args[args.index("--title") + 1] == (
            f"[observed] enrich run: expected {EXPECTED}; observed {OBSERVED} fp-{FP}"
        )
        assert args[args.index("--body") + 1] == (
            "Session-observed report from a dex instance. Structured mechanics\n"
            "only: each field was refused, not scrubbed, on any detected leak,\n"
            "and the reporter's fuller note never leaves the instance.\n"
            "\n"
            "- engine: 0.1.0\n"
            "- verb: enrich run\n"
            "- date: 2026-08-20\n"
            f"- expected: {EXPECTED}\n"
            f"- observed: {OBSERVED}\n"
            f"- fingerprint: fp-{FP}\n"
            "\n"
            "steps:\n"
            "1. queue one unit that errors\n"
            "2. run the enrich drain\n"
            "3. read the done section"
        )
        assert "filed engine issue #7: observed report on enrich run" in output

    def test_steps_are_optional_and_absent_from_the_body_when_omitted(self, tmp_path):
        gh = FakeGh()
        _report(_write(tmp_path), gh, tmp_path / "state")
        body = gh.of("create")[0][gh.of("create")[0].index("--body") + 1]
        assert "steps:" not in body

    def test_search_happens_before_filing(self, tmp_path):
        gh = FakeGh()
        _report(_write(tmp_path), gh, tmp_path / "state")
        assert gh.calls[0][:2] == ["issue", "list"]
        assert f"fp-{FP} in:title" in gh.calls[0]
        assert "all" in gh.calls[0]  # --state all: closed issues drive dedup too

    def test_memory_records_the_filing(self, tmp_path):
        gh = FakeGh()
        _report(_write(tmp_path), gh, tmp_path / "state")
        record = _records(tmp_path / "state")[0]
        assert record["action"] == "filed"
        assert record["engine"] == "0.1.0"
        assert record["issue"] == 7
        assert record["fingerprint"] == FP

    def test_title_stays_under_the_github_cap_at_maximal_bounds(self, tmp_path):
        gh = FakeGh()
        longest_verb = max(KNOWN_VERBS, key=len)
        payload = _write(
            tmp_path,
            verb=longest_verb,
            expected="e" * MAX_CLAUSE_CHARS,
            observed="o" * MAX_CLAUSE_CHARS,
        )
        _report(payload, gh, tmp_path / "state")
        title = gh.of("create")[0][gh.of("create")[0].index("--title") + 1]
        assert len(title) <= 256


class TestFingerprint:
    def test_stable_across_steps_and_note(self):
        assert observed_fingerprint(verb="lint", expected="a", observed="b") == (
            observed_fingerprint(verb="lint", expected="a", observed="b")
        )

    def test_varies_by_verb_expected_observed(self):
        base = observed_fingerprint(verb="lint", expected="a", observed="b")
        assert observed_fingerprint(verb="sync", expected="a", observed="b") != base
        assert observed_fingerprint(verb="lint", expected="x", observed="b") != base
        assert observed_fingerprint(verb="lint", expected="a", observed="x") != base


def _rejects(tmp_path, *hints, payload_text=None, **overrides) -> str:
    """One rejection probe: raises, names every hint, files and writes nothing."""
    gh = FakeGh()
    state = tmp_path / "state"
    if payload_text is not None:
        path = tmp_path / "issue.json"
        path.write_text(payload_text, encoding="utf-8")
    else:
        path = _write(tmp_path, **overrides)
    with pytest.raises(IssuePayloadError) as err:
        _report(path, gh, state)
    for hint in hints:
        assert hint in str(err.value)
    assert gh.calls == []
    assert not (state / "issue-reports.jsonl").exists()
    return str(err.value)


class TestRejection:
    """The privacy ruling: a mechanically detectable leak refuses the payload.

    Never scrubbed, never partially filed — the error names the field and
    what to abstract, and nothing is written anywhere.
    """

    def test_item_id_in_expected(self, tmp_path):
        message = _rejects(
            tmp_path,
            "expected",
            "corpus item id",
            expected="the digest for 2026-08-19-claude-agent-teardown-ab12cd is rewritten",
        )
        assert "abstractly" in message

    def test_url_in_observed(self, tmp_path):
        _rejects(
            tmp_path,
            "observed",
            "a URL",
            observed="the fetch of https://example.com/x lands as done",
        )

    def test_email_in_expected(self, tmp_path):
        _rejects(
            tmp_path,
            "expected",
            "email address",
            expected="mail to owner@example.com is never sent",
        )

    def test_home_path_in_a_step(self, tmp_path):
        _rejects(
            tmp_path,
            "steps[0]",
            "filesystem path",
            steps=["read /Users/owner/dex-notes/notes.md and rerun"],
        )

    def test_windows_home_path_in_a_step(self, tmp_path):
        _rejects(
            tmp_path,
            "steps[0]",
            "filesystem path",
            steps=[r"open C:\Users\owner\dex-notes\capture.md"],
        )

    def test_relative_instance_path_in_observed(self, tmp_path):
        _rejects(
            tmp_path,
            "observed",
            "instance content",
            observed="the file lands under enrichment/x-112233 twice",
        )

    def test_instance_repo_name_in_observed(self, tmp_path):
        _rejects(tmp_path, "observed", "instance content", observed="the push to dex-notes fails")

    def test_multiple_leaky_fields_are_all_named(self, tmp_path):
        message = _rejects(
            tmp_path,
            "expected:",
            "observed:",
            expected="item 2026-08-19-claude-agent-teardown-ab12cd is digested",
            observed="the fetch of https://example.com/x lands as done",
        )
        assert "corpus item id" in message
        assert "a URL" in message

    def test_rejection_with_a_note_still_writes_nothing(self, tmp_path):
        _rejects(
            tmp_path,
            "observed",
            observed="the fetch of https://example.com/x lands as done",
            note="fuller local context",
        )

    def test_multiline_expected(self, tmp_path):
        _rejects(tmp_path, "expected", "single-line", expected="one\ntwo")

    def test_over_length_expected(self, tmp_path):
        _rejects(tmp_path, "expected", str(MAX_CLAUSE_CHARS), expected="x" * (MAX_CLAUSE_CHARS + 1))

    def test_over_length_step(self, tmp_path):
        _rejects(tmp_path, "steps[0]", str(MAX_STEP_CHARS), steps=["x" * (MAX_STEP_CHARS + 1)])

    def test_too_many_steps(self, tmp_path):
        _rejects(tmp_path, "steps", str(MAX_STEPS), steps=["a step"] * (MAX_STEPS + 1))

    def test_unknown_verb(self, tmp_path):
        message = _rejects(tmp_path, "verb", "frobnicate", verb="frobnicate")
        assert "enrich run" in message  # the vocabulary is stated, not hinted

    def test_bare_enrich_is_not_a_runnable_verb(self, tmp_path):
        _rejects(tmp_path, "verb", verb="enrich")

    def test_missing_field(self, tmp_path):
        _rejects(
            tmp_path,
            "missing required key(s) ['observed']",
            payload_text=json.dumps({"verb": "lint", "expected": EXPECTED}),
        )

    def test_non_object_payload(self, tmp_path):
        _rejects(tmp_path, "expected a JSON object", payload_text=json.dumps(["not", "it"]))

    def test_invalid_json(self, tmp_path):
        _rejects(tmp_path, "invalid JSON", payload_text="{not json")

    def test_unknown_key(self, tmp_path):
        _rejects(tmp_path, "unknown key(s) ['severity']", severity="high")

    def test_wrong_type_steps(self, tmp_path):
        _rejects(tmp_path, "steps must be a list", steps="not a list")

    def test_over_length_note(self, tmp_path):
        _rejects(tmp_path, "note", str(MAX_NOTE_CHARS), note="x" * (MAX_NOTE_CHARS + 1))

    def test_wrong_type_note(self, tmp_path):
        _rejects(tmp_path, "note", note=7)


class TestMirror:
    """The rejectors and scrub() judge the same texts the same way.

    Both consume classify's detectors, so a detector improvement reaches
    both; this pins that a text scrub() would touch is a text the verb
    refuses, and a text scrub() passes untouched files cleanly.
    """

    @pytest.mark.parametrize(
        ("text", "leaks"),
        [
            ("fetch of https://example.com fails", True),
            ("mail to owner@example.com bounces", True),
            ("a write to /Users/owner/notes.md fails", True),
            ("item 2026-08-19-some-slug-ab12cd is rewritten", True),
            ("the corpus/2026 directory is scanned twice", True),
            ("the repo dex-notes is pushed twice", True),
            ("the ledger resolves the unit to the newest line", False),
            ("a unit with two child URLs drains once", False),
            ("the report lists the unit as done", False),
        ],
    )
    def test_rejectors_mirror_the_scrubber(self, tmp_path, text, leaks):
        assert (scrub(text) != text) == leaks
        gh = FakeGh()
        path = _write(tmp_path, observed=text)
        if leaks:
            with pytest.raises(IssuePayloadError):
                _report(path, gh, tmp_path / "state")
            assert gh.calls == []
        else:
            _report(path, gh, tmp_path / "state")
            assert len(gh.of("create")) == 1


ITEM_ID_NOTE = (
    "the unit for 2026-08-19-claude-agent-teardown-ab12cd shows done while its ledger line is error"
)


class TestNoteSplit:
    def test_note_lands_locally_and_never_in_any_gh_argument(self, tmp_path):
        gh = FakeGh()
        output = _report(_write(tmp_path, note=ITEM_ID_NOTE), gh, tmp_path / "state")
        for call in gh.calls:
            for arg in call:
                assert "2026-08-19-claude-agent-teardown-ab12cd" not in arg
                assert ITEM_ID_NOTE not in arg
        record = _records(tmp_path / "state")[0]
        assert record["note"] == ITEM_ID_NOTE
        assert "note kept in state/issue-reports.jsonl" in output

    def test_record_without_note_has_no_note_key(self, tmp_path):
        gh = FakeGh()
        _report(_write(tmp_path), gh, tmp_path / "state")
        assert "note" not in _records(tmp_path / "state")[0]

    def test_seen_again_comment_still_records_the_note(self, tmp_path):
        gh = FakeGh(search_results=[{"number": 12, "state": "OPEN"}])
        _report(_write(tmp_path, note=ITEM_ID_NOTE), gh, tmp_path / "state")
        record = _records(tmp_path / "state")[0]
        assert record["action"] == "commented"
        assert record["note"] == ITEM_ID_NOTE
        comment_body = gh.of("comment")[0][gh.of("comment")[0].index("--body") + 1]
        assert ITEM_ID_NOTE not in comment_body


class TestDedupe:
    """Same arithmetic as the exception filer — the mechanism is shared."""

    def test_second_report_at_the_same_engine_is_silent(self, tmp_path):
        _report(_write(tmp_path), FakeGh(), tmp_path / "state")
        gh2 = FakeGh()
        output = _report(_write(tmp_path), gh2, tmp_path / "state")
        assert gh2.calls == []  # the local memory answers before gh is consulted
        assert output.startswith("nothing filed")
        assert len(_records(tmp_path / "state")) == 1

    def test_open_issue_gets_one_seen_again_comment(self, tmp_path):
        gh = FakeGh(search_results=[{"number": 12, "state": "OPEN"}])
        output = _report(_write(tmp_path), gh, tmp_path / "state")
        assert gh.of("create") == []
        comments = gh.of("comment")
        assert len(comments) == 1
        assert comments[0][2] == "12"
        assert "engine issue #12: seen-again comment added" in output

    def test_closed_issue_with_no_local_baseline_stays_silent(self, tmp_path):
        gh = FakeGh(search_results=[{"number": 12, "state": "CLOSED"}])
        output = _report(_write(tmp_path), gh, tmp_path / "state")
        assert gh.of("create") == []
        assert gh.of("comment") == []
        assert output.startswith("nothing filed")

    def test_closed_issue_recurring_on_newer_engine_files_regression(self, tmp_path):
        _report(_write(tmp_path), FakeGh(), tmp_path / "state", engine="0.1.0")
        gh2 = FakeGh(search_results=[{"number": 7, "state": "CLOSED"}])
        output = _report(_write(tmp_path), gh2, tmp_path / "state", engine="0.2.0")
        body = gh2.of("create")[0][gh2.of("create")[0].index("--body") + 1]
        assert "Recurrence of #7" in body
        assert "filed engine issue" in output


class TestGate:
    def test_disabled_config_files_and_records_nothing(self, tmp_path):
        gh = FakeGh()
        output = _report(_write(tmp_path), gh, tmp_path / "state", enabled=False)
        assert gh.calls == []
        assert not (tmp_path / "state" / "issue-reports.jsonl").exists()
        assert "report_issues" in output

    def test_disabled_config_still_rejects_a_leaky_payload(self, tmp_path):
        path = _write(tmp_path, observed="the fetch of https://example.com/x lands as done")
        with pytest.raises(IssuePayloadError):
            _report(path, FakeGh(), tmp_path / "state", enabled=False)


class TestSoftEdge:
    def test_gh_failure_is_a_stated_outcome_never_a_raise(self, tmp_path):
        def broken(_args):
            raise subprocess.SubprocessError("gh exploded")

        output = _report(_write(tmp_path), broken, tmp_path / "state")
        assert "issue filing failed" in output
        assert not (tmp_path / "state" / "issue-reports.jsonl").exists()

    def test_torn_memory_line_is_a_stated_outcome(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        (state / "issue-reports.jsonl").write_text('{"note": "torn record"}\n')
        output = _report(_write(tmp_path), FakeGh(), state)
        assert "issue filing failed" in output


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    return {
        name: choice
        for action in parser._actions  # noqa: SLF001 — argparse offers no public walk
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
        for name, choice in action.choices.items()
    }


class TestVocabulary:
    """KNOWN_VERBS is maintained by hand and pinned here to the real CLI."""

    def test_known_verbs_pin_the_real_cli_surface(self):
        scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "scripts"
        ]
        expected = {name.removeprefix("dex-") for name in scripts} - {"enrich"}
        for name, sub in _subcommands(enrich_parser()).items():
            nested = _subcommands(sub)
            if nested:
                expected.update(f"enrich {name} {leaf}" for leaf in nested)
            else:
                expected.add(f"enrich {name}")
        assert set(KNOWN_VERBS) == expected

    def test_known_verbs_are_sorted(self):
        assert list(KNOWN_VERBS) == sorted(KNOWN_VERBS)

    def test_shim_usage_names_every_top_level_verb(self):
        shim = (ROOT / "instance" / "dex").read_text(encoding="utf-8")
        listed = re.search(r"usage: bin/dex <([a-z|]+)>", shim)
        assert listed is not None
        scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "scripts"
        ]
        assert set(listed.group(1).split("|")) == {name.removeprefix("dex-") for name in scripts}
