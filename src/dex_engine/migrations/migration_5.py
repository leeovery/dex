"""Migration 5 — drop a unit's superseded outputs left beside its landing.

The superseded-output drop matched candidate names from the closed
``<kind>-<hash6>.md`` set, so a unit healed by hand under any other name
kept its old file when a later landing — a migration-seeded rerun, a
redetection — wrote the engine-named file beside it. The item's derived
``enrichment:`` listing names every markdown in the directory, so the one
unit listed twice: two views of one unit, the pre-repair view served to
the digest forever, and no ledger row owning the old file.

The rule the fixed engine now applies on every landing is applied here
once, retroactively: for every live ``done`` page unit whose stored
``path`` is on disk, every OTHER markdown in the same directory whose
frontmatter records the unit's URL is that unit's own earlier output —
the URL is the unit's identity (two entries under one URL are one hash) —
and leaves. A candidate that cannot be read is left alone and named for
the session: an unreadable file is not proof of anything, and deleting on
a guess is how enrichment gets lost.

``job`` units are never anchors: a media download or asset write owns no
markdown, and its parent's file speaks for the unit. Entries whose stored
``path`` is missing anchor nothing — with no kept file there is no
"superseded", and which of two orphaned views is current is session
judgment, not mechanics.

Idempotent: the first pass leaves one file per unit, so the second finds
no sibling recording a kept unit's URL.
"""

import datetime
from collections.abc import Callable
from pathlib import Path

from dex_engine.pipeline.enrichment import read_enrichment
from dex_engine.pipeline.ledger import LedgerSchemaError, from_line, resolution_key
from dex_engine.pipeline.types import LedgerEntry, MigrationReport, Skipped, Status

__all__ = ["SupersededOutputSweep", "build"]

NUMBER = 5
INTENT = (
    "drop superseded enrichment files the closed-name drop missed: a hand-named heal "
    "kept its old file when a rerun landed the engine-named one beside it, and the "
    "item listed one unit twice"
)


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — the shared build signature; nothing here is dated
    now: Callable[[], datetime.datetime],
    engine_version: str,  # noqa: ARG001 — nothing here is version-stamped
) -> "SupersededOutputSweep":
    """Build migration 5; ``now`` orders ledger lines, nothing else is stamped."""
    return SupersededOutputSweep(now=now)


class SupersededOutputSweep:
    """Migration 5: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def __init__(self, *, now: Callable[[], datetime.datetime]) -> None:
        """``now`` breaks ties when the ledger resolves each hash's live line."""
        self._now = now

    def apply(self, root: Path) -> MigrationReport:
        """Drop every done unit's URL-proven siblings, once.

        Args:
            root: The instance root.

        Returns:
            The report: one action per dropped file naming what replaced
            it, and a skip for every candidate that could not be read.
        """
        actions: list[str] = []
        skipped: list[Skipped] = []
        ledger_path = root / "state" / "enrichment-ledger.jsonl"
        if not ledger_path.exists():
            return MigrationReport()
        for entry in _latest_entries(ledger_path, skipped, now=self._now()):
            if entry.job is not None or entry.status is not Status.DONE or entry.path is None:
                continue
            kept = root / entry.path
            if not kept.is_file():
                continue
            for stale in sorted(kept.parent.glob("*.md")):
                if stale.name == kept.name:
                    continue
                try:
                    fields, _body = read_enrichment(stale)
                except (OSError, UnicodeDecodeError):
                    skipped.append(
                        Skipped(
                            what=str(stale.relative_to(root)),
                            why="could not be read, so whether it is a superseded view "
                            f"of {entry.url} is unknown — inspect and delete by hand if it is",
                        )
                    )
                    continue
                if fields.get("url") == entry.url:
                    stale.unlink()
                    actions.append(
                        f"dropped {stale.relative_to(root)}: a superseded view of "
                        f"{entry.url}, replaced by {entry.path}"
                    )
        return MigrationReport(actions=actions, skipped=skipped)


def _latest_entries(
    path: Path, skipped: list[Skipped], *, now: datetime.datetime
) -> list[LedgerEntry]:
    """Every hash's live line, read tolerantly.

    Read line by line rather than through ``ledger.load``: one line this
    migration has no business touching must not stop it repairing the
    ones it does.
    """
    best: dict[str, tuple[tuple[datetime.datetime, int], LedgerEntry]] = {}
    for position, line in enumerate(path.read_text(encoding="utf-8").split("\n")):
        if not line.strip():
            continue
        try:
            entry = from_line(line)
        except (LedgerSchemaError, ValueError):
            skipped.append(
                Skipped(
                    what=f"ledger line {position + 1}",
                    why="does not parse — left untouched; the superseded-output sweep skipped it",
                )
            )
            continue
        key = resolution_key(entry, position, now=now)
        held = best.get(entry.hash)
        if held is None or key > held[0]:
            best[entry.hash] = (key, entry)
    return [entry for _, entry in best.values()]
