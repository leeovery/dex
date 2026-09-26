"""Migration 17 — rerun the x posts whose long-form article landed without its entities.

Until the x driver read an article's ``content.entityMap``, it rendered
only the article's prose blocks. Every ``atomic`` block carries a lone
space as its text, so each one was stripped away: fenced code listings,
figures, embedded posts and dividers. A link kept its anchor text and
lost its target, so harvest never saw it. The figures and the cover were
never pooled, so none downloaded. The fixed driver renders all of it,
but a unit that has landed is never re-fetched, so every article captured
before the fix keeps the lossy body until something reruns it.

Membership is read off what the engine stored. A member is the live line
of an x POST (``job`` unset) landed ``done``, by an engine older than the
one applying this migration, whose stored body carries an article.

**The article test.** The driver writes an article as a post's text: its
``# <title>`` heading first, then the blocks, then the announcement's own
text. A post's text always follows its attribution line (``@<handle> —
<date>``) and a blank line, and a quoted post's follows ``> Quoting
@<handle>: ``. So the article's mark is a ``# `` heading at the head of a
post's text or of a quote, which nothing else the driver writes puts
there: an ordinary post's text is what its author typed, and X's own
text carries hashtags as ``#tag``, never a markdown heading. The one post
that could pass is one whose author typed a line opening ``# `` with a
space. A rerun of it re-fetches and lands the same file unchanged, so the
test prefers that over missing an article. Every article carries a title,
and so every one carries the heading.

**The engine test**, and why it is the engine rather than a shape. The
fixed renderer has no shape it always produces: an article with no
entities, no code block and no multi-line quote renders byte for byte as
the old renderer rendered it, so no line of a body can say which engine
wrote it. The landing line can. Sync runs pending migrations first,
under the engine it has just moved to, before anything else touches
state, so at the first apply every landed article was written by an
older engine, which is to say before the fix. Each rerun lands under the
running engine, so a second apply, after an interrupted sync or a log
race, finds no member it already seeded. A seeded post whose live line
is still ``queued`` is never a member either, because it is not
``done``. That covers a post that is both an article and a video post,
which migration 16 may have queued first.

One seed per member: the post itself, ``queued, rerun, via:
migration-17``, parent/depth preserved. The stored body is merely
incomplete, so it stays where it is until the rerun rewrites it, and a
rerun whose re-fetch fails keeps it. The digest a session wrote from the
lossy body read neither the code nor the figures, so rewriting it once
the rerun lands is the session's judgment, and each member's report line
says so where a digest exists.

Which live item a seed writes under follows migration 2's rule: the
stored ``item`` answers where its corpus file still exists; a renamed
item is found through the corpus's own claim on the post URL; an item
nothing claims is a purge honored on the record — skipped-with-why,
naming the ``state/exclusions.tsv`` entry where one exists. Migration
12's helpers for all of this are re-implemented rather than imported: a
migration is a frozen historical act.
"""

import datetime
import re
from collections.abc import Callable
from pathlib import Path

from dex_engine.pipeline.enrichment import read_enrichment
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
    parse_version,
)

__all__ = ["ArticleEntitiesRerun", "build"]

NUMBER = 17
INTENT = (
    "rerun the x posts whose long-form article landed without its entities: every done "
    "post whose stored body carries an article, landed by an older engine, requeues as "
    "{queued, rerun, via: migration-17}, so the fixed driver renders its code, links, "
    "figures and embedded posts"
)

_ARTICLE_HEADING_RE = re.compile(r"^(?:@\S+ — [^\n]+\n\n|> Quoting @\S+: )# \S", re.MULTILINE)


def build(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> "ArticleEntitiesRerun":
    """Build migration 17; seeds are stamped with the injected clocks and engine."""
    return ArticleEntitiesRerun(today=today, now=now, engine_version=engine_version)


class ArticleEntitiesRerun:
    """Migration 17: see the module docstring."""

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
        """Requeue every done x post whose stored article an older engine rendered.

        Args:
            root: The instance root.

        Returns:
            The report: a summary action with the count, one action per
            seeded post stating what its rerun changes, a skip for every
            member no live item claims or whose file is gone, and an
            anomaly for every enrichment file that cannot be read.
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
            if entry.kind is Kind.X
            and entry.job is None
            and entry.status is Status.DONE
            and _older(entry.engine, self._engine_version)
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
            # stored id: its file outlives the corpus file, and whether it
            # holds an article decides whether to report at all.
            if not _carries_article(root, entry, item or entry.item, skipped, anomalies):
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
            f"seeded {len(seeds)} x article rerun(s) — the fixed driver renders an article's "
            "code, links, figures and embedded posts, and pools its figures"
        )
        return MigrationReport(actions=[summary, *actions], skipped=skipped, anomalies=anomalies)

    def _stamped(self, entry: LedgerEntry) -> LedgerEntry:
        return stamp(entry, today=self._today, now=self._now, engine_version=self._engine_version)


def _older(landed: str, running: str) -> bool:
    """Whether a line was landed by an engine older than the running one.

    A version that does not parse was never written by an engine that
    carries this fix, which always stamps its own valid version.
    """
    try:
        return parse_version(landed) < parse_version(running)
    except ValueError:
        return True


def _carries_article(
    root: Path, entry: LedgerEntry, item: str, skipped: list[Skipped], anomalies: list[str]
) -> bool:
    """Whether the post's stored body carries a long-form article."""
    rel = _enrichment_path(entry, item)
    if not (root / rel).is_file():
        skipped.append(
            Skipped(
                what=f"article rerun for {entry.url}",
                why=f"{rel} is gone, so whether the post carries an article is unknown — "
                "lint names the missing file; requeue the post with `enrich mark` to fetch "
                "it again",
            )
        )
        return False
    try:
        _fields, body = read_enrichment(root / rel)
    except (OSError, UnicodeDecodeError) as e:
        anomalies.append(
            f"{rel} cannot be read ({e}) — {entry.url} was left alone; repair the file, then "
            "requeue the post with `enrich mark` if it carries an article"
        )
        return False
    return _ARTICLE_HEADING_RE.search(body) is not None


def _consequences(root: Path, entry: LedgerEntry, item: str) -> str:
    """What the rerun changes for this item, and what it leaves for the session."""
    line = (
        f"{item}: the rerun re-reads the article of {entry.url} — its code, link targets, "
        "figures and embedded posts land in the post's file, and its figures download beside it"
    )
    if (root / "state" / "digests" / f"{item}.md").exists():
        line += " — the item's digest was written without them and wants rewriting"
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
        via="migration-17",
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
                    why="does not parse — left untouched; the x article rerun skipped it",
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
    return Skipped(what=f"article rerun for {entry.item}", why=why)
