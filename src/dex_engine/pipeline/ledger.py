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

This layer never reads the ambient clock: today's date, the write instant,
and the engine version are injected so stamping and retry-on-new-engine are
testable.
"""

import datetime
import json
from collections.abc import Callable, Collection
from dataclasses import replace
from pathlib import Path

from dex_engine import atomic

from .types import Format, Kind, LedgerEntry, Need, Status

__all__ = [
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

    Raised at the serialization boundary — with the likely missing migration
    named — instead of surfacing as a ``KeyError`` three stages later.
    """


# Pre-rename vocabulary, translated by migration 1. Recognized here only
# to name that migration in the error; never silently accepted (no aliases).
_RENAMED_KINDS = {"tweet": "x", "blog": "web"}
_RETIRED_STATUSES = {"nocaptions", "toolong"}

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
        "capped",
        "at",
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
        LedgerSchemaError: The line is not JSON, not an object, carries
            unknown or pre-migration vocabulary, or violates a schema invariant.
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as e:
        raise LedgerSchemaError(f"unparseable ledger line ({e}): {line!r}") from e
    if not isinstance(raw, dict):
        raise LedgerSchemaError(f"ledger line is not a JSON object: {line!r}")

    kind = raw.get("kind")
    if kind in _RENAMED_KINDS:
        raise LedgerSchemaError(
            f"kind {kind!r} is pre-rename vocabulary (now {_RENAMED_KINDS[kind]!r}); "
            f"{_MIGRATION_HINT}"
        )
    status = raw.get("status")
    if status in _RETIRED_STATUSES:
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
            capped=_expect_bool(raw, "capped") if "capped" in raw else False,
            engine=_expect_str(raw, "engine"),
            date=datetime.date.fromisoformat(_expect_str(raw, "date")),
            at=None if "at" not in raw else _expect_datetime(raw, "at"),
            via=None if "via" not in raw else _expect_str(raw, "via"),
            parent=None if "parent" not in raw else _expect_str(raw, "parent"),
            depth=None if "depth" not in raw else _expect_int(raw, "depth"),
            rerun=_expect_bool(raw, "rerun") if "rerun" in raw else False,
            path=None if "path" not in raw else _expect_str(raw, "path"),
            title=None if "title" not in raw else _expect_str(raw, "title"),
            error=None if "error" not in raw else _expect_str(raw, "error"),
            reason=None if "reason" not in raw else _expect_str(raw, "reason"),
        )
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

    ``None`` fields are dropped; ``rerun`` and ``capped`` appear only when
    true — the written line carries exactly the ledger schema, in schema
    order.
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
        ("capped", entry.capped or None),
        ("engine", entry.engine),
        ("date", entry.date.isoformat()),
        ("at", entry.at.isoformat() if entry.at is not None else None),
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


def resolution_key(entry: LedgerEntry, position: int) -> tuple[datetime.datetime, int]:
    """The ordering key that decides which line of a hash is the latest.

    Public so a migration reading the file tolerantly resolves hashes by the
    same rule ``load`` does, rather than reimplementing it and drifting.

    Args:
        entry: The parsed entry.
        position: Its position in the file (line number, or any monotonic
            counter over the lines read).

    Returns:
        A sort key; the greatest key for a hash names its live line.
    """
    return (entry.at or _UNSTAMPED, position)


def load(path: Path) -> dict[str, LedgerEntry]:
    """Load the ledger; the latest line per hash wins.

    Latest is by write timestamp, then by file position — see the module
    docstring for why position alone is not enough.

    Args:
        path: The ledger file; a missing file is an empty ledger.

    Returns:
        Entries keyed by hash, in first-seen-hash order, each holding the
        latest line for that hash.

    Raises:
        LedgerSchemaError: A line does not conform — the message names the
            file, line number, and likely missing migration.
    """
    entries: dict[str, LedgerEntry] = {}
    if not path.exists():
        return entries
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
        key = resolution_key(entry, lineno)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        # Reassigning an existing key keeps its insertion position, so a
        # loser landing after its winner does not reorder the ledger.
        entries[entry.hash] = entry
        winning[entry.hash] = key
    return entries


def append(path: Path, entry: LedgerEntry) -> None:
    """Append one entry as a full-record line, creating the file if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(to_line(entry) + "\n")


def compact(path: Path) -> int:
    """Rewrite the ledger keeping only the latest line per hash.

    Also settles git union merges — it keeps the line ``load`` resolves to,
    so a merge whose newer line landed first is settled in the newer line's
    favor rather than against it. Superseded lines — the audit trail — are
    dropped.

    Args:
        path: The ledger file; a missing file is a no-op.

    Returns:
        The number of superseded lines removed.
    """
    if not path.exists():
        return 0
    lines = [line for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
    entries = load(path)
    # Atomic: a crash mid-write must never lose the ledger.
    atomic.write_text(path, "".join(to_line(entry) + "\n" for entry in entries.values()))
    return len(lines) - len(entries)


def drop_items(path: Path, items: Collection[str]) -> int:
    """Remove every line naming one of ``items``; return how many went.

    For purges only. ``bin/dex exclude`` deletes a corpus item and its
    enrichment permanently and on the record, so the item's ledger lines
    name work that seeding can never raise again — they would linger
    forever, and a ledger entry naming a nonexistent item is an anomaly,
    not a designed steady state. Git history keeps what this removes.

    Unparseable lines are kept, not purged: a line this layer cannot read
    is not provably one of these items', and a purge must never become
    incidental data loss. (:func:`from_line` is deliberately not used — one
    hand-tampered line would abort the whole purge, and the next command's
    ``load`` names that line loudly anyway.)

    Args:
        path: The ledger file; a missing file is a no-op.
        items: The purged corpus item ids.

    Returns:
        The number of lines removed.
    """
    if not items or not path.exists():
        return 0
    kept: list[str] = []
    removed = 0
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if isinstance(raw, dict) and raw.get("item") in items:
            removed += 1
            continue
        kept.append(line)
    if removed:
        # Atomic: a crash mid-write must never lose the ledger.
        atomic.write_text(path, "".join(line + "\n" for line in kept))
    return removed


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
    global.

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
