"""Session-observed bug reports: the ``dex issue`` verb, the second producer.

The exception filer catches what RAISES; this verb files what a session
SAW misbehave with nothing raised: a report contradicting state on disk, a
documented behaviour that did not happen, a verb that wrote the wrong
thing quietly. The session states the defect as structured mechanics —
``verb``, ``expected``, ``observed``, optional ``steps`` — and the same
mechanism as the exception path (:func:`.issues.file_reports`) dedupes,
rate-limits and files it at the public engine repo.

Privacy is by REJECTION, never redaction (the ruled design): every public
field runs against the scrubber's own detectors — URLs, emails,
filesystem paths, corpus item ids, instance-content tokens — and one hit
refuses the whole payload with per-field reasons telling the session what
to abstract and retry. A rejected payload files nothing and writes
nothing anywhere. Redaction would leave judgment about what the residue
still says; refusal leaves nothing to judge.

The one free-text slot is ``note``: local only, appended to the
``state/issue-reports.jsonl`` record for the owner to read and forward by
hand. It is exempt from the leak rejectors precisely because it is never
filed.
"""

import datetime
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from . import issues
from .classify import (
    EMAIL_RE,
    HOME_RE,
    INSTANCE_TOKEN_RE,
    ITEM_ID_PATTERN,
    ITEM_SLUG_PATTERN,
    SCHEMELESS_URL_RE,
    URL_RE,
)

__all__ = [
    "KNOWN_VERBS",
    "MAX_CLAUSE_CHARS",
    "MAX_NOTE_CHARS",
    "MAX_STEPS",
    "MAX_STEP_CHARS",
    "IssuePayloadError",
    "observed_fingerprint",
    "report_observed",
]

# The command vocabulary `verb` is validated against: every runnable
# `bin/dex …` form — the shim's top-level verbs (one per dex-* entry
# point in pyproject) plus enrich's spaced subcommand forms. Maintained
# by hand, pinned by test to the real CLI surface (pyproject's
# [project.scripts] and enrich's argparse tree), so it cannot rot.
KNOWN_VERBS = (
    "connect",
    "enrich compact",
    "enrich fetch",
    "enrich item digest",
    "enrich item new",
    "enrich mark",
    "enrich pass",
    "enrich run",
    "enrich status",
    "enrich transcribe",
    "exclude",
    "inbox",
    "issue",
    "lint",
    "new",
    "normalize",
    "render",
    "serve",
    "sync",
)

# Bounds, chosen so the templated title (both clauses plus overhead)
# stays under GitHub's 256-character issue-title cap:
MAX_CLAUSE_CHARS = 90  # expected / observed — one mechanics sentence each
MAX_STEP_CHARS = 120  # one mechanics statement per step
MAX_STEPS = 8  # a repro is short or it is not mechanics
MAX_NOTE_CHARS = 2000  # local-only free text; newlines allowed

_REQUIRED_KEYS = frozenset({"verb", "expected", "observed"})
_OPTIONAL_KEYS = frozenset({"steps", "note"})

# The leak rejectors: the scrubber's own detectors (classify.py), reused
# for DETECTION with refusal instead of redaction. Two of them are the
# rejectors' alone — the schemeless URL and the shortid-trimmed item slug.
# They guard PUBLIC fields, where the cost of a miss is permanent and
# off-instance; `scrub` only ever writes into the instance's own ledger,
# and widening it would redact honest local diagnostics for no gain. Order matters only for
# the message — the first hit names the field's problem most precisely,
# so the specific item-id grammar runs before the instance-token detector
# that also contains it. Each entry: (pattern, what the field contains,
# how to abstract it).
_REJECTORS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (URL_RE, "a URL", "name the fetch mechanics, never the address"),
    (
        SCHEMELESS_URL_RE,
        "a URL with its scheme dropped",
        "deleting the https:// does not abstract an address — name the fetch mechanics",
    ),
    (EMAIL_RE, "an email address", "no address belongs in a mechanics sentence"),
    (HOME_RE, "a filesystem path", "name the file's role, never its location"),
    (
        re.compile(ITEM_ID_PATTERN),
        "a corpus item id",
        'describe the item abstractly ("an item with two URLs"), never by id or slug',
    ),
    (
        re.compile(ITEM_SLUG_PATTERN),
        "a corpus item's date and slug",
        (
            "trimming the shortid leaves the owner's own words — describe the "
            'item abstractly ("an item with two URLs")'
        ),
    ),
    (
        INSTANCE_TOKEN_RE,
        "instance content (an instance-directory path or dex-* name)",
        'describe it abstractly ("the config", "an enrichment file")',
    ),
)

_EXCERPT_CHARS = 40


class IssuePayloadError(ValueError):
    """An issue payload does not conform, or a public field leaks instance content."""


def observed_fingerprint(*, verb: str, expected: str, observed: str) -> str:
    """The dedup fingerprint for an observed report.

    Hashes the three identity fields under an ``observed`` domain prefix
    so it can never collide with an exception fingerprint. ``steps`` and
    ``note`` are excluded: repro detail and local prose churn without the
    defect changing.

    Args:
        verb: The misbehaving command, from :data:`KNOWN_VERBS`.
        expected: The expected-mechanics clause.
        observed: The observed-mechanics clause.

    Returns:
        The 12-hex fingerprint.
    """
    key = f"observed|{verb}|{expected}|{observed}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]  # noqa: S324 — dedup key, not a security context


def report_observed(  # noqa: PLR0913 — every input is injected, none ambient
    payload_path: Path,
    *,
    enabled: bool,
    state_dir: Path,
    engine_version: str,
    today: Callable[[], datetime.date],
    gh: issues.GhRunner,
) -> str:
    """File one session-observed report from its JSON payload.

    Validation first, the gate second: a malformed or leaky payload is
    refused with per-field reasons whether or not reporting is enabled —
    the payload contract holds either way. Past validation, the shared
    mechanism (:func:`.issues.file_reports`) owns everything: dedup
    against local memory and the tracker, the seen-again comment, the
    regression arithmetic, the memory record (which alone carries the
    ``note``), and the never-fatal gh edge. The gate stops upstream
    filing only — a gated report still lands in the local memory as a
    ``filed: false`` record, so the observation is never lost and never
    auto-refiled once the gate turns on.

    Args:
        payload_path: The JSON payload (conventionally under ``cache/``):
            ``{"verb", "expected", "observed"}``, ``steps`` and the
            local-only ``note`` optional.
        enabled: The ``report_issues`` config gate — gates what is sent
            to GitHub, never the local record.
        state_dir: The instance's ``state/`` — holds the local memory.
        engine_version: The running engine's version (injected).
        today: Injected clock.
        gh: The ``gh`` seam.

    Returns:
        A one-line account of what was filed, commented, skipped or
        refused-softly — filing is a side errand, so everything past
        validation is a stated outcome, never a raise.

    Raises:
        IssuePayloadError: The payload is not a JSON object, a key is
            missing, unknown or the wrong shape, a field breaks its
            bound, ``verb`` is outside the command vocabulary, or a
            public field trips a leak rejector. Nothing is filed and
            nothing is written.
        OSError: The payload file cannot be read.
    """
    payload = _load(payload_path)
    verb = _verb(payload_path, payload)
    expected = _clause(payload_path, payload, "expected")
    observed = _clause(payload_path, payload, "observed")
    steps = _steps(payload_path, payload)
    note = _note(payload_path, payload)
    _reject_leaks(
        payload_path,
        [("expected", expected), ("observed", observed)]
        + [(f"steps[{i}]", step) for i, step in enumerate(steps)],
    )
    fp = observed_fingerprint(verb=verb, expected=expected, observed=observed)

    def body(regression_of: int | None) -> str:
        return _body(
            verb=verb,
            expected=expected,
            observed=observed,
            steps=steps,
            fp=fp,
            engine_version=engine_version,
            date=today(),
            regression_of=regression_of,
        )

    filable = issues.Filable(
        fingerprint=fp,
        title=_title(verb=verb, expected=expected, observed=observed, fp=fp),
        summary=f"observed report on {verb}",
        body=body,
        note=note,
    )
    outcome = issues.file_reports(
        [filable],
        enabled=enabled,
        state_dir=state_dir,
        engine_version=engine_version,
        today=today,
        gh=gh,
    )
    return _render_outcome(outcome, fp=fp, note=note)


def _title(*, verb: str, expected: str, observed: str, fp: str) -> str:
    """The templated title: ``[observed] <verb>: expected …; observed … fp-…``.

    The fp suffix is the dedup search key, exactly as on exception
    titles; the bounds on both clauses keep the whole line under
    GitHub's title cap.
    """
    return f"[observed] {verb}: expected {expected}; observed {observed} fp-{fp}"


def _body(  # noqa: PLR0913 — the allowlist IS the parameter list; nothing else can get in
    *,
    verb: str,
    expected: str,
    observed: str,
    steps: list[str],
    fp: str,
    engine_version: str,
    date: datetime.date,
    regression_of: int | None,
) -> str:
    """The observed issue body — the validated fields, enumerated, nothing ambient.

    Same shape discipline as the exception body: every line below is a
    named field that survived the leak rejectors. The reporter's fuller
    ``note`` is not a field here at all — it lives only in the local
    ``issue-reports.jsonl`` record.
    """
    lines = [
        "Session-observed report from a dex instance. Structured mechanics",
        "only: each field was refused, not scrubbed, on any detected leak,",
        "and the reporter's fuller note never leaves the instance.",
        "",
    ]
    if regression_of is not None:
        lines += [
            f"Recurrence of #{regression_of} on a newer engine — possible regression.",
            "",
        ]
    lines += [
        f"- engine: {engine_version}",
        f"- verb: {verb}",
        f"- date: {date.isoformat()}",
        f"- expected: {expected}",
        f"- observed: {observed}",
        f"- fingerprint: fp-{fp}",
    ]
    if steps:
        lines += ["", "steps:"]
        lines += [f"{i}. {step}" for i, step in enumerate(steps, start=1)]
    return "\n".join(lines)


def _render_outcome(outcome: issues.FilerOutcome, *, fp: str, note: str | None) -> str:
    """The verb's one-line account of what the mechanism did."""
    if not outcome.notes:
        # The mechanism's silent branches: this instance already reported
        # or recorded the fingerprint at this engine, or the issue is
        # closed upstream with no newer-engine recurrence to claim.
        return (
            f"nothing filed: fp-{fp} is already reported at this engine or closed "
            "upstream — no new record written"
        )
    text = "; ".join(outcome.notes)
    if note is not None and (
        outcome.filed or outcome.commented or outcome.recorded or outcome.deferred
    ):
        text += " · note kept in state/issue-reports.jsonl (local only — the owner forwards it)"
    return text


# ---------------------------------------------------------------------------
# Payload validation. Every value is written by a session, so none is
# trusted for its type, and the message names the key that broke.
# ---------------------------------------------------------------------------


def _fail(payload_path: Path, message: str) -> NoReturn:
    raise IssuePayloadError(f"{payload_path}: {message}")


def _load(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise IssuePayloadError(f"{path}: invalid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise IssuePayloadError(f"{path}: expected a JSON object, got {type(raw).__name__}")
    missing = sorted(_REQUIRED_KEYS - set(raw))
    if missing:
        _fail(path, f"missing required key(s) {missing}")
    unknown = sorted(set(raw) - _REQUIRED_KEYS - _OPTIONAL_KEYS)
    if unknown:
        _fail(path, f"unknown key(s) {unknown}")
    return raw


def _verb(path: Path, payload: dict[str, object]) -> str:
    """The misbehaving command, validated against the CLI vocabulary."""
    value = payload["verb"]
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        _fail(path, f"verb must be a non-empty single-line string, got {value!r}")
    verb = value.strip()
    if verb not in KNOWN_VERBS:
        _fail(
            path,
            f"verb {verb!r} is not a dex command — one of: {', '.join(KNOWN_VERBS)}",
        )
    return verb


def _clause(path: Path, payload: dict[str, object], key: str) -> str:
    """One mechanics clause: required, single-line, bounded."""
    value = payload[key]
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        _fail(path, f"{key} must be a non-empty single-line string, got {value!r}")
    clause = value.strip()
    if len(clause) > MAX_CLAUSE_CHARS:
        _fail(
            path,
            f"{key} is {len(clause)} characters — one mechanics sentence, "
            f"at most {MAX_CLAUSE_CHARS}",
        )
    return clause


def _steps(path: Path, payload: dict[str, object]) -> list[str]:
    """The optional repro steps: a short list of bounded mechanics statements."""
    value = payload.get("steps", [])
    if not isinstance(value, list):
        _fail(path, f"steps must be a list of strings, got {type(value).__name__}")
    if len(value) > MAX_STEPS:
        _fail(path, f"steps holds {len(value)} entries — at most {MAX_STEPS}")
    steps: list[str] = []
    for i, step in enumerate(value):
        if not isinstance(step, str) or not step.strip() or "\n" in step:
            _fail(path, f"steps[{i}] must be a non-empty single-line string, got {step!r}")
        stripped = step.strip()
        if len(stripped) > MAX_STEP_CHARS:
            _fail(
                path,
                f"steps[{i}] is {len(stripped)} characters — one mechanics "
                f"statement, at most {MAX_STEP_CHARS}",
            )
        steps.append(stripped)
    return steps


def _note(path: Path, payload: dict[str, object]) -> str | None:
    """The local-only note: optional free text, bounded, never leak-checked."""
    value = payload.get("note")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(path, f"note must be a non-empty string when present, got {value!r}")
    note = value.strip()
    if len(note) > MAX_NOTE_CHARS:
        _fail(path, f"note is {len(note)} characters — at most {MAX_NOTE_CHARS}")
    return note


def _reject_leaks(path: Path, fields: list[tuple[str, str]]) -> None:
    """Refuse the payload when any public field trips a scrubber detector.

    ALL fields are scanned before refusing, so the session gets every
    reason at once — abstract, rewrite, retry. One reason per field (the
    first detector to hit names the problem most precisely).
    """
    reasons: list[str] = []
    for name, text in fields:
        for pattern, what, fix in _REJECTORS:
            match = pattern.search(text)
            if match is not None:
                reasons.append(f"{name}: contains {what} ({_excerpt(match.group(0))!r}) — {fix}")
                break
    if reasons:
        _fail(
            path,
            "a public field leaks instance content, and this verb refuses "
            "rather than scrubs:\n"
            + "\n".join(f"  - {reason}" for reason in reasons)
            + "\nAbstract the mechanics and run the verb again; the fuller "
            "detail belongs in the local-only note field.",
        )


def _excerpt(matched: str) -> str:
    """The matched leak, truncated — enough to find it, not a transcript."""
    if len(matched) <= _EXCERPT_CHARS:
        return matched
    return matched[: _EXCERPT_CHARS - 1] + "…"
