"""The issue filer (§13): decentralized self-healing, sanitized by construction.

Instances auto-file engine bugs at the public engine repo; the owner's
session fixes; Mint releases; every instance heals at next sync. The filer
fires only on ``status: error`` outcomes — the world misbehaving
(blocked/dead/manual/waiting) is never an engine bug.

Everything that leaves the instance is allowlisted: engine version, command,
kind/format enums, error class, engine-frames-only traceback, the URL
*hash*; free text passes through the shared :func:`~.classify.scrub`. The
sanitizer is code, so it cannot leak by judgment lapse.

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
    "FilerOutcome",
    "GhRunner",
    "error_event",
    "fingerprint",
    "gh_runner",
    "report_errors",
]

# The public engine repo issues are filed against (§13). Instances are
# private; this is the one shared tracker.
ENGINE_REPO = "leeovery/dex"

# Rate limit (§13): a run that suddenly errors everywhere must not flood the
# tracker — three distinct new bugs is signal enough for one run.
MAX_NEW_ISSUES_PER_RUN = 3

# Local memory (§4/§13): what this instance filed or commented — re-spam
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
        OSError: ``gh`` is not installed (not a dex environment, §13).
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
    """One ``status: error`` outcome, captured at the catch site (§13).

    Built while the exception object is still in hand — the ledger stores
    only the scrubbed message, and the fingerprint needs the traceback's
    innermost engine frame.
    """

    unit_hash: str
    kind: Kind
    format: Format | None
    error_class: str
    function: str  # innermost engine frame's function name
    message: str  # scrubbed
    frames: str  # engine-frames-only traceback, package-relative paths


@dataclass(frozen=True, slots=True, kw_only=True)
class FilerOutcome:
    """What the filer did this run — feeds the run report (§13)."""

    filed: int = 0
    notes: list[str] = field(default_factory=list)


def error_event(
    *, unit_hash: str, kind: Kind, unit_format: Format | None, exc: BaseException
) -> ErrorEvent:
    """Capture an error outcome for the filer, sanitized by construction.

    Args:
        unit_hash: The work unit's ledger hash — the URL is never sent, its
            hash is (§13).
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
        f"{_package_path(frame.filename)}:{frame.lineno} in {frame.name}"
        for frame in engine_frames
    )
    if isinstance(exc, OSError):
        # OSError messages embed the offending FILENAME (str(exc) renders
        # `[Errno n] reason: 'path'`) — class + errno carry the diagnosis;
        # the filename never leaves the instance.
        errno = f": errno {exc.errno}" if exc.errno is not None else ""
        message = f"{type(exc).__name__}{errno}"
    else:
        message = scrub(f"{type(exc).__name__}: {exc}")
    return ErrorEvent(
        unit_hash=unit_hash,
        kind=kind,
        format=unit_format,
        error_class=type(exc).__name__,
        function=engine_frames[-1].name if engine_frames else "unknown",
        message=message,
        frames=frames or "no engine frames",
    )


def _package_path(filename: str) -> str:
    """The frame path from the package root down — never the machine's prefix."""
    _, marker, rest = filename.rpartition(_PACKAGE_MARKER)
    if marker:
        return marker + rest
    return scrub(filename)


def fingerprint(event: ErrorEvent) -> str:
    """The dedup fingerprint: error class + function + kind/format (§13).

    Version and line numbers are excluded — they churn per release and would
    mint duplicate issues; version data lives in the issue body instead.
    """
    fmt = event.format.value if event.format is not None else ""
    key = f"{event.error_class}|{event.function}|{event.kind.value}|{fmt}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]  # noqa: S324 — dedup key, not a security context


# ---------------------------------------------------------------------------
# Local memory: state/issue-reports.jsonl (§4 JSONL rules — append-only,
# full-record lines, merge=union between machines).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class _MemoryRecord:
    fingerprint: str
    action: str  # "filed" | "commented"
    engine: str
    date: str
    issue: int | None = None


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
        missing = [
            key for key in ("fingerprint", "action", "engine", "date") if key not in raw
        ]
        if missing:
            raise ValueError(
                f"{path.name}:{lineno}: issue-reports record missing field(s) {missing}"
            )
        issue = raw.get("issue")
        records.append(
            _MemoryRecord(
                fingerprint=str(raw["fingerprint"]),
                action=str(raw["action"]),
                engine=str(raw["engine"]),
                date=str(raw["date"]),
                issue=issue if isinstance(issue, int) else None,
            )
        )
    return records


def _append_memory(path: Path, record: _MemoryRecord) -> None:
    raw: dict[str, str | int] = {
        "fingerprint": record.fingerprint,
        "action": record.action,
        "engine": record.engine,
        "date": record.date,
    }
    if record.issue is not None:
        raw["issue"] = record.issue
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(raw, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# The gh conversation: search before filing, dedup by state (§13).
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
    """The allowlisted issue body (§13) — every field enumerated, nothing ambient."""
    lines = [
        "Automated report from a dex instance (design §13). Sanitized by",
        "construction: allowlisted fields only; messages scrubbed of URLs,",
        "emails, and home paths.",
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
        f"- url hash: {event.unit_hash}",
        f"- fingerprint: fp-{fp}",
        "",
        f"message: {event.message}",
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
    """One run's filing pass — internal to :func:`report_errors`."""

    gh: GhRunner
    memory_path: Path
    engine_version: str
    command: str
    today: Callable[[], datetime.date]
    memory: list[_MemoryRecord] = field(default_factory=list)
    filed: int = 0
    notes: list[str] = field(default_factory=list)

    def handle(self, event: ErrorEvent) -> None:
        fp = fingerprint(event)
        if any(
            record.fingerprint == fp and record.engine == self.engine_version
            for record in self.memory
        ):
            return  # already reported at this engine version (§13 re-spam guard)
        found = _search(self.gh, fp)
        open_issues = [e for e in found if str(e.get("state", "")).upper() == "OPEN"]
        closed_issues = [e for e in found if str(e.get("state", "")).upper() == "CLOSED"]
        if open_issues:
            self._comment(fp, open_issues[0])
            return
        if closed_issues:
            self._handle_closed(event, fp, closed_issues[0])
            return
        self._file(event, fp, regression_of=None)

    def _comment(self, fp: str, issue: dict[str, object]) -> None:
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
        self._remember(fp, action="commented", issue=number)
        self.notes.append(f"engine issue #{number}: seen-again comment added")

    def _handle_closed(self, event: ErrorEvent, fp: str, issue: dict[str, object]) -> None:
        """Closed-issue dedup (§13): silent unless this is a newer-engine recurrence.

        The instance never reads tracker content, so "predates the fix" is
        judged from its own memory: a prior report of this fingerprint at a
        strictly older engine means the bug came back after the engine moved
        forward — a regression worth a fresh issue referencing the old one.
        No local baseline (another instance filed it) stays silent — sync
        cures the ordinary case, and guessing files noise.
        """
        prior = [record for record in self.memory if record.fingerprint == fp]
        recurred_newer = any(
            _safe_version_newer(self.engine_version, record.engine) for record in prior
        )
        if not recurred_newer:
            return
        number = issue.get("number")
        self._file(event, fp, regression_of=number if isinstance(number, int) else None)

    def _file(self, event: ErrorEvent, fp: str, *, regression_of: int | None) -> None:
        if self.filed >= MAX_NEW_ISSUES_PER_RUN:
            return  # the rate limit (§13); the fingerprint retries next run
        out = self.gh(
            [
                "issue",
                "create",
                "--repo",
                ENGINE_REPO,
                "--title",
                _title(event, fp),
                "--body",
                _body(
                    event,
                    fp,
                    engine_version=self.engine_version,
                    command=self.command,
                    date=self.today(),
                    regression_of=regression_of,
                ),
            ]
        )
        self.filed += 1
        number = _issue_number(out)
        self._remember(fp, action="filed", issue=number)
        label = f"#{number}" if number is not None else f"fp-{fp}"
        self.notes.append(
            f"filed engine issue {label}: {event.error_class} in {event.function}"
        )

    def _remember(self, fp: str, *, action: str, issue: int | None) -> None:
        record = _MemoryRecord(
            fingerprint=fp,
            action=action,
            engine=self.engine_version,
            date=self.today().isoformat(),
            issue=issue,
        )
        _append_memory(self.memory_path, record)
        self.memory.append(record)


def _safe_version_newer(candidate: str, baseline: str) -> bool:
    try:
        return version_newer(candidate, baseline)
    except ValueError:
        return False  # an unparseable stored engine can't prove a regression


def report_errors(  # noqa: PLR0913 — every §13 input is injected, none ambient (§14)
    events: Sequence[ErrorEvent],
    *,
    enabled: bool,
    state_dir: Path,
    engine_version: str,
    command: str,
    today: Callable[[], datetime.date],
    gh: GhRunner,
) -> FilerOutcome:
    """File this run's error outcomes upstream, dedup'd and rate-limited (§13).

    Args:
        events: The run's error events (one per errored unit).
        enabled: The ``report_issues`` config gate (default true, §13).
        state_dir: The instance's ``state/`` — holds the local memory.
        engine_version: The running engine's version (injected, §14).
        command: The CLI command that ran, for the issue body.
        today: Injected clock (§14).
        gh: The ``gh`` seam.

    Returns:
        What was filed, plus report notes. Filer failures are wrapped
        non-fatal: the outcome notes "issue filing failed" and the run
        continues — the ledger already holds the truth.
    """
    if not enabled or not events:
        return FilerOutcome()
    seen: set[str] = set()
    unique: list[ErrorEvent] = []
    for event in events:
        fp = fingerprint(event)
        if fp not in seen:
            seen.add(fp)
            unique.append(event)
    filer = _Filer(
        gh=gh,
        memory_path=_memory_path(state_dir),
        engine_version=engine_version,
        command=command,
        today=today,
    )
    try:
        filer.memory = _read_memory(filer.memory_path)
        for event in unique:
            filer.handle(event)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        # The §13 soft edge: gh missing/failing, unparseable output, a torn
        # memory line. Never fatal — noted, and the run continues.
        filer.notes.append(f"issue filing failed ({scrub(str(e))}) — run continues")
    return FilerOutcome(filed=filer.filed, notes=filer.notes)
