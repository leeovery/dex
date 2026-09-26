"""Migration 16 — rerun the x posts whose video was pooled as a file, never heard.

Until the x driver transcribed video, an x post carrying one landed
``done`` with the video pooled as a media child: a file in the item's
directory owing a description, or, past the 10MB ceiling, a ``skipped``
line and no file at all. A talking-head clip was digested from frames,
or not at all, while its substance sat in the audio. The fixed driver
parks such a post for transcription and the transcript joins the post's
own file, but a unit that has landed is never re-fetched, so every post
captured before the fix keeps the old shape until something reruns it.

Membership is read off what the engine stored, and it is exact: the live
line of an x POST (``job`` unset) landed ``done``, with at least one media
child — whatever that child's status — whose URL is an x video, and whose
enrichment file carries no ``enclosure``. The child is the evidence: X's
CDN serves every post video and every gif from ``video.twimg.com``, a gif
(x's silent loop, which never transcribes) under ``/tweet_video/`` and a
video under a path of its own kind (``amplify_video/``, ``ext_tw_video/``)
— a wild-data fact verified against live fxtwitter payloads for both. The
missing ``enclosure`` is what dates the file: the fixed driver writes it
on every post that has a video, as the pointer the transcribe drain reads
back, and the landing keeps it; the old driver never wrote it at all.

One seed per member: the post itself, ``queued, rerun, via:
migration-16``, parent/depth preserved. The stored post is merely
incomplete, so it stays where it is — the rerun rewrites it, and a rerun
whose video can no longer be fetched keeps the post done. When the
transcript lands, the engine retires the video child: its file and any
description of it leave, because the transcript stands for both. A
digest written before then was drawn from the description, or from
nothing, and rewriting it from the transcript is the session's judgment,
so each member's report line says so where a digest exists.

Which live item a seed writes under follows migration 2's rule: the
stored ``item`` answers where its corpus file still exists; a renamed
item is found through the corpus's own claim on the post URL; an item
nothing claims is a purge honored on the record — skipped-with-why,
naming the ``state/exclusions.tsv`` entry where one exists. Migration
12's helpers for all of this are re-implemented rather than imported: a
migration is a frozen historical act.

Idempotent: a seeded post's live line is ``queued``, so an apply before
it drains seeds nothing, and once it drains its file carries the
``enclosure`` that removes it from membership — whether the transcript
landed or the post was kept without one.
"""

import datetime
import urllib.parse
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
    Job,
    Kind,
    LedgerEntry,
    MigrationReport,
    Skipped,
    Status,
)

__all__ = ["UnheardVideoRerun", "build"]

NUMBER = 16
INTENT = (
    "rerun the x posts whose video was pooled as a file and never heard: every done post "
    "with an x video among its media children, whose enrichment file carries no enclosure, "
    "requeues as {queued, rerun, via: migration-16}, so the fixed driver transcribes it"
)

_VIDEO_HOST = "video.twimg.com"
_GIF_PATH = "/tweet_video/"

# The transcribe park's pointer to the video: written on every x post the
# fixed driver finds a video on, never by the driver before it.
_POINTER_FIELD = "enclosure"


def build(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> "UnheardVideoRerun":
    """Build migration 16; seeds are stamped with the injected clocks and engine."""
    return UnheardVideoRerun(today=today, now=now, engine_version=engine_version)


class UnheardVideoRerun:
    """Migration 16: see the module docstring."""

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
        """Requeue every done x post whose video was pooled instead of heard.

        Args:
            root: The instance root.

        Returns:
            The report: a summary action with the count, one action per
            seeded post stating what its rerun changes, a skip for every
            member no live item claims or whose file is gone, and an
            anomaly for every enrichment file that does not parse.
        """
        skipped: list[Skipped] = []
        anomalies: list[str] = []
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport()
        latest = _latest_per_hash(path, skipped, now=self._now())
        posts = _posts_with_video(latest)
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
            # stored id: its file outlives the corpus file, and whether it
            # predates the fix decides whether to report at all.
            if not _unheard(root, entry, item or entry.item, skipped, anomalies):
                continue
            if item is None:
                skipped.append(_unclaimed(entry, exclusions))
                continue
            seeds.append((entry, item))
            actions.append(_consequences(root, entry, item))
        for entry, item in seeds:
            append(path, self._stamped(_post_seed(entry, item)))
        if not seeds:
            return MigrationReport(skipped=skipped, anomalies=anomalies)
        summary = (
            f"seeded {len(seeds)} x post rerun(s) — the fixed driver transcribes a post's "
            "video into the post's own file instead of pooling it as a file to describe"
        )
        return MigrationReport(actions=[summary, *actions], skipped=skipped, anomalies=anomalies)

    def _stamped(self, entry: LedgerEntry) -> LedgerEntry:
        return stamp(entry, today=self._today, now=self._now, engine_version=self._engine_version)


def _is_video(url: str) -> bool:
    """Whether a media child's URL is an x post video, never a gif."""
    parts = urllib.parse.urlsplit(url)
    return parts.hostname == _VIDEO_HOST and not parts.path.startswith(_GIF_PATH)


def _posts_with_video(latest: dict[str, LedgerEntry]) -> list[LedgerEntry]:
    """The done x posts with an x video among their media children, whatever its status."""
    parents = {
        entry.parent
        for entry in latest.values()
        if entry.job is Job.MEDIA and entry.parent is not None and _is_video(entry.url)
    }
    return [
        entry
        for entry in latest.values()
        if entry.hash in parents
        and entry.kind is Kind.X
        and entry.job is None
        and entry.status is Status.DONE
    ]


def _unheard(
    root: Path, entry: LedgerEntry, item: str, skipped: list[Skipped], anomalies: list[str]
) -> bool:
    """Whether the post's stored file predates the fix — it carries no video pointer."""
    rel = _enrichment_path(entry, item)
    if not (root / rel).is_file():
        skipped.append(
            Skipped(
                what=f"video rerun for {entry.url}",
                why=f"{rel} is gone, so whether the post was landed before its video could "
                "be heard is unknown — lint names the missing file; requeue the post with "
                "`enrich mark` to fetch it again",
            )
        )
        return False
    try:
        fields = read_enrichment_fields(root / rel)
    except (ValueError, OSError, UnicodeDecodeError) as e:
        anomalies.append(
            f"{rel} does not parse ({e}) — {entry.url} was left alone; repair the file, then "
            "requeue the post with `enrich mark` if it carries no enclosure"
        )
        return False
    return _POINTER_FIELD not in fields


def _consequences(root: Path, entry: LedgerEntry, item: str) -> str:
    """What the rerun changes for this item, and what it leaves for the session."""
    line = (
        f"{item}: the rerun transcribes the video of {entry.url} into the post's file; when "
        "the transcript lands, the downloaded video and any description of it leave"
    )
    if (root / "state" / "digests" / f"{item}.md").exists():
        line += (
            " — the item's digest was written without the transcript and wants rewriting from it"
        )
    return line


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
        via="migration-16",
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
                    why="does not parse — left untouched; the x video rerun skipped it",
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
            f"(state/exclusions.tsv: {reason}) — excluded, never rerun"
        )
    else:
        why = (
            f"{item_path} does not exist, no state/exclusions.tsv record names the item, "
            f"and no live corpus item lists {entry.url} — nothing claims this work, so the "
            "rerun would have no item to write under; not rerun. If the item was renamed "
            "and its urls: no longer carries the post, requeue by hand with `enrich mark`"
        )
    return Skipped(what=f"video rerun for {entry.item}", why=why)
