"""Migration 9 — reseed pooled-kind media the 4-file cap refused.

The 4-file media cap treated an Instagram carousel's slides and an X
thread's photos — one post split across files — as embedded extras, and
skipped every download past the fourth with a terminal ``media cap (4
files) reached``. The pooled cap (100, a safety backstop) now takes those
kinds, but a terminal skip is owed nothing by any drain, so the refused
slides stay refused on every instance that shared a big carousel unless
they are requeued here.

Membership is exact, not vintage-guessed: a live ``job: media`` line,
``skipped``, whose reason is the old cap's wording, on a pooled kind
(instagram, x — frozen HERE as of this migration, deliberately not
imported: a kind pooled later has no 4-file history to heal). The fixed
engine writes that wording only for kinds still under the tight cap, so a
pooled-kind line carrying it is pre-fix by construction — no engine
version check needed, and the ``media cap (100 files) reached`` a pooled
backstop would write can never qualify.

Two seeds per truncated Instagram post, one per truncated X unit:

- **The refused media unit requeues** (``queued, rerun, via:
  migration-9``, job/parent/depth preserved). Necessary even where the
  parent reruns: the media stage skips every URL whose unit already
  exists, whatever its status, so a re-walked carousel would step right
  over its own skipped slide.
- **The Instagram parent post reseeds too**: the old driver's probe walk
  stopped at index 5, so slides 6+ were never ledgered at all — only a
  re-fetch can discover them. X needs no parent seed: its driver always
  emitted the full pooled list; only the downloads were refused. Parents
  seed BEFORE children, so an apply interrupted between the two leaves
  the children still-skipped — still members — and the re-apply resumes;
  a parent whose live line is already queued is not seeded twice.

Which live item a seed writes under follows migration 2's rule: the
stored ``item`` answers where its corpus file still exists; a renamed
item is found through the corpus's own claim on the PARENT post's URL
(media URLs are never listed in ``urls:``, the post is); an item nothing
claims is a purge honored on the record — skipped-with-why, naming the
``state/exclusions.tsv`` entry where one exists.

Idempotent: a seeded member's live line is queued (then done), never
``skipped`` at the old cap, so a second apply finds no members. Slot
names hold: a media unit's file index is its position among the item's
media units in ledger order, and a reseed reuses the unit's own hash, so
requeued downloads land in their original slots beside the files already
kept, and newly discovered slides append after them.
"""

import datetime
from collections.abc import Callable
from pathlib import Path

from dex_engine.pipeline.ledger import (
    LedgerSchemaError,
    append,
    from_line,
    resolution_key,
    stamp,
)
from dex_engine.pipeline.ownership import corpus_owners
from dex_engine.pipeline.registry import default_drivers
from dex_engine.pipeline.types import (
    Job,
    Kind,
    LedgerEntry,
    MigrationReport,
    Skipped,
    Status,
)

__all__ = ["PooledMediaCapReseed", "build"]

NUMBER = 9
INTENT = (
    "reseed pooled-kind media the 4-file cap refused: every instagram/x media unit "
    "skipped at the old cap requeues as {queued, rerun, via: migration-9}, and each "
    "truncated instagram post reseeds so the probe walk can discover the slides it "
    "never ledgered"
)

# The old tight cap's exact ledger wording — historical, never derived from
# the live constant: the fixed engine still writes this string for kinds
# under the tight cap, and only its appearance on a POOLED kind marks
# pre-fix truncation.
_OLD_CAP_REASON = "media cap (4 files) reached"

# The pooled kinds as of this migration, frozen: a kind admitted to the
# pool later has no 4-file history for this migration to heal.
_POOLED = frozenset({Kind.INSTAGRAM, Kind.X})


def build(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> "PooledMediaCapReseed":
    """Build migration 9; seeds are stamped with the injected clocks and engine."""
    return PooledMediaCapReseed(today=today, now=now, engine_version=engine_version)


class PooledMediaCapReseed:
    """Migration 9: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def __init__(
        self,
        *,
        today: Callable[[], datetime.date],
        now: Callable[[], datetime.datetime],
        engine_version: str,
    ) -> None:
        """Seeds are stamped with the injected clocks and running engine."""
        self._today = today
        self._now = now
        self._engine_version = engine_version

    def apply(self, root: Path) -> MigrationReport:
        """Requeue every cap-refused pooled media unit, and re-walk its post.

        Args:
            root: The instance root.

        Returns:
            The report: one summary action with the seed counts, a skip
            for every member no live item claims, and an anomaly for a
            member whose parent line the ledger no longer holds.
        """
        actions: list[str] = []
        skipped: list[Skipped] = []
        anomalies: list[str] = []
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport()
        latest = _latest_per_hash(path, skipped, now=self._now())
        members = [
            entry
            for entry in latest.values()
            if entry.job is Job.MEDIA
            and entry.status is Status.SKIPPED
            and entry.reason == _OLD_CAP_REASON
            and entry.kind in _POOLED
        ]
        if not members:
            return MigrationReport(actions=actions, skipped=skipped, anomalies=anomalies)
        exclusions = _exclusions(root)
        # The corpus scan answers one question — which live item claims a
        # renamed member's work — and only a member whose stored item file
        # is gone needs to ask it.
        owners = (
            corpus_owners(root, default_drivers())
            if any(not (root / _item_path(entry.item)).exists() for entry in members)
            else {}
        )
        seeds: list[tuple[LedgerEntry, LedgerEntry | None]] = []
        for entry in members:
            parent = latest.get(entry.parent) if entry.parent else None
            if parent is None:
                anomalies.append(
                    f"media unit {entry.hash} ({entry.url}) is skipped at the old cap but "
                    f"its parent {entry.parent or '(none recorded)'} has no ledger line — "
                    "requeue by hand with `enrich mark` if the post should re-walk"
                )
                continue
            item = _live_item(entry, parent=parent, root=root, owners=owners)
            if item is None:
                skipped.append(_unclaimed(entry, exclusions))
                continue
            seeds.append((_member_seed(entry, item), _parent_seed(parent, item)))
        counts = {Kind.INSTAGRAM: 0, Kind.X: 0}
        parents_seeded: set[str] = set()
        # Parents first: an apply interrupted before the children leaves
        # them still-skipped — still members — so the re-apply resumes.
        for _child, parent in seeds:
            if parent is None or parent.hash in parents_seeded:
                continue
            append(path, self._stamped(parent))
            parents_seeded.add(parent.hash)
        for child, _parent in seeds:
            append(path, self._stamped(child))
            counts[child.kind] += 1
        total = counts[Kind.INSTAGRAM] + counts[Kind.X]
        if total:
            actions.append(
                f"seeded {total} cap-refused media rerun(s) ({counts[Kind.INSTAGRAM]} "
                f"instagram, {counts[Kind.X]} x) and {len(parents_seeded)} instagram post "
                "re-walk(s) — the pooled cap fetches what the 4-file cap refused"
            )
        return MigrationReport(actions=actions, skipped=skipped, anomalies=anomalies)

    def _stamped(self, entry: LedgerEntry) -> LedgerEntry:
        return stamp(entry, today=self._today, now=self._now, engine_version=self._engine_version)


def _member_seed(entry: LedgerEntry, item: str) -> LedgerEntry:
    """The member's requeue line: its own identity and lineage, queued again."""
    return LedgerEntry(
        hash=entry.hash,
        url=entry.url,
        item=item,
        kind=entry.kind,
        status=Status.QUEUED,
        engine="seed",  # stamped in apply
        date=datetime.date.min,
        job=Job.MEDIA,
        parent=entry.parent,
        depth=entry.depth,
        via="migration-9",
        rerun=True,
    )


def _parent_seed(parent: LedgerEntry, item: str) -> LedgerEntry | None:
    """The post re-walk seed — instagram only, and never over a queued line.

    X posts never re-walk (the driver emitted the full pooled list; only
    downloads were refused), and a parent already queued — a prior
    interrupted apply, or an owner's own requeue — is already going to
    re-fetch.
    """
    if parent.kind is not Kind.INSTAGRAM or parent.status is Status.QUEUED:
        return None
    return LedgerEntry(
        hash=parent.hash,
        url=parent.url,
        item=item,
        kind=parent.kind,
        status=Status.QUEUED,
        engine="seed",  # stamped in apply
        date=datetime.date.min,
        # A harvest-promoted post carries lineage of its own — parent and
        # depth travel together, so both ride the seed.
        parent=parent.parent,
        depth=parent.depth,
        via="migration-9",
        rerun=True,
    )


def _latest_per_hash(
    path: Path, skipped: list[Skipped], *, now: datetime.datetime
) -> dict[str, LedgerEntry]:
    """Every hash's live line, read tolerantly, resolved as ``load`` resolves.

    Line by line rather than through ``ledger.load``: one line this
    migration has no business touching must not stop it healing the ones
    it does.
    """
    latest: dict[str, LedgerEntry] = {}
    winning: dict[str, tuple[datetime.datetime, int]] = {}
    for position, line in enumerate(path.read_text(encoding="utf-8").split("\n")):
        if not line.strip():
            continue
        try:
            entry = from_line(line)
        except (LedgerSchemaError, ValueError):
            skipped.append(
                Skipped(
                    what=f"ledger line {position + 1}",
                    why="does not parse — left untouched; the cap reseed skipped it",
                )
            )
            continue
        key = resolution_key(entry, position, now=now)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        latest[entry.hash] = entry
        winning[entry.hash] = key
    return latest


def _exclusions(root: Path) -> dict[str, str]:
    """``state/exclusions.tsv`` as item id -> stated reason, tolerantly read."""
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


def _item_path(item: str) -> str:
    return f"corpus/{item[:4]}/{item}.md"


def _live_item(
    entry: LedgerEntry,
    *,
    parent: LedgerEntry,
    root: Path,
    owners: dict[str, str],
) -> str | None:
    """The live corpus item the seeds write under, or None to refuse them.

    The stored ``item`` answers first. A renamed item is found through the
    corpus's claim on the PARENT post's URL — the post is what ``urls:``
    lists; a signed media URL never is, so the member's own hash finds
    nothing there by design.
    """
    if (root / _item_path(entry.item)).exists():
        return entry.item
    return owners.get(parent.hash)


def _unclaimed(entry: LedgerEntry, exclusions: dict[str, str]) -> Skipped:
    item_path = _item_path(entry.item)
    if entry.item in exclusions:
        reason = exclusions[entry.item] or "no reason recorded"
        why = (
            f"{item_path} is gone and the item is excluded on the record "
            f"(state/exclusions.tsv: {reason}) — excluded, never reseeded"
        )
    else:
        why = (
            f"{item_path} does not exist, no state/exclusions.tsv record names the item, "
            f"and no live corpus item lists the parent post of {entry.url} — nothing "
            "claims this work, so the reseed would have no item to write under; not "
            "reseeded. If the item was renamed and its urls: no longer carries the post, "
            "requeue by hand with `enrich mark`"
        )
    return Skipped(what=f"cap reseed for {entry.item}", why=why)
