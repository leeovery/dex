"""Migration 12 — re-walk the instagram posts whose stills the park dropped.

A carousel holding any video parked for transcription with the first
video as its enclosure and DROPPED every image: the stills were never
ledgered, never downloaded, and the whole loss was recorded as
``images_dropped: <n>`` in the park file's frontmatter, which nothing
reads. The driver now carries those stills as media — but a fetch unit
that has landed is never re-fetched, so on every existing instance the
images stay undiscovered, and nothing but that count even remembers they
existed.

The count is therefore the membership test, and it is exact: the live
line of an instagram POST (``job`` unset — never a media child) whose
enrichment file's frontmatter carries ``images_dropped``. The fixed
driver never writes the key again, so a file carrying it is pre-fix by
construction and no engine-version check is needed; and the key survives
transcription — the drain preserves the park's meta — so an affected
post is found whatever its status.

One seed per member: the post itself, ``queued, rerun, via:
migration-12``, parent/depth preserved. The re-walk is a full re-fetch,
which has consequences the owner must weigh — the enrichment file is
rewritten from the caption, the video is transcribed again, and the
stills land in media slots beside any description already written — so
every reseeded post gets its own report line naming them.

Which live item a seed writes under follows migration 2's rule: the
stored ``item`` answers where its corpus file still exists; a renamed
item is found through the corpus's own claim on the post URL; an item
nothing claims is a purge honored on the record — skipped-with-why,
naming the ``state/exclusions.tsv`` entry where one exists.

Idempotent: the rerun rewrites the enrichment file without the key, so a
second apply finds no members, and a post whose live line is already
queued — an apply interrupted before its rerun, or the owner's own
requeue — is never seeded over.
"""

import datetime
from collections.abc import Callable
from pathlib import Path

from dex_engine.pipeline.enrichment import read_enrichment_fields
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
    Kind,
    LedgerEntry,
    MigrationReport,
    Skipped,
    Status,
)

__all__ = ["DroppedStillsReseed", "build"]

NUMBER = 12
INTENT = (
    "re-walk the instagram posts whose stills the transcribe park dropped: every post "
    "whose enrichment file still records images_dropped requeues as {queued, rerun, via: "
    "migration-12}, so the fixed driver ledgers the images it once threw away"
)

# The park's own record of the loss, and the only trace of it: the fixed
# driver never writes this key, so its presence dates the file.
_DROPPED_FIELD = "images_dropped"


def build(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> "DroppedStillsReseed":
    """Build migration 12; seeds are stamped with the injected clocks and engine."""
    return DroppedStillsReseed(today=today, now=now, engine_version=engine_version)


class DroppedStillsReseed:
    """Migration 12: see the module docstring."""

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
        """Requeue every instagram post whose park recorded dropped stills.

        Args:
            root: The instance root.

        Returns:
            The report: a summary action with the count, one action per
            reseeded post stating what the re-walk rewrites, a skip for
            every member no live item claims, and an anomaly for every
            enrichment file that does not parse.
        """
        skipped: list[Skipped] = []
        anomalies: list[str] = []
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport()
        latest = _latest_per_hash(path, skipped, now=self._now())
        posts = [
            entry
            for entry in latest.values()
            if entry.kind is Kind.INSTAGRAM
            and entry.job is None
            and entry.status is not Status.QUEUED
        ]
        if not posts:
            return MigrationReport(skipped=skipped)
        # The corpus scan answers one question — which live item claims a
        # renamed post's work — and only a post whose stored item file is
        # gone needs to ask it.
        owners = (
            corpus_owners(root, default_drivers())
            if any(not (root / _item_path(entry.item)).exists() for entry in posts)
            else {}
        )
        exclusions = _exclusions(root)
        actions: list[str] = []
        seeds: list[tuple[LedgerEntry, str]] = []
        for entry in posts:
            item = _live_item(entry, root=root, owners=owners)
            # A member whose live item is gone is still read from the
            # stored id: its park file outlives the corpus file, and the
            # loss it records is what decides whether to report at all.
            dropped = _dropped_stills(root, entry, item or entry.item, anomalies)
            if dropped is None:
                continue
            if item is None:
                skipped.append(_unclaimed(entry, exclusions))
                continue
            seeds.append((entry, item))
            actions.append(_consequences(item, dropped))
        for entry, item in seeds:
            append(path, self._stamped(_post_seed(entry, item)))
        if not seeds:
            return MigrationReport(skipped=skipped, anomalies=anomalies)
        summary = (
            f"seeded {len(seeds)} instagram post re-walk(s) — the fixed driver keeps a "
            "post's stills instead of dropping them for its video"
        )
        return MigrationReport(actions=[summary, *actions], skipped=skipped, anomalies=anomalies)

    def _stamped(self, entry: LedgerEntry) -> LedgerEntry:
        return stamp(entry, today=self._today, now=self._now, engine_version=self._engine_version)


def _consequences(item: str, dropped: str) -> str:
    """What the owner is agreeing to by letting this post re-walk."""
    return (
        f"{item}: {dropped} still(s) return — the re-fetch rewrites the enrichment file "
        "from the caption and transcribes the video again (a silent reel re-parks manual "
        "and wants closing with `enrich mark`); an existing media-N.md in "
        f"enrichment/{item}/ now pairs with reseeded slide N — check it describes that slide"
    )


def _post_seed(entry: LedgerEntry, item: str) -> LedgerEntry:
    """The post's requeue line: its own identity and lineage, queued again."""
    return LedgerEntry(
        hash=entry.hash,
        url=entry.url,
        item=item,
        kind=entry.kind,
        status=Status.QUEUED,
        engine="seed",  # stamped in apply
        date=datetime.date.min,
        # A harvest-promoted post carries lineage of its own — parent and
        # depth travel together, so both ride the seed.
        parent=entry.parent,
        depth=entry.depth,
        via="migration-12",
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
                    why="does not parse — left untouched; the stills reseed skipped it",
                )
            )
            continue
        key = resolution_key(entry, position, now=now)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        latest[entry.hash] = entry
        winning[entry.hash] = key
    return latest


def _dropped_stills(root: Path, entry: LedgerEntry, item: str, anomalies: list[str]) -> str | None:
    """The post's recorded still count, or None when the post is no member."""
    path = root / _enrichment_path(entry, item)
    if not path.is_file():
        return None
    try:
        fields = read_enrichment_fields(path)
    except ValueError as e:
        anomalies.append(
            f"{_enrichment_path(entry, item)} does not parse ({e}) — {entry.url} was left "
            "alone; repair the file, then requeue the post with `enrich mark` if its "
            "frontmatter records images_dropped"
        )
        return None
    return fields.get(_DROPPED_FIELD)


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


def _enrichment_path(entry: LedgerEntry, item: str) -> str:
    return f"enrichment/{item}/{entry.kind.value}-{entry.hash[:6]}.md"


def _live_item(entry: LedgerEntry, *, root: Path, owners: dict[str, str]) -> str | None:
    """The live corpus item the seed writes under, or None to refuse it.

    The stored ``item`` answers first; a renamed item is found through the
    corpus's own claim on the post URL, which is what ``urls:`` lists.
    """
    if (root / _item_path(entry.item)).exists():
        return entry.item
    return owners.get(entry.hash)


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
            f"and no live corpus item lists {entry.url} — nothing claims this work, so the "
            "reseed would have no item to write under; not reseeded. If the item was "
            "renamed and its urls: no longer carries the post, requeue by hand with "
            "`enrich mark`"
        )
    return Skipped(what=f"stills reseed for {entry.item}", why=why)
