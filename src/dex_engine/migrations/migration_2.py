"""Migration 2 — rerun seed (§12): requeue two known-deficient cohorts.

Two fixes shipped with the rewrite that make old stored enrichments
deficient:

- **web**: the old extractor stripped hyperlinks; the rewrite keeps them
  (links are harvest input, §10);
- **x**: thread walk-up did not exist; a stored single post may have been a
  thread, and the rerun walks up and re-harvests.

Membership is total by construction: this migration runs directly after
migration 1, and the old engine had *neither* fix — so **every pre-rewrite
``done`` web/x entry qualifies**. Pre-rewrite is precise, not fuzzy:
migration 1 stamps un-versioned old lines ``engine: 0.0.1``, the only
version the pre-rewrite engine ever shipped as; entries written by the
rewritten engine always carry a newer version. Only ``done`` entries are
seeded — old ``error`` entries already retry under the new-engine rule, and
``manual`` entries stay parked for judgment (§12).

The seed is queue seeding with provenance, one of the two permitted acts:
the existing URL-keyed work unit re-enters as ``{status: queued, rerun:
true, via: migration-2}`` — a real URL through the front door; the pipeline
does the actual work with current code. Idempotent: seeds append to the
ledger, last-per-hash wins (§4), so a hash whose latest line is already the
seed is no longer ``done`` and is never seeded twice.
"""

import datetime
from collections.abc import Callable
from pathlib import Path

from dex_engine.migrations import MigrationError
from dex_engine.pipeline.ledger import LedgerSchemaError, append, from_line
from dex_engine.pipeline.types import (
    Kind,
    LedgerEntry,
    MigrationReport,
    Skipped,
    Status,
    parse_version,
)

__all__ = ["RerunSeed", "build"]

NUMBER = 2
INTENT = (
    "rerun seed: requeue every pre-rewrite done web/x entry as {queued, rerun, via: "
    "migration-2} — the old engine (0.0.1) had neither the link-keeping fix (web) nor "
    "thread walk-up (x), so all its stored web/x enrichments qualify"
)

_PRE_REWRITE = (0, 0, 1)
_SEEDED_KINDS = frozenset({Kind.WEB, Kind.X})


def build(
    *,
    today: Callable[[], datetime.date],
    engine_version: str,
) -> "RerunSeed":
    """Build migration 2 with the injected clock and running engine version.

    Raises:
        MigrationError: The running engine identifies as 0.0.1 — the
            pre-rewrite marker. Seeds stamped with it would satisfy this
            migration's own membership test and corrupt it: a re-run would
            reseed the seeds. The rewrite ships as >= 0.1.0 by construction
            (pyproject); hitting this guard means a mis-built engine.
    """
    if parse_version(engine_version) == _PRE_REWRITE:
        raise MigrationError(
            f"migration 2 refuses to run as engine {engine_version!r}: that version is "
            "the pre-rewrite marker (migration 1's stamp, §12) — seeds stamped with it "
            "would corrupt their own membership test; a rewrite engine is >= 0.1.0"
        )
    return RerunSeed(today=today, engine_version=engine_version)


class RerunSeed:
    """Migration 2 (§12): see the module docstring."""

    number = NUMBER
    intent = INTENT

    def __init__(self, *, today: Callable[[], datetime.date], engine_version: str) -> None:
        """Seeds are stamped with the injected clock and running engine (§14)."""
        self._today = today
        self._engine_version = engine_version

    def apply(self, root: Path) -> MigrationReport:
        """Seed the reruns into the ledger at ``root``.

        Tolerates un-pulled repos (no ledger → nothing to seed) and lines
        migration 1 could not translate (skipped here too, with the same
        repair pointer — they cannot poison the seeding of readable lines).
        """
        actions: list[str] = []
        skipped: list[Skipped] = []
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport(actions=actions, skipped=skipped, anomalies=[])
        latest = _latest_per_hash(path, skipped)
        counts = {Kind.X: 0, Kind.WEB: 0}
        for entry in latest.values():
            if not _qualifies(entry, skipped):
                continue
            append(
                path,
                LedgerEntry(
                    hash=entry.hash,
                    url=entry.url,
                    item=entry.item,
                    kind=entry.kind,
                    status=Status.QUEUED,
                    engine=self._engine_version,
                    date=self._today(),
                    via="migration-2",
                    rerun=True,
                ),
            )
            counts[entry.kind] += 1
        seeded = counts[Kind.X] + counts[Kind.WEB]
        if seeded:
            actions.append(
                f"seeded {seeded} rerun(s): {counts[Kind.X]} x (thread walk-up), "
                f"{counts[Kind.WEB]} web (link keeping)"
            )
        return MigrationReport(actions=actions, skipped=skipped, anomalies=[])


def _latest_per_hash(path: Path, skipped: list[Skipped]) -> dict[str, LedgerEntry]:
    """Last-per-hash over readable lines; unreadable lines are skipped-with-why.

    Not ``ledger.load``: a hand-tampered line would abort the whole load,
    and this migration must still seed everything readable. Known wrinkle:
    when a hash's *latest* line is the unreadable one, an earlier superseded
    line gets promoted to "latest" here and may seed. Harmless — the seed is
    a real URL through the front door, and the pipeline re-classifies it
    with current code; worst case is one redundant fetch.
    """
    latest: dict[str, LedgerEntry] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            entry = from_line(line)
        except LedgerSchemaError as e:
            skipped.append(
                Skipped(
                    what=f"ledger line {lineno}",
                    why=f"unreadable under the current schema ({e}) — repair it per "
                    "migration 1's report; not considered for seeding",
                )
            )
            continue
        latest[entry.hash] = entry
    return latest


def _qualifies(entry: LedgerEntry, skipped: list[Skipped]) -> bool:
    if entry.status is not Status.DONE or entry.kind not in _SEEDED_KINDS:
        return False
    try:
        return parse_version(entry.engine) == _PRE_REWRITE
    except ValueError:
        skipped.append(
            Skipped(
                what=f"ledger entry {entry.hash}",
                why=f"done {entry.kind.value} entry with unparseable engine "
                f"{entry.engine!r} — cannot judge pre-rewrite membership; requeue by "
                "hand if its enrichment predates the rewrite",
            )
        )
        return False
