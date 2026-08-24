"""The issue filer: decentralized self-healing, sanitized by construction.

Instances auto-file engine bugs at the public engine repo; the owner's
session fixes; Mint releases; every instance heals at next sync. One
mechanism, two producers: the exception path here fires only on
``status: error`` outcomes — the world misbehaving
(blocked/dead/manual/waiting) is never an engine bug — and the observed
path (:mod:`.observed`, the ``dex issue`` verb) files what a session SAW
misbehave without anything raising. Both reduce to a :class:`Filable` and
go through :func:`file_reports`: the same repo, dedup arithmetic, local
memory, rate limit and soft edge. The ``report_issues`` gate stops
upstream filing only — a gated report still lands in the local memory as
a ``filed: false`` record, which dedupe treats like any other, so turning
the gate on later never auto-refiles old observations. The rate limit
likewise caps upstream filings only: a capped report is recorded locally
as ``action: deferred`` — the one record the re-spam guard ignores, so
the observation is never lost and files the next time it is seen.

Everything the exception path sends is allowlisted: engine version,
command, kind/format enums, error class, errno where present,
engine-frames-only traceback, the URL *hash* — and NO free text at all.
The exception message is deliberately absent from issue bodies (the trade,
ruled: zero residual leak risk beats remote diagnostics; the scrubbed
message stays in the local ledger's ``error`` field, so diagnosis happens
where the data lives). The sanitizer is code, so it cannot leak by
judgment lapse. The observed path holds the same line by rejection
instead: its module refuses any payload the scrubber's detectors would
touch, so nothing needing scrubbing ever reaches the mechanism.

Fire-and-forget: the filer never polls issues or acts on tracker content —
search results feed dedup arithmetic only, and the fix loop closes through
releases and sync. ``gh`` is assumed (a declared instance dependency); the
filer's only soft edge is that its own failures are wrapped non-fatal — the
run report notes "issue filing failed" and the run continues.
"""

import datetime
import hashlib
import json
import subprocess
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .classify import scrub
from .types import Format, Kind, version_newer

__all__ = [
    "ENGINE_REPO",
    "MAX_NEW_ISSUES_PER_RUN",
    "ErrorEvent",
    "Filable",
    "FilerOutcome",
    "GhRunner",
    "error_event",
    "file_reports",
    "fingerprint",
    "gh_runner",
    "report_errors",
]

# The public engine repo issues are filed against. Instances are
# private; this is the one shared tracker.
ENGINE_REPO = "leeovery/dex"

# Rate limit: a run that suddenly errors everywhere must not flood the
# tracker — three distinct new bugs is signal enough for one run. It caps
# UPSTREAM filings only: local memory records always write (per-run and
# per-engine fingerprint dedupe already bounds their growth), because a
# cap on the local record silently dropped every observation past it.
MAX_NEW_ISSUES_PER_RUN = 3

# Local memory: what this instance filed or commented — re-spam
# prevention and the owner's visible record. Append-only JSONL, merge=union.
MEMORY_FILE = "issue-reports.jsonl"

_PACKAGE_MARKER = "dex_engine"


GhRunner = Callable[[Sequence[str]], str]
"""The injected ``gh`` seam: run ``gh <args>``, return stdout, raise on failure."""


def gh_runner(args: Sequence[str]) -> str:
    """The production ``gh`` seam.

    Args:
        args: Arguments after ``gh``.

    Returns:
        The command's stdout.

    Raises:
        OSError: ``gh`` is not installed (not a dex environment).
        subprocess.SubprocessError: ``gh`` exited nonzero or timed out.
    """
    completed = subprocess.run(  # noqa: S603 — engine-built args, no shell
        ["gh", *args],  # noqa: S607 — gh resolves via PATH like every dev tool
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return completed.stdout


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorEvent:
    """One ``status: error`` outcome, captured at the catch site.

    Built while the exception object is still in hand — the fingerprint
    needs the traceback's innermost engine frame. Carries NO free text:
    the exception message never leaves the instance (it lives in the local
    ledger's ``error`` field); ``errno`` is the one mechanical detail kept.
    """

    unit_hash: str
    kind: Kind
    format: Format | None
    error_class: str
    function: str  # innermost engine frame's function name
    errno: int | None  # OSError family only — mechanical, never prose
    frames: str  # engine-frames-only traceback, package-relative paths


@dataclass(frozen=True, slots=True, kw_only=True)
class Filable:
    """One producer-shaped report, ready for the shared filing mechanism.

    Both producers (§13) reduce to this: :func:`report_errors` wraps each
    :class:`ErrorEvent`, and the observed verb wraps its validated
    payload. ``body`` takes the regression reference as its argument
    because only the mechanism knows, at file time, whether the filing is
    a recurrence. ``note`` is the local-only free text: it lands in the
    memory record for the owner and NEVER in any ``gh`` argument.
    """

    fingerprint: str
    title: str
    summary: str  # the run-report note's label: what misbehaved, where
    body: Callable[[int | None], str]  # regression_of -> the issue body
    note: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FilerOutcome:
    """What the filer did this run — feeds the run report.

    ``recorded`` counts the gate-off outcomes: reports written to the
    local memory only, with nothing sent upstream. ``deferred`` counts
    the rate-limited ones — recorded locally this run, eligible to file
    the next time they are observed.
    """

    filed: int = 0
    commented: int = 0
    recorded: int = 0
    deferred: int = 0
    notes: list[str] = field(default_factory=list)


def error_event(
    *, unit_hash: str, kind: Kind, unit_format: Format | None, exc: BaseException
) -> ErrorEvent:
    """Capture an error outcome for the filer, sanitized by construction.

    Args:
        unit_hash: The work unit's ledger hash — the URL is never sent, its
            hash is.
        kind: The unit's kind.
        unit_format: The unit's format, for file work.
        exc: The caught exception, traceback attached.

    Returns:
        The event.
    """
    engine_frames = [
        frame
        for frame in traceback.extract_tb(exc.__traceback__)
        if _PACKAGE_MARKER in frame.filename
    ]
    frames = "\n".join(
        f"{_package_path(frame.filename)}:{frame.lineno} in {frame.name}" for frame in engine_frames
    )
    return ErrorEvent(
        unit_hash=unit_hash,
        kind=kind,
        format=unit_format,
        error_class=type(exc).__name__,
        function=engine_frames[-1].name if engine_frames else "unknown",
        errno=exc.errno if isinstance(exc, OSError) else None,
        frames=frames or "no engine frames",
    )


def _package_path(filename: str) -> str:
    """The frame path from the package root down — never the machine's prefix."""
    _, marker, rest = filename.rpartition(_PACKAGE_MARKER)
    if marker:
        return marker + rest
    return scrub(filename)


def fingerprint(event: ErrorEvent) -> str:
    """The dedup fingerprint: error class + function + kind/format.

    Version and line numbers are excluded — they churn per release and would
    mint duplicate issues; version data lives in the issue body instead.
    """
    fmt = event.format.value if event.format is not None else ""
    key = f"{event.error_class}|{event.function}|{event.kind.value}|{fmt}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]  # noqa: S324 — dedup key, not a security context


# ---------------------------------------------------------------------------
# Local memory: state/issue-reports.jsonl (append-only,
# full-record lines, merge=union between machines).
# ---------------------------------------------------------------------------


# The one action that never suppresses a future filing. "recorded" is the
# owner's choice not to file (report_issues: false) and suppresses forever;
# "deferred" is THIS RUN's rate limit, so the observation stays eligible
# the next time it is seen. Distinguished by action, not by a second flag:
# both are `filed: false`, and the difference is what the record MEANS.
_DEFERRED = "deferred"


@dataclass(frozen=True, slots=True, kw_only=True)
class _MemoryRecord:
    fingerprint: str
    # "filed" | "commented" | "recorded" (gate off: local only, never
    # refiled) | "deferred" (rate-limited this run: local only, refiles
    # when the observation recurs)
    action: str
    engine: str
    date: str
    # Whether anything reached GitHub for this record. False is the
    # report_issues:false record — observed locally, nothing sent — and
    # dedupe reads records regardless of it: turning the gate on later
    # must not auto-refile what was already seen. Absent on records that
    # predate the field, which were all upstream actions.
    filed: bool = True
    issue: int | None = None
    # The observed verb's local-only free text. THE SPLIT: the note is for
    # the owner to read here and forward by hand — it is never part of any
    # filed title or body, which is why it may name items, paths and URLs
    # the public fields must not.
    note: str | None = None


def _memory_path(state_dir: Path) -> Path:
    return state_dir / MEMORY_FILE


def _read_memory(path: Path) -> list[_MemoryRecord]:
    records: list[_MemoryRecord] = []
    if not path.exists():
        return records
    # Newline-delimited alone — splitlines() would shear records on unicode
    # separators inside JSON strings (the standing state-JSONL rule).
    for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError(f"{path.name}:{lineno}: issue-reports record is not an object")
        # Everything raised here must be in the filer's never-fatal catch
        # set — a torn-but-valid-JSON record (union merges, hand damage)
        # must degrade to an "issue filing failed" note, never kill the run.
        missing = [key for key in ("fingerprint", "action", "engine", "date") if key not in raw]
        if missing:
            raise ValueError(
                f"{path.name}:{lineno}: issue-reports record missing field(s) {missing}"
            )
        issue = raw.get("issue")
        note = raw.get("note")
        filed = raw.get("filed")
        records.append(
            _MemoryRecord(
                fingerprint=str(raw["fingerprint"]),
                action=str(raw["action"]),
                engine=str(raw["engine"]),
                date=str(raw["date"]),
                # Records that predate the field were all upstream actions.
                filed=filed if isinstance(filed, bool) else True,
                issue=issue if isinstance(issue, int) else None,
                note=note if isinstance(note, str) else None,
            )
        )
    return records


def _append_memory(path: Path, record: _MemoryRecord) -> None:
    raw: dict[str, str | int | bool] = {
        "fingerprint": record.fingerprint,
        "action": record.action,
        "engine": record.engine,
        "date": record.date,
        "filed": record.filed,
    }
    if record.issue is not None:
        raw["issue"] = record.issue
    if record.note is not None:
        raw["note"] = record.note
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(raw, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# The gh conversation: search before filing, dedup by state.
# ---------------------------------------------------------------------------


def _search(gh: GhRunner, fp: str) -> list[dict[str, object]]:
    out = gh(
        [
            "issue",
            "list",
            "--repo",
            ENGINE_REPO,
            "--state",
            "all",
            "--search",
            f"fp-{fp} in:title",
            "--json",
            "number,state",
            "--limit",
            "20",
        ]
    )
    found = json.loads(out or "[]")
    if not isinstance(found, list):
        raise ValueError(f"gh issue list returned non-list JSON: {out!r}")
    return [entry for entry in found if isinstance(entry, dict)]


def _title(event: ErrorEvent, fp: str) -> str:
    fmt = f"/{event.format.value}" if event.format is not None else ""
    return f"[auto] {event.error_class} in {event.function} ({event.kind.value}{fmt}) fp-{fp}"


def _body(  # noqa: PLR0913 — the allowlist IS the parameter list; nothing else can get in
    event: ErrorEvent,
    fp: str,
    *,
    engine_version: str,
    command: str,
    date: datetime.date,
    regression_of: int | None = None,
) -> str:
    """The allowlisted issue body — every field enumerated, nothing ambient.

    No free text, by construction: the exception message is not a field
    here at all. Diagnosis works from the error class, errno, and engine
    frames; the message lives in the reporting instance's ledger.
    """
    lines = [
        "Automated report from a dex instance. Sanitized by construction:",
        "allowlisted fields only — no free text leaves the instance (the",
        "exception message stays in its local ledger).",
        "",
    ]
    if regression_of is not None:
        lines += [
            f"Recurrence of #{regression_of} on a newer engine — possible regression.",
            "",
        ]
    lines += [
        f"- engine: {engine_version}",
        f"- command: {command}",
        f"- date: {date.isoformat()}",
        f"- kind: {event.kind.value}",
        f"- format: {event.format.value if event.format is not None else '—'}",
        f"- error: {event.error_class}",
    ]
    if event.errno is not None:
        lines.append(f"- errno: {event.errno}")
    lines += [
        f"- url hash: {event.unit_hash}",
        f"- fingerprint: fp-{fp}",
        "",
        "engine frames:",
        "```",
        event.frames,
        "```",
    ]
    return "\n".join(lines)


def _issue_number(create_output: str) -> int | None:
    """The issue number from ``gh issue create`` output (the issue URL)."""
    tail = create_output.strip().rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


@dataclass(slots=True, kw_only=True)
class _Filer:
    """One filing pass over producer-shaped reports — internal to :func:`file_reports`.

    ``enabled`` is the ``report_issues`` gate, and it gates UPSTREAM only:
    with it off, ``gh`` is never consulted and each new report becomes a
    local ``filed: false`` memory record instead — the same memory dedupe
    reads, so turning the gate on later never auto-refiles what was
    already observed.
    """

    gh: GhRunner
    memory_path: Path
    engine_version: str
    today: Callable[[], datetime.date]
    enabled: bool = True
    memory: list[_MemoryRecord] = field(default_factory=list)
    filed: int = 0
    commented: int = 0
    recorded: int = 0
    deferred: int = 0
    notes: list[str] = field(default_factory=list)

    def handle(self, filable: Filable) -> None:
        fp = filable.fingerprint
        if any(
            record.fingerprint == fp
            and record.engine == self.engine_version
            and record.action != _DEFERRED
            for record in self.memory
        ):
            # Already reported at this engine version — the re-spam guard.
            # A deferred record is the one exception: it says "rate-limited
            # that run", never "settled", so the observation stays eligible.
            return
        if not self.enabled:
            self._record_local(filable)
            return
        found = _search(self.gh, fp)
        open_issues = [e for e in found if str(e.get("state", "")).upper() == "OPEN"]
        closed_issues = [e for e in found if str(e.get("state", "")).upper() == "CLOSED"]
        if open_issues:
            self._comment(filable, open_issues[0])
            return
        if closed_issues:
            self._handle_closed(filable, closed_issues[0])
            return
        self._file(filable, regression_of=None)

    def _comment(self, filable: Filable, issue: dict[str, object]) -> None:
        number = issue.get("number")
        if not isinstance(number, int):
            raise ValueError(f"gh issue list entry has no integer number: {issue!r}")
        self.gh(
            [
                "issue",
                "comment",
                str(number),
                "--repo",
                ENGINE_REPO,
                "--body",
                f"seen again (engine {self.engine_version}, {self.today().isoformat()})",
            ]
        )
        self.commented += 1
        self._remember(filable, action="commented", issue=number)
        self.notes.append(f"engine issue #{number}: seen-again comment added")

    def _handle_closed(self, filable: Filable, issue: dict[str, object]) -> None:
        """Closed-issue dedup: silent unless this is a newer-engine recurrence.

        The instance never reads tracker content, so "predates the fix" is
        judged from its own memory: a prior report of this fingerprint at a
        strictly older engine means the bug came back after the engine moved
        forward — a regression worth a fresh issue referencing the old one.
        No local baseline (another instance filed it) stays silent — sync
        cures the ordinary case, and guessing files noise.
        """
        prior = [record for record in self.memory if record.fingerprint == filable.fingerprint]
        recurred_newer = any(
            _safe_version_newer(self.engine_version, record.engine) for record in prior
        )
        if not recurred_newer:
            return
        number = issue.get("number")
        self._file(filable, regression_of=number if isinstance(number, int) else None)

    def _file(self, filable: Filable, *, regression_of: int | None) -> None:
        if self._rate_limited():
            self._defer(filable)
            return
        out = self.gh(
            [
                "issue",
                "create",
                "--repo",
                ENGINE_REPO,
                "--title",
                filable.title,
                "--body",
                filable.body(regression_of),
            ]
        )
        self.filed += 1
        number = _issue_number(out)
        self._remember(filable, action="filed", issue=number)
        label = f"#{number}" if number is not None else f"fp-{filable.fingerprint}"
        self.notes.append(f"filed engine issue {label}: {filable.summary}")

    def _record_local(self, filable: Filable) -> None:
        """The gate-off outcome: the memory record, and nothing upstream.

        Never rate-limited: the cap protects the shared tracker, and a
        local record costs nobody anything — capping it silently dropped
        every observation past the third in a gate-off run.
        """
        self.recorded += 1
        self._remember(filable, action="recorded", issue=None, filed=False)
        self.notes.append(
            f"recorded locally: {filable.summary} — issue reporting is off "
            "(report_issues: false), nothing filed upstream"
        )

    def _defer(self, filable: Filable) -> None:
        """The rate-limited outcome: recorded locally, eligible again next run.

        The record is ``action: deferred`` — the one action the re-spam
        guard ignores — so the observation is not lost AND not settled: it
        files the next time it is seen, budget allowing. At most one
        deferred record per (fingerprint, engine): a second deferral of
        the same observation adds nothing but a line.
        """
        self.deferred += 1
        already = any(
            record.fingerprint == filable.fingerprint
            and record.engine == self.engine_version
            and record.action == _DEFERRED
            for record in self.memory
        )
        if not already:
            self._remember(filable, action=_DEFERRED, issue=None, filed=False)
        self.notes.append(
            f"rate limit reached ({MAX_NEW_ISSUES_PER_RUN} new issues per run) — "
            f"{filable.summary} recorded locally; it files when next observed"
        )

    def _rate_limited(self) -> bool:
        """Whether this run's upstream budget is spent — filings alone count."""
        return self.filed >= MAX_NEW_ISSUES_PER_RUN

    def _remember(
        self, filable: Filable, *, action: str, issue: int | None, filed: bool = True
    ) -> None:
        record = _MemoryRecord(
            fingerprint=filable.fingerprint,
            action=action,
            engine=self.engine_version,
            date=self.today().isoformat(),
            filed=filed,
            issue=issue,
            note=filable.note,
        )
        _append_memory(self.memory_path, record)
        self.memory.append(record)


def _safe_version_newer(candidate: str, baseline: str) -> bool:
    try:
        return version_newer(candidate, baseline)
    except ValueError:
        return False  # an unparseable stored engine can't prove a regression


def file_reports(  # noqa: PLR0913 — every input is injected, none ambient
    filables: Sequence[Filable],
    *,
    enabled: bool,
    state_dir: Path,
    engine_version: str,
    today: Callable[[], datetime.date],
    gh: GhRunner,
) -> FilerOutcome:
    """The one filing mechanism, shared by both producers.

    Dedup'd (local memory first, then a title search; open issues take a
    seen-again comment, closed ones stay silent unless the recurrence is
    on a newer engine), rate-limited upstream — a capped filing is
    recorded locally as deferred and stays eligible for the next run it
    is observed on; local records themselves always write — remembered in
    ``state/issue-reports.jsonl``, and never fatal.

    The gate stops UPSTREAM filing only: with ``enabled`` false, ``gh``
    is never invoked and each new report still lands in the local memory
    as a ``filed: false`` record. Dedupe and the per-run rate limit read
    any record as seen, so turning the gate on later never auto-refiles
    old observations.

    Args:
        filables: The producer-shaped reports to file.
        enabled: The ``report_issues`` config gate (default true) — gates
            what is sent to GitHub, never the local record.
        state_dir: The instance's ``state/`` — holds the local memory.
        engine_version: The running engine's version (injected).
        today: Injected clock.
        gh: The ``gh`` seam.

    Returns:
        What was filed, commented and recorded locally, plus report
        notes. Filer failures are wrapped non-fatal: the outcome notes
        "issue filing failed" and the caller continues — the local state
        already holds the truth.
    """
    if not filables:
        return FilerOutcome()
    filer = _Filer(
        gh=gh,
        memory_path=_memory_path(state_dir),
        engine_version=engine_version,
        today=today,
        enabled=enabled,
    )
    try:
        filer.memory = _read_memory(filer.memory_path)
        for filable in filables:
            filer.handle(filable)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        # The filer's one soft edge: gh missing/failing, unparseable output, a torn
        # memory line. Never fatal — noted, and the run continues.
        filer.notes.append(f"issue filing failed ({scrub(str(e))}) — run continues")
    return FilerOutcome(
        filed=filer.filed,
        commented=filer.commented,
        recorded=filer.recorded,
        deferred=filer.deferred,
        notes=filer.notes,
    )


def _error_filable(
    event: ErrorEvent,
    *,
    engine_version: str,
    command: str,
    today: Callable[[], datetime.date],
) -> Filable:
    """The exception producer: one error event as the mechanism's input."""
    fp = fingerprint(event)

    def body(regression_of: int | None) -> str:
        return _body(
            event,
            fp,
            engine_version=engine_version,
            command=command,
            date=today(),
            regression_of=regression_of,
        )

    return Filable(
        fingerprint=fp,
        title=_title(event, fp),
        summary=f"{event.error_class} in {event.function}",
        body=body,
    )


def report_errors(  # noqa: PLR0913 — every input is injected, none ambient
    events: Sequence[ErrorEvent],
    *,
    enabled: bool,
    state_dir: Path,
    engine_version: str,
    command: str,
    today: Callable[[], datetime.date],
    gh: GhRunner,
) -> FilerOutcome:
    """File this run's error outcomes upstream, dedup'd and rate-limited.

    Args:
        events: The run's error events (one per errored unit).
        enabled: The ``report_issues`` config gate (default true) — gates
            what is sent to GitHub, never the local record.
        state_dir: The instance's ``state/`` — holds the local memory.
        engine_version: The running engine's version (injected).
        command: The CLI command that ran, for the issue body.
        today: Injected clock.
        gh: The ``gh`` seam.

    Returns:
        What was filed, plus report notes. Filer failures are wrapped
        non-fatal: the outcome notes "issue filing failed" and the run
        continues — the ledger already holds the truth.
    """
    seen: set[str] = set()
    unique: list[ErrorEvent] = []
    for event in events:
        fp = fingerprint(event)
        if fp not in seen:
            seen.add(fp)
            unique.append(event)
    filables = [
        _error_filable(event, engine_version=engine_version, command=command, today=today)
        for event in unique
    ]
    return file_reports(
        filables,
        enabled=enabled,
        state_dir=state_dir,
        engine_version=engine_version,
        today=today,
        gh=gh,
    )
