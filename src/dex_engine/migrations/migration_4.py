"""Migration 4 — sweep ledger entries whose corpus item no longer exists.

``bin/dex exclude`` purges an item permanently and on the record: the corpus
file, ``enrichment/<id>/``, and — since the release this migration ships in —
the item's ledger entries. Purges made before that last part left their
entries behind, and so did items removed by hand. This is the one-time
historical cleanup, run automatically so no session has to notice.

Dropping them is safe for the same reason migration 1 drops an unattributable
line: the corpus is the source of truth and the ledger is derived work state.
Seeding builds work units from corpus items' URLs, so an entry whose item is
gone names work nothing will ever raise again — it cannot be reached, cannot
be drained, and cannot be healed (``enrich mark`` heals a unit that belongs to
an item). And the pre-sweep ledger is committed in git history, so nothing is
destroyed.

**The item is the test, never the output file.** An entry is swept only when
``corpus/<id[:4]>/<id>.md`` is absent. An entry whose ``path`` is missing from
disk is a different finding with a different repair — the item still exists,
the work still belongs to it, and the fix is to re-fetch, not to forget.

The unit swept is the hash, decided on its live line: where a hash's latest
entry names a missing item, every line of that hash goes, audit trail
included. A hash whose latest line names a live item is kept whole even if an
older line named a purged one — that is a re-parenting's history, and it
retires at ``compact`` like any superseded line.

Idempotent: a second apply finds every remaining entry's item on disk.
"""

import datetime
import json
from collections.abc import Callable
from pathlib import Path

from dex_engine import atomic
from dex_engine.pipeline.ledger import LedgerSchemaError, from_line, resolution_key
from dex_engine.pipeline.types import LedgerEntry, MigrationReport, Skipped

__all__ = ["GhostItemSweep", "build"]

NUMBER = 4
INTENT = (
    "sweep ledger entries whose corpus item no longer exists: work state for content "
    "purged by `dex exclude` (before it swept the ledger itself) or removed by hand — "
    "seeding can never raise it again, and git history holds it"
)

# How many swept entries the report names individually before it summarizes
# the rest — the same shape migration 1 uses for dropped lines.
_SWEEP_REPORT_CAP = 5


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — the shared build signature; this writes no dated state
    now: Callable[[], datetime.datetime],  # noqa: ARG001 — and appends no ledger line
    engine_version: str,  # noqa: ARG001 — nothing here is version-stamped
) -> "GhostItemSweep":
    """Build migration 4 (the shared build signature; this migration only removes)."""
    return GhostItemSweep()


class GhostItemSweep:
    """Migration 4: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def apply(self, root: Path) -> MigrationReport:
        """Remove every ledger line whose hash resolves to a missing item.

        Args:
            root: The instance root.

        Returns:
            The report: the swept count, a capped named list, and a skip for
            any line too malformed to judge (kept, never swept).
        """
        actions: list[str] = []
        skipped: list[Skipped] = []
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport(actions=actions, skipped=skipped, anomalies=[])
        original = path.read_text(encoding="utf-8")
        live = _live_entries(original, skipped)
        exclusions = _exclusions(root)
        doomed = {
            unit_hash: _why_swept(entry, exclusions=exclusions)
            for unit_hash, entry in live.items()
            if not (root / "corpus" / entry.item[:4] / f"{entry.item}.md").exists()
        }
        if not doomed:
            return MigrationReport(actions=actions, skipped=skipped, anomalies=[])
        kept = [line for line in original.split("\n") if line.strip() and not _swept(line, doomed)]
        # Atomic: a crash mid-write must never lose the ledger.
        atomic.write_text(path, "".join(line + "\n" for line in kept))
        _report(doomed, live, actions)
        return MigrationReport(actions=actions, skipped=skipped, anomalies=[])


def _live_entries(text: str, skipped: list[Skipped]) -> dict[str, LedgerEntry]:
    """The latest entry per hash, resolved as ``ledger.load`` resolves it.

    Not ``ledger.load`` itself: one hand-tampered line would abort the sweep,
    and a migration must handle everything readable. An unreadable line is
    skipped-with-why and kept verbatim — this code cannot tell whose item it
    names, and a sweep must never remove what it cannot read.
    """
    live: dict[str, LedgerEntry] = {}
    winning: dict[str, tuple[datetime.datetime, int]] = {}
    for lineno, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            entry = from_line(line)
        except LedgerSchemaError as e:
            skipped.append(
                Skipped(
                    what=f"ledger line {lineno}",
                    why=f"unreadable under the current schema ({e}) — kept as it is and "
                    "not considered for the sweep; repair it per migration 1's report",
                )
            )
            continue
        key = resolution_key(entry, lineno)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        live[entry.hash] = entry
        winning[entry.hash] = key
    return live


def _swept(line: str, doomed: dict[str, str]) -> bool:
    """True when this line belongs to a doomed hash.

    Read raw rather than through ``from_line``: the lines being removed
    include a hash's older audit lines, which need no validation to identify,
    and an unreadable line must survive (it names no hash this code trusts).
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return False
    return isinstance(raw, dict) and raw.get("hash") in doomed


def _why_swept(entry: LedgerEntry, *, exclusions: dict[str, str]) -> str:
    item_path = f"corpus/{entry.item[:4]}/{entry.item}.md"
    if entry.item in exclusions:
        reason = exclusions[entry.item] or "no reason recorded"
        return (
            f"{item_path} is gone and the item is excluded on the record "
            f"(state/exclusions.tsv: {reason})"
        )
    return (
        f"{item_path} does not exist and no state/exclusions.tsv record names the item — "
        "it was removed by hand or a purge was interrupted"
    )


def _exclusions(root: Path) -> dict[str, str]:
    """``state/exclusions.tsv`` as item id -> stated reason.

    Read here rather than shared with migration 2: a migration's behavior is
    frozen at its release, and a shared reader would let a later change to one
    migration silently alter another's meaning. Missing file, blank lines and
    reason-less rows are all tolerated — the corpus file's absence is what
    decides; the reason only makes the report readable.
    """
    path = root / "state" / "exclusions.tsv"
    if not path.exists():
        return {}
    reasons: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        item, _, reason = line.partition("\t")
        reasons[item.strip()] = reason.strip()
    return reasons


def _report(doomed: dict[str, str], live: dict[str, LedgerEntry], actions: list[str]) -> None:
    """State what went and why — a silent sweep is indistinguishable from loss."""
    on_record = sum(1 for why in doomed.values() if "excluded on the record" in why)
    actions.append(
        f"ledger: {len(doomed)} entr{'y' if len(doomed) == 1 else 'ies'} swept — their "
        f"corpus item no longer exists ({on_record} excluded on the record, "
        f"{len(doomed) - on_record} removed by hand); seeding can never raise this work "
        "again, and git history holds the entries"
    )
    for unit_hash, why in list(doomed.items())[:_SWEEP_REPORT_CAP]:
        actions.append(f"ledger swept {unit_hash} {live[unit_hash].url}: {why}")
    if len(doomed) > _SWEEP_REPORT_CAP:
        actions.append(
            f"ledger: … and {len(doomed) - _SWEEP_REPORT_CAP} further swept entr"
            f"{'y' if len(doomed) - _SWEEP_REPORT_CAP == 1 else 'ies'}, same treatment — "
            "read them in git history"
        )
