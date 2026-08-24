"""Ledger persistence: the single serialization boundary for LedgerEntry.

``state/enrichment-ledger.jsonl`` is append-only, full-record lines,
latest-per-hash wins. Superseded lines are the audit trail until
``compact`` rewrites the file keeping only the latest line per hash.

"Latest" is decided by the ``at`` write timestamp, not by file position:
git's union merge driver concatenates ours-then-theirs, so after two
machines merge, the last line of a hash is whichever side git appended, not
whichever machine wrote later. A line without ``at`` sorts oldest — every
writer stamps one, so an unstamped line necessarily predates every stamped
one — and lines that tie (both unstamped, or the same instant) fall back to
file position.

``at`` is the writing machine's wall clock, and a wall clock can be wrong.
Two guards bound what a wrong one costs: it is set by the writer alone
(:func:`stamp`, from an injected clock) and never carried in from a caller,
and a stored value further ahead than :data:`FUTURE_SKEW_ALLOWANCE` is read
as no timestamp at all (:func:`resolution_key`).

Stamping never reads the ambient clock: today's date, the write instant,
and the engine version are injected so stamping and retry-on-new-engine are
testable. Resolution has to compare a stored ``at`` against the present, so
it does read a clock — injectable, defaulting to the wall clock, because
every reader resolves and not every reader has a clock to hand.
"""

import datetime
import json
import os
from collections.abc import Callable, Collection
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from dex_engine import atomic

from .types import Cap, Format, Job, Kind, LedgerEntry, LineageError, Need, Status

__all__ = [
    "FUTURE_SKEW_ALLOWANCE",
    "RENAMED_KINDS",
    "RETIRED_STATUSES",
    "LedgerSchemaError",
    "append",
    "compact",
    "drop_items",
    "from_line",
    "load",
    "resolution_key",
    "stamp",
    "to_line",
]


class LedgerSchemaError(ValueError):
    """A ledger line does not conform to the current schema.

    Raised at the serialization boundary instead of surfacing as a
    ``KeyError`` three stages later, and the message carries the repair
    that fits the fault: a line that is not JSON at all is a TORN line —
    an interrupted append, which no migration mends — and names the
    sanctioned deletion; a valid-JSON line breaking the schema names the
    likely missing migration.
    """


# Pre-rename vocabulary — the one spelling of what the old engine wrote.
# Migration 1 translates FROM these tables and this boundary hints off the
# same objects, so the loud error can never name a word the migration does
# not fix, nor miss one it does. Recognized here only to name that
# migration in the error; never silently accepted (no aliases).
RENAMED_KINDS = {"tweet": "x", "blog": "web"}
RETIRED_STATUSES = frozenset({"nocaptions", "toolong"})

_MIGRATION_HINT = (
    "migration 1 (renames + status vocabulary) has likely not been applied — run `bin/dex sync`"
)
_REQUIRED_KEYS = ("hash", "url", "item", "kind", "status", "engine", "date")
_ALL_KEYS = frozenset(
    (
        *_REQUIRED_KEYS,
        "format",
        "needs",
        "attempts",
        "cap",
        "forced",
        "http_shared",
        "at",
        "job",
        "via",
        "parent",
        "depth",
        "rerun",
        "path",
        "title",
        "error",
        "reason",
    )
)


def from_line(line: str) -> LedgerEntry:
    """Parse one JSONL ledger line into a validated :class:`LedgerEntry`.

    Args:
        line: One line of ``state/enrichment-ledger.jsonl``.

    Returns:
        The parsed entry.

    Raises:
        LedgerSchemaError: The line is not JSON (a torn line — the message
            names the sanctioned single-line deletion), not an object,
            carries unknown or pre-migration vocabulary (the message names
            the likely missing migration), or violates a schema invariant.
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as e:
        # A tear, not a schema fault: an interrupted append leaves a
        # half-written trailing line (relocatable mid-file by later
        # appends or a union merge), and no migration mends one — the
        # migration hint would send the owner to a repair that cannot
        # work. The advice is the passes row's, applied to this file.
        raise LedgerSchemaError(
            f"unparseable ledger line ({e}) — delete this torn line, and only this "
            f"line (a half-written line is not a record; every intact line is, and "
            f"no migration repairs a tear): {line!r}"
        ) from e
    if not isinstance(raw, dict):
        raise LedgerSchemaError(f"ledger line is not a JSON object: {line!r}")

    kind = raw.get("kind")
    if kind in RENAMED_KINDS:
        raise LedgerSchemaError(
            f"kind {kind!r} is pre-rename vocabulary (now {RENAMED_KINDS[kind]!r}); "
            f"{_MIGRATION_HINT}"
        )
    status = raw.get("status")
    if status in RETIRED_STATUSES:
        raise LedgerSchemaError(
            f"status {status!r} is retired vocabulary (now 'waiting' + needs: 'transcribe'); "
            f"{_MIGRATION_HINT}"
        )
    missing = [key for key in _REQUIRED_KEYS if key not in raw]
    if missing:
        raise LedgerSchemaError(
            f"ledger line missing required field(s) {missing}; {_MIGRATION_HINT}: {line!r}"
        )
    unknown = sorted(set(raw) - _ALL_KEYS)
    if unknown:
        raise LedgerSchemaError(
            f"ledger line has unknown field(s) {unknown}; either the running engine is "
            f"older than the line's writer (sync), or the line predates the current "
            f"schema and carries pre-migration extra fields — {_MIGRATION_HINT}: {line!r}"
        )

    try:
        return LedgerEntry(
            hash=_expect_str(raw, "hash"),
            url=_expect_str(raw, "url"),
            item=_expect_str(raw, "item"),
            kind=Kind(_expect_str(raw, "kind")),
            format=None if "format" not in raw else Format(_expect_str(raw, "format")),
            status=Status(_expect_str(raw, "status")),
            needs=None if "needs" not in raw else Need(_expect_str(raw, "needs")),
            attempts=None if "attempts" not in raw else _expect_int(raw, "attempts"),
            cap=None if "cap" not in raw else Cap(_expect_str(raw, "cap")),
            forced=_expect_bool(raw, "forced") if "forced" in raw else False,
            http_shared=_expect_bool(raw, "http_shared") if "http_shared" in raw else False,
            engine=_expect_str(raw, "engine"),
            date=datetime.date.fromisoformat(_expect_str(raw, "date")),
            at=None if "at" not in raw else _expect_datetime(raw, "at"),
            job=None if "job" not in raw else Job(_expect_str(raw, "job")),
            via=None if "via" not in raw else _expect_str(raw, "via"),
            parent=None if "parent" not in raw else _expect_str(raw, "parent"),
            depth=None if "depth" not in raw else _expect_int(raw, "depth"),
            rerun=_expect_bool(raw, "rerun") if "rerun" in raw else False,
            path=None if "path" not in raw else _expect_str(raw, "path"),
            title=None if "title" not in raw else _expect_str(raw, "title"),
            error=None if "error" not in raw else _expect_str(raw, "error"),
            reason=None if "reason" not in raw else _expect_str(raw, "reason"),
        )
    except LineageError as e:
        # No engine ever wrote this shape and no migration repairs it — the
        # rule itself is the whole story, and the hint would misattribute.
        raise LedgerSchemaError(f"nonconforming ledger line ({e})") from e
    except ValueError as e:
        raise LedgerSchemaError(f"nonconforming ledger line ({e}); {_MIGRATION_HINT}") from e


def _expect_str(raw: dict[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str):
        raise LedgerSchemaError(f"field {key!r} must be a string, got {type(value).__name__}")
    return value


def _expect_int(raw: dict[str, object], key: str) -> int:
    value = raw[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise LedgerSchemaError(f"field {key!r} must be an integer, got {type(value).__name__}")
    return value


def _expect_datetime(raw: dict[str, object], key: str) -> datetime.datetime:
    text = _expect_str(raw, key)
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError as e:
        raise LedgerSchemaError(f"field {key!r} must be an ISO 8601 timestamp, got {text!r}") from e


def _expect_bool(raw: dict[str, object], key: str) -> bool:
    value = raw[key]
    if not isinstance(value, bool):
        raise LedgerSchemaError(f"field {key!r} must be a boolean, got {type(value).__name__}")
    return value


def to_line(entry: LedgerEntry) -> str:
    """Serialize an entry to its JSONL line (no trailing newline).

    ``None`` fields are dropped; ``rerun``, ``forced`` and ``http_shared``
    appear only when true — the written line carries exactly the ledger
    schema, in schema order.
    """
    fields: tuple[tuple[str, str | int | bool | None], ...] = (
        ("hash", entry.hash),
        ("url", entry.url),
        ("item", entry.item),
        ("kind", entry.kind.value),
        ("format", entry.format.value if entry.format is not None else None),
        ("status", entry.status.value),
        ("needs", entry.needs.value if entry.needs is not None else None),
        ("attempts", entry.attempts),
        ("cap", entry.cap.value if entry.cap is not None else None),
        ("forced", entry.forced or None),
        ("http_shared", entry.http_shared or None),
        ("engine", entry.engine),
        ("date", entry.date.isoformat()),
        ("at", entry.at.isoformat() if entry.at is not None else None),
        ("job", entry.job.value if entry.job is not None else None),
        ("via", entry.via),
        ("parent", entry.parent),
        ("depth", entry.depth),
        ("rerun", entry.rerun or None),
        ("path", entry.path),
        ("title", entry.title),
        ("error", entry.error),
        ("reason", entry.reason),
    )
    record = {key: value for key, value in fields if value is not None}
    return json.dumps(record, ensure_ascii=False)


# An unstamped line predates every stamped one: every writer from the
# release that added `at` stamps it, so a line without one was written
# before that release shipped.
_UNSTAMPED = datetime.datetime.min.replace(tzinfo=datetime.UTC)

# How far ahead of the reader's clock a write timestamp may sit and still be
# read as one. `at` decides which line of a hash is live and it comes from
# another machine's wall clock: a machine whose clock has jumped stamps a
# line years ahead, that line wins its hash against every later real write,
# and `compact` then deletes the real ones — the unit stuck until the wall
# clock catches up, with no working repair verb. Past this much skew a value
# is not a write instant, so it is read as unstamped: it sorts oldest and
# loses to every real write, costing that one line its ordering and never
# the hash. Five minutes is wide enough for the ordinary spread between two
# machines whose lines merge; a clock stepping BACKWARDS inside the window
# still mis-orders two of its own writes, and that is accepted.
FUTURE_SKEW_ALLOWANCE = datetime.timedelta(minutes=5)


def _utc_now() -> datetime.datetime:
    """The reader's clock: the default for resolution, never for stamping."""
    return datetime.datetime.now(datetime.UTC)


def resolution_key(
    entry: LedgerEntry, position: int, *, now: datetime.datetime
) -> tuple[datetime.datetime, int]:
    """The ordering key that decides which line of a hash is the latest.

    Public so a migration reading the file tolerantly resolves hashes by the
    same rule ``load`` does, rather than reimplementing it and drifting.

    Args:
        entry: The parsed entry.
        position: Its position in the file (line number, or any monotonic
            counter over the lines read).
        now: The instant the ledger is being read at, UTC-aware. An ``at``
            further ahead than :data:`FUTURE_SKEW_ALLOWANCE` is read as
            unstamped — no clock is trusted past the present.

    Returns:
        A sort key; the greatest key for a hash names its live line.
    """
    at = entry.at
    if at is None or at > now + FUTURE_SKEW_ALLOWANCE:
        at = _UNSTAMPED
    return (at, position)


def load(path: Path, *, now: Callable[[], datetime.datetime] = _utc_now) -> dict[str, LedgerEntry]:
    """Load the ledger; the latest line per hash wins.

    Latest is by write timestamp, then by file position — see the module
    docstring for why position alone is not enough.

    Args:
        path: The ledger file; a missing file is an empty ledger.
        now: The reader's clock, defaulting to the wall clock — what a
            future-dated ``at`` is judged against (:func:`resolution_key`).

    Returns:
        Entries keyed by hash, in first-seen-hash order, each holding the
        latest line for that hash.

    Raises:
        LedgerSchemaError: A line does not conform — the message names the
            file, the line number, and the repair that fits the fault
            (the sanctioned torn-line deletion, or the likely missing
            migration), so every verb that dies on this load dies naming
            them.
    """
    entries: dict[str, LedgerEntry] = {}
    if not path.exists():
        return entries
    moment = now()
    winning: dict[str, tuple[datetime.datetime, int]] = {}
    # JSONL records are delimited by "\n" alone: str.splitlines() would also
    # split on unicode line separators (U+0085, U+2028, ...) INSIDE a JSON
    # string field and shear records apart (caught by the round-trip property).
    for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            entry = from_line(line)
        except LedgerSchemaError as e:
            raise LedgerSchemaError(f"{path}:{lineno}: {e}") from e
        key = resolution_key(entry, lineno, now=moment)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        # Reassigning an existing key keeps its insertion position, so a
        # loser landing after its winner does not reorder the ledger.
        entries[entry.hash] = entry
        winning[entry.hash] = key
    return entries


class Appender:
    """One open append handle over the ledger, for a run's many writes.

    ``append`` reopens the file per line — a cost the drain paid per unit.
    Holding the handle makes a line one write, and each line is flushed as
    it is written, which keeps open-per-line's durability contract exactly:
    after :meth:`append` returns the line is with the OS, visible to every
    reader (a fresh ``load`` in this process, or another process's) and
    safe against this process crashing. Neither shape fsyncs, so an OS
    crash or power loss can cost the tail either way — the read-back
    guards (mark's, the run report's) rely on the flush, never on close.

    **The per-line reopen was load-bearing, and the stat below replaces
    it — do not optimise it away.** ``compact``, exclude's purge, and the
    migrations rewrite the ledger atomically: a temp file, then one
    ``replace`` — a NEW inode at the same path. Open-per-line re-resolved
    the path every write, so appends after a rewrite landed in the new
    file; a held handle keeps the replaced inode, and without
    revalidation every later write of the run goes to an orphaned file no
    reader ever sees (discovered when this class first removed the
    reopen). So each append re-checks that the handle still names the
    file at the path — one ``fstat``/``stat`` pair, far cheaper than the
    open/write/close it replaced — and reopens on mismatch, or where the
    path is briefly gone mid-rewrite. The window that remains (a replace
    landing between the check and the write) is the same one the per-line
    reopen always had between its open and its write.

    The file opens lazily on the first append, in append mode, so every
    line lands at the file's current end even where another writer's line
    arrived in between — exactly as the per-line reopen behaved.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: TextIO | None = None

    def append(self, entry: LedgerEntry) -> None:
        """Append one entry as a full-record line, creating the file if needed.

        ``entry`` arrives already stamped: :func:`stamp` is the only place a
        written line's ``at`` is set, so the timestamp that orders a line is
        always the writing machine's own clock and never a value a caller
        chose.
        """
        handle = self._current_handle()
        handle.write(to_line(entry) + "\n")
        handle.flush()

    def _current_handle(self) -> TextIO:
        """The handle for the file the path names NOW, reopened if it moved."""
        if self._handle is not None:
            if self._still_current(self._handle):
                return self._handle
            self._handle.close()
            self._handle = None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8")
        return self._handle

    def _still_current(self, handle: TextIO) -> bool:
        """Whether the held handle is still the file at the path.

        False when an atomic rewrite replaced the inode, or the file is
        (however briefly) gone — either way the next line belongs to the
        file the path names, not to the one the handle kept.
        """
        try:
            current = self._path.stat()
        except FileNotFoundError:
            return False
        held = os.fstat(handle.fileno())
        return (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)

    def close(self) -> None:
        """Release the handle; a later append reopens."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def append(path: Path, entry: LedgerEntry) -> None:
    """Append one entry and release the handle — :class:`Appender`, one-shot.

    For the callers that write a line and are done (a purge's landing
    correction, a test's fixture line); a drain holds an :class:`Appender`
    instead of paying this reopen per unit.
    """
    appender = Appender(path)
    try:
        appender.append(entry)
    finally:
        appender.close()


def compact(path: Path, *, now: Callable[[], datetime.datetime] = _utc_now) -> int:
    """Rewrite the ledger keeping only the latest line per hash.

    Also settles git union merges — it keeps the line ``load`` resolves to,
    so a merge whose newer line landed first is settled in the newer line's
    favor rather than against it. Superseded lines — the audit trail — are
    dropped. Because it keeps exactly ``load``'s line, a future-dated ``at``
    cannot make it delete a real write: such a line is read as unstamped and
    loses, and it is the line that goes.

    Args:
        path: The ledger file; a missing file is a no-op.
        now: The reader's clock, passed through to :func:`load`.

    Returns:
        The number of superseded lines removed.
    """
    if not path.exists():
        return 0
    lines = [line for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
    entries = load(path, now=now)
    # Atomic: a crash mid-write must never lose the ledger.
    atomic.write_text(path, "".join(to_line(entry) + "\n" for entry in entries.values()))
    return len(lines) - len(entries)


def drop_items(
    path: Path,
    items: Collection[str],
    *,
    claimed: Collection[str],
    now: Callable[[], datetime.datetime] = _utc_now,
) -> tuple[int, int]:
    """Purge the work units ``items`` leave behind; return (lines removed, units kept).

    For purges only. ``bin/dex exclude`` deletes a corpus item and its
    enrichment permanently and on the record, so the item's ledger lines
    name work that seeding can never raise again — they would linger
    forever, and a ledger entry naming a nonexistent item is an anomaly,
    not a designed steady state. Git history keeps what this removes.

    **The unit is the hash, and a claimed hash is never purged.** A work
    unit is keyed by URL, not by item: where two corpus items list one URL
    they share one entry, and that entry names only one of them. Purging on
    the stored name alone deletes a history the other item still claims,
    and a hash two live items share is common in a real instance. So the
    hash is judged on the line ``load`` resolves to, and ``claimed`` — the
    work every surviving corpus item still lists — vetoes the removal. What
    goes is a hash whose live line names a purged item and which no live
    item claims; its superseded lines, the audit trail, go with it.

    Unparseable lines are kept, not purged: a line this layer cannot read
    is not provably one of these items', and a purge must never become
    incidental data loss. (:func:`load` is deliberately not used — one
    hand-tampered line would abort the whole purge, and the next command's
    ``load`` names that line loudly anyway.)

    Args:
        path: The ledger file; a missing file is a no-op.
        items: The purged corpus item ids.
        claimed: The work hashes live corpus items still list — computed
            after the purge deletes their files, so a purged item cannot
            claim its own work.
        now: The reader's clock — the hash is judged on the line ``load``
            resolves to, so it resolves against the same present.

    Returns:
        The number of lines removed, and the number of work units kept
        because a live corpus item still claims them.
    """
    if not items or not path.exists():
        return 0, 0
    text = path.read_text(encoding="utf-8")
    purged = [
        unit_hash
        for unit_hash, entry in _latest_readable(text, now=now()).items()
        if entry.item in items
    ]
    orphaned = {unit_hash for unit_hash in purged if unit_hash not in claimed}
    kept_units = len(purged) - len(orphaned)
    if not orphaned:
        return 0, kept_units
    kept: list[str] = []
    removed = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        if _names_hash(line, orphaned):
            removed += 1
            continue
        kept.append(line)
    # Atomic: a crash mid-write must never lose the ledger.
    atomic.write_text(path, "".join(line + "\n" for line in kept))
    return removed, kept_units


def _latest_readable(text: str, *, now: datetime.datetime) -> dict[str, LedgerEntry]:
    """The latest entry per hash over the lines that parse, as ``load`` resolves them.

    Not ``load`` itself: one hand-tampered line would abort the purge, and a
    line this code cannot read names no item it can judge.
    """
    latest: dict[str, LedgerEntry] = {}
    winning: dict[str, tuple[datetime.datetime, int]] = {}
    for lineno, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            entry = from_line(line)
        except LedgerSchemaError:
            continue
        key = resolution_key(entry, lineno, now=now)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        latest[entry.hash] = entry
        winning[entry.hash] = key
    return latest


def _names_hash(line: str, hashes: Collection[str]) -> bool:
    """True when this line belongs to one of ``hashes``.

    Read raw rather than through ``from_line``: the lines going include the
    hash's older audit lines, which need no validation to identify, and an
    unreadable line must survive (it names no hash this code trusts).
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return False
    return isinstance(raw, dict) and raw.get("hash") in hashes


def stamp(
    entry: LedgerEntry,
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> LedgerEntry:
    """Return ``entry`` re-stamped with the injected clocks and engine version.

    Every entry the pipeline writes goes through this — it is the one place
    ``date``, ``at`` and ``engine`` are set, and none comes from an ambient
    global. Whatever ``at`` the caller built the entry with is overwritten:
    the write timestamp is what orders this line against another machine's,
    so it is the writer's own clock or nothing.

    There is no carve-out, because nothing writes a line that records no
    work. ``date`` and ``engine`` answer WHEN, and by which engine, the
    work this line records was done, and both are load-bearing:
    :func:`dex_engine.pipeline.run.is_drainable` retries an ``error``
    entry once per newer engine, and ``date`` is the day the enrichment
    landed, which the digest-staleness backstop reads. A line written
    purely to correct an earlier one's attribution would have to carry
    both rather than claim them — so the drain asks the corpus who owns a
    unit at the moment it writes (``run._Drain.owner_of``) instead, and no
    persisted line is ever re-recorded to change what it says.

    Args:
        entry: The entry to stamp.
        today: Injected date clock — never ``datetime.date.today`` inline.
        now: Injected instant clock, UTC-aware — the write timestamp that
            orders this line against another machine's after a union merge.
        engine_version: The running engine's version string.

    Returns:
        A copy of the entry with ``date``, ``at`` and ``engine`` set.
    """
    return replace(entry, date=today(), at=now(), engine=engine_version)
