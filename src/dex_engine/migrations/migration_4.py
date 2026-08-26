"""Migration 4 — requeue x posts stored as a bare link to their own article.

On 0.1.0 the x driver read a post's ``text`` before its ``article``. For an
ordinary post that is right; for a long-form article's announcement it was
the whole loss. fxtwitter expands x's shortlinks, so such a post arrives
with ``text`` already reading ``https://x.com/i/article/<id>`` — truthy,
and past the guard that refused a body which was nothing but a ``t.co``.
The unit ledgered ``done`` on a two-line enrichment file holding an
attribution and a URL, while the article's whole body sat unread under
``article.content.blocks[]`` in the same response. Nothing raised, nothing
parked, and the run report named the item as enriched.

``done`` is terminal for the drain, so the fixed driver never revisits
those units on its own: they need seeding, and that is all this migration
does.

**Membership is read off the stored file, not off a vintage.** Migration 2
could ask "was this written by the pre-rewrite engine", because the fixes
it seeded for shipped with the rewrite. This defect shipped INSIDE 0.1.0
and was not present in every x post that version wrote — most posts have
no article at all. Requeuing every ``done`` x unit would re-walk thousands
of threads to repair a handful of stubs. So the cohort is exactly the
units whose stored body IS the stub the broken driver wrote: every line an
attribution, a media note, or a bare link, and one of those links an x
article URL. Requiring the stub SHAPE, not merely the URL's presence, is
what makes the apply a no-op on already-migrated state: the repaired body
the rerun writes keeps the article link at its tail (harvest reads links
out of stored bodies), so "contains an article URL" would re-seed repaired
work on every log-race re-application, forever. A prose post that merely
links someone else's article fails the shape test too — nothing of it was
lost, so nothing of it is re-fetched.

An entry whose file is missing or unreadable is left alone and reported:
the point is to repair a body we can see, and a body we cannot read is
not evidence of anything.

The live-item check is migration 2's, deliberately re-implemented rather
than imported. A migration is a frozen historical act; reaching into
another one's internals would let a later edit to migration 2 silently
change what migration 4 did to instances that already ran it.

Idempotent: a seeded hash's latest line is the queued seed, no longer
``done``, so a second apply finds nothing in the cohort.
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
)

__all__ = ["ArticleStubRequeue", "build"]

NUMBER = 4
INTENT = (
    "requeue x posts stored as a bare link to their own article: the driver read `text` "
    "before `article`, so an announcement ledgered done on a two-line file while the "
    "article's body sat unread in the same response"
)

# The article link inside a stub. Both spellings occur: the id-keyed form
# fxtwitter hands over, and the username form a post may carry verbatim.
_ARTICLE_URL_RE = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/[^/\s]+/article/\d+")

# The three line shapes the broken driver could write into a stub, and
# nothing else: the attribution line, a bare link standing where the body
# should be, and the media stand-in note. One line of anything else is
# prose, and prose means the body is not a stub.
_ATTRIBUTION_LINE_RE = re.compile(r"@\S+ — .+")
_BARE_LINK_LINE_RE = re.compile(r"https?://\S+")
_MEDIA_NOTE_LINE_RE = re.compile(r"\((?:photo|video|media) post\)")


def build(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> "ArticleStubRequeue":
    """Build migration 4 with its injected clocks and the running version."""
    return ArticleStubRequeue(today=today, now=now, engine_version=engine_version)


class ArticleStubRequeue:
    """Migration 4: see the module docstring."""

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
        """Seed a rerun for every x unit whose stored body is an article link.

        Args:
            root: The instance root.

        Returns:
            The report: one action naming the seeded count, and a skip for
            every stub whose file could not be read or whose work no live
            corpus item claims.
        """
        actions: list[str] = []
        skipped: list[Skipped] = []
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport()
        latest = _latest_entries(path, skipped, now=self._now())
        cohort = [entry for entry in latest if _is_article_stub(root, entry, skipped)]
        if not cohort:
            return MigrationReport(actions=actions, skipped=skipped)
        drivers = default_drivers()
        # The corpus scan answers one question — which live item claims this
        # work — and only entries whose stored item is already gone need to
        # ask it, so a run with nothing missing never pays for it.
        owners = (
            corpus_owners(root, drivers)
            if any(not _item_file(root, entry.item).exists() for entry in cohort)
            else {}
        )
        seeded = 0
        reattributed = 0
        for entry in cohort:
            item = _live_item(entry, root=root, owners=owners, skipped=skipped)
            if item is None:
                continue
            append(
                path,
                stamp(
                    LedgerEntry(
                        hash=entry.hash,
                        url=entry.url,
                        item=item,
                        kind=entry.kind,
                        status=Status.QUEUED,
                        engine="seed",  # stamped below
                        date=datetime.date.min,
                        via="migration-4",
                        rerun=True,
                    ),
                    today=self._today,
                    now=self._now,
                    engine_version=self._engine_version,
                ),
            )
            seeded += 1
            if item != entry.item:
                reattributed += 1
        action = (
            f"seeded {seeded} rerun(s): x posts whose stored body is a bare link to an "
            "article the same fetch could have read"
        )
        if reattributed:
            action += (
                f"; {reattributed} re-attributed to the renamed item whose urls: still lists them"
            )
        actions.append(action)
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
                    why="does not parse — left untouched; the article-stub sweep skipped it",
                )
            )
            continue
        key = resolution_key(entry, position, now=now)
        held = best.get(entry.hash)
        if held is None or key > held[0]:
            best[entry.hash] = (key, entry)
    return [entry for _, entry in best.values()]


def _is_article_stub(root: Path, entry: LedgerEntry, skipped: list[Skipped]) -> bool:
    """Is this a done x page whose stored body is an article stub?

    A ``job`` unit is never a member: a media download has no article to
    have missed, and its bytes are regenerated by the parent's own rerun.
    """
    if entry.job is not None or entry.status is not Status.DONE or entry.kind is not Kind.X:
        return False
    if entry.path is None:
        return False
    file = root / entry.path
    if not file.exists():
        return False
    try:
        _, body = read_enrichment(file)
    except (OSError, UnicodeDecodeError):
        skipped.append(
            Skipped(
                what=f"enrichment file for unit {entry.hash}",
                why="could not be read, so whether it is an article stub is unknown — "
                "re-fetch by hand with `enrich fetch --force` if its body is a bare link",
            )
        )
        return False
    return _is_stub_body(body)


def _is_stub_body(body: str) -> bool:
    """Whether a stored body is the stub shape, holding an article link.

    Every non-empty line must be one of the three shapes the broken driver
    wrote — attribution, bare link, media note — and at least one line
    must link an x article. The repaired body the rerun writes opens with
    a markdown heading and carries the article's prose, so it fails this
    test by its first real line: that failure is the idempotency.
    """
    has_article_link = False
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _BARE_LINK_LINE_RE.fullmatch(line):
            if _ARTICLE_URL_RE.search(line):
                has_article_link = True
            continue
        if _ATTRIBUTION_LINE_RE.fullmatch(line) or _MEDIA_NOTE_LINE_RE.fullmatch(line):
            continue
        return False
    return has_article_link


def _item_file(root: Path, item: str) -> Path:
    return root / "corpus" / item[:4] / f"{item}.md"


def _live_item(
    entry: LedgerEntry,
    *,
    root: Path,
    owners: dict[str, str],
    skipped: list[Skipped],
) -> str | None:
    """The live corpus item this rerun's output belongs to, or None to refuse it.

    The stored ``item`` answers first, then the corpus's own claim on the
    work — an item renamed since the line was written still lists this URL
    under its new id, and that is a re-attribution, not a purge. Seeding
    work nothing claims would re-fetch content the owner purged and mint
    an enrichment directory under a dead id.
    """
    if _item_file(root, entry.item).exists():
        return entry.item
    owner = owners.get(entry.hash)
    if owner is not None:
        return owner
    skipped.append(
        Skipped(
            what=f"article-stub rerun for {entry.item}",
            why=f"the item has no corpus file and no live item lists {entry.url} — nothing "
            "claims this work, so the rerun would have no item to write under. If the "
            "item was renamed and its urls: no longer carries this URL, requeue by hand "
            "with `enrich mark`",
        )
    )
    return None
