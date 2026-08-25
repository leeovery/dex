"""Migrations: mechanical, near-instant state rewrites, run by sync first.

One module per migration — ``migration_<n>.py``, plain integers, discovered
and run in numeric order (padding buys nothing; single author +
release-serialized numbering means no collisions). Each module exposes
``build(*, today, now, engine_version) -> Migration``; the two clocks (the
date and the UTC write instant a seeded ledger line carries) and the
running engine version are injected, never read ambiently.

The applied log is ``state/migrations.jsonl`` — one ``{number, engine,
date}`` record per applied migration, append-only, ``merge=union`` like all
state JSONL. The log is the fast path; **idempotency is the backstop**:
when racing machines union-merge their logs, or a machine runs against an
un-pulled repo, a migration may be applied again — every migration's
``apply`` is therefore a no-op on already-migrated state by construction.

Authoring rules: mechanical and near-instant only. Exactly two
permitted acts — provably-safe state rewrites, and queue seeding with
provenance. Migrations never fetch, never transform content, never
re-implement pipeline stages; whatever they cannot do safely lands in the
report's ``skipped``/``anomalies`` for the session to repair with judgment.
"""

import datetime
import json
import pkgutil
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Protocol

from dex_engine.pipeline.types import MigrationReport

__all__ = [
    "AppliedMigration",
    "Migration",
    "MigrationError",
    "append_applied",
    "discover",
    "log_path",
    "read_applied",
    "run_pending",
]


class MigrationError(RuntimeError):
    """A migration, or the applied-migrations log, is in a state code cannot fix."""


class Migration(Protocol):
    """One migration: a number, a stated intent, and a mechanical apply."""

    number: int
    intent: str

    def apply(self, root: Path) -> MigrationReport:
        """Apply against the instance at ``root``; idempotent by construction.

        Must tolerate un-pulled repos (missing state files, missing corpus)
        and be safe to re-run after an interruption at any point.
        """
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class AppliedMigration:
    """One migration applied this run, with its report — for the sync report."""

    number: int
    intent: str
    report: MigrationReport


_MODULE_RE = re.compile(r"^migration_([1-9][0-9]*)$")


def log_path(root: Path) -> Path:
    """``state/migrations.jsonl`` under the instance at ``root``."""
    return root / "state" / "migrations.jsonl"


def discover(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> list[Migration]:
    """Discover shipped migrations, built with the injected clocks and version.

    Modules named ``migration_<n>.py`` in this package are the registry;
    the runner sorts numerically.

    Args:
        today: Injected date clock.
        now: Injected UTC write-instant clock.
        engine_version: The running engine's version string.

    Returns:
        Migrations in ascending numeric order.

    Raises:
        MigrationError: A migration module lacks ``build`` or its number
            disagrees with its filename — a packaging bug, named loudly.
    """
    found: list[tuple[int, Migration]] = []
    for module_info in pkgutil.iter_modules(__path__):
        match = _MODULE_RE.match(module_info.name)
        if not match:
            if module_info.name.startswith("migration"):
                # A near-miss name (migration_01, migration_x) would silently
                # never run — a packaging bug that must be loud, not latent.
                raise MigrationError(
                    f"module {module_info.name!r} looks like a migration but does not "
                    "match migration_<n> with a plain integer — rename it or it "
                    "will never be discovered"
                )
            continue
        number = int(match.group(1))
        module = import_module(f"{__name__}.{module_info.name}")
        build = getattr(module, "build", None)
        if build is None:
            raise MigrationError(
                f"{module.__name__} does not expose build(*, today, now, engine_version)"
            )
        migration: Migration = build(today=today, now=now, engine_version=engine_version)
        if migration.number != number:
            raise MigrationError(
                f"{module.__name__} builds migration number {migration.number}, "
                f"but its filename says {number} — the filename is the registry"
            )
        found.append((number, migration))
    found.sort(key=lambda pair: pair[0])
    return [migration for _, migration in found]


def read_applied(path: Path) -> set[int]:
    """Read the applied-migrations log into the set of applied numbers.

    The file merges as a union between machines, so duplicate records
    are expected and collapse here; a missing file is an empty log. Records
    are delimited by newlines alone — ``str.splitlines()`` would also split
    on unicode line separators inside a JSON string.

    Args:
        path: The ``state/migrations.jsonl`` file.

    Returns:
        The applied migration numbers.

    Raises:
        MigrationError: A line is not a ``{number, engine, date}`` record —
            the log is corrupt, and guessing which migrations ran is exactly
            how state gets destroyed.
    """
    applied: set[int] = set()
    if not path.exists():
        return applied
    for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise MigrationError(
                f"{path}:{lineno}: unparseable applied-migration record ({e}) — a "
                "half-written line is not a record: delete the torn line and re-run "
                "sync (idempotent migrations make a lost record harmless)"
            ) from e
        if not isinstance(raw, dict):
            raise MigrationError(f"{path}:{lineno}: record must be a JSON object: {line!r}")
        number = raw.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise MigrationError(
                f"{path}:{lineno}: record must carry an integer 'number' "
                f"({{number, engine, date}}): {line!r}"
            )
        applied.add(number)
    return applied


def append_applied(
    path: Path,
    *,
    number: int,
    engine: str,
    date: datetime.date,
) -> None:
    """Append one ``{number, engine, date}`` record, creating the file if needed.

    The append is not atomic: a crash mid-write can tear the record. That
    window is accepted — a torn line fails :func:`read_applied` loudly with
    the file and line named, and the repair is one line of session judgment:
    fix or delete the torn line, re-run sync; idempotent migrations make a
    lost record harmless.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"number": number, "engine": engine, "date": date.isoformat()}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def run_pending(
    root: Path,
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
    migrations: Sequence[Migration] | None = None,
) -> list[AppliedMigration]:
    """Run every pending migration in numeric order; log each as it lands.

    Called by sync before anything else touches state. The log record
    is appended only after a migration's ``apply`` returns — an interrupted
    migration is re-run next time, which its idempotency makes safe.

    Args:
        root: The instance root.
        today: Injected date clock.
        now: Injected UTC write-instant clock.
        engine_version: The running engine's version — stamped into each
            log record.
        migrations: Override for tests; ``None`` discovers the shipped set.

    Returns:
        The migrations applied this run, with their reports, in order.
    """
    if migrations is None:
        migrations = discover(today=today, now=now, engine_version=engine_version)
    path = log_path(root)
    applied_numbers = read_applied(path)
    applied: list[AppliedMigration] = []
    for migration in migrations:
        if migration.number in applied_numbers:
            continue
        report = migration.apply(root)
        append_applied(path, number=migration.number, engine=engine_version, date=today())
        applied.append(
            AppliedMigration(number=migration.number, intent=migration.intent, report=report)
        )
    return applied
