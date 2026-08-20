"""Ledger persistence: the single serialization boundary for LedgerEntry (§5).

``state/enrichment-ledger.jsonl`` is append-only, full-record lines,
last-per-hash wins (§4). Superseded lines are the audit trail until
``compact`` rewrites the file keeping only the latest line per hash (which
also settles git union merges).

This layer never reads the ambient clock: today's date and the engine
version are injected (§14) so date-stamping and retry-on-new-engine are
testable.
"""

import datetime
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .types import Format, Kind, LedgerEntry, Need, Status

__all__ = [
    "LedgerSchemaError",
    "append",
    "compact",
    "from_line",
    "load",
    "stamp",
    "to_line",
]


class LedgerSchemaError(ValueError):
    """A ledger line does not conform to the current schema (§5).

    Raised at the serialization boundary — with the likely missing migration
    named — instead of surfacing as a ``KeyError`` three stages later.
    """


# Pre-rename vocabulary, translated by migration 1 (§12). Recognized here only
# to name that migration in the error; never silently accepted (no aliases, §3).
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
        "via",
        "parent",
        "depth",
        "rerun",
        "path",
        "title",
        "error",
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
            unknown or pre-migration vocabulary, or violates a §5 invariant.
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
            f"ledger line has unknown field(s) {unknown}; "
            f"the running engine may be older than the line's writer: {line!r}"
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
            engine=_expect_str(raw, "engine"),
            date=datetime.date.fromisoformat(_expect_str(raw, "date")),
            via=None if "via" not in raw else _expect_str(raw, "via"),
            parent=None if "parent" not in raw else _expect_str(raw, "parent"),
            depth=None if "depth" not in raw else _expect_int(raw, "depth"),
            rerun=_expect_bool(raw, "rerun") if "rerun" in raw else False,
            path=None if "path" not in raw else _expect_str(raw, "path"),
            title=None if "title" not in raw else _expect_str(raw, "title"),
            error=None if "error" not in raw else _expect_str(raw, "error"),
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


def _expect_bool(raw: dict[str, object], key: str) -> bool:
    value = raw[key]
    if not isinstance(value, bool):
        raise LedgerSchemaError(f"field {key!r} must be a boolean, got {type(value).__name__}")
    return value


def to_line(entry: LedgerEntry) -> str:
    """Serialize an entry to its JSONL line (no trailing newline).

    ``None`` fields are dropped; ``rerun`` appears only when true — the
    written line carries exactly the §5 schema, in schema order.
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
        ("engine", entry.engine),
        ("date", entry.date.isoformat()),
        ("via", entry.via),
        ("parent", entry.parent),
        ("depth", entry.depth),
        ("rerun", entry.rerun or None),
        ("path", entry.path),
        ("title", entry.title),
        ("error", entry.error),
    )
    record = {key: value for key, value in fields if value is not None}
    return json.dumps(record, ensure_ascii=False)


def load(path: Path) -> dict[str, LedgerEntry]:
    """Load the ledger; the last line per hash wins (§4).

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
        entries[entry.hash] = entry
    return entries


def append(path: Path, entry: LedgerEntry) -> None:
    """Append one entry as a full-record line, creating the file if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(to_line(entry) + "\n")


def compact(path: Path) -> int:
    """Rewrite the ledger keeping only the latest line per hash.

    Also settles git union merges (§4). Superseded lines — the audit trail —
    are dropped.

    Args:
        path: The ledger file; a missing file is a no-op.

    Returns:
        The number of superseded lines removed.
    """
    if not path.exists():
        return 0
    lines = [line for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
    entries = load(path)
    path.write_text("".join(to_line(entry) + "\n" for entry in entries.values()), encoding="utf-8")
    return len(lines) - len(entries)


def stamp(
    entry: LedgerEntry,
    *,
    today: Callable[[], datetime.date],
    engine_version: str,
) -> LedgerEntry:
    """Return ``entry`` re-stamped with the injected clock and engine version.

    Every entry the pipeline writes goes through this — it is the one place
    ``date`` and ``engine`` are set, and neither comes from an ambient global.

    Args:
        entry: The entry to stamp.
        today: Injected clock (§14) — never ``datetime.date.today`` inline.
        engine_version: The running engine's version string.

    Returns:
        A copy of the entry with ``date`` and ``engine`` set.
    """
    return replace(entry, date=today(), engine=engine_version)
