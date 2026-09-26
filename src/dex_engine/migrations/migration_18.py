"""Migration 18 — re-extract every page the article seam stored before it kept tables and math.

Four losses sat inside the extractor every fetched page goes through, and
none of them raised: a scroll-area wrapper whose class says ``scrollbar``
was discarded as navigation, taking a docs page's tables and code blocks
with it; every ``<math>`` was deleted along with its TeX, closing the
prose over each value it held; LaTeXML's equation tables stored as rows
of empty cells; and a LaTeXML table typeset as spans went with the figure
floating it, so an appendix of nothing but tables vanished whole. A page
that declares the markdown it was rendered from is now stored from that
markdown as well. The unit ledgered ``done`` on what survived, and
``done`` is terminal for the drain, so the fixed extractor never revisits
those pages on its own.

**Membership is every landing the unfixed seam extracted.** Nothing
stored says whether a page declared a markdown source, wore a scrollbar
wrapper or carried MathML — the losses left no trace in the body, which
is what made them silent — so the cohort cannot be read off the stored
file the way migration 4 reads its stubs. It is the live line of every
page unit (``job`` unset: a media download or an asset write has no text
to re-extract, and a page's rerun regenerates its children anyway) whose
status is ``done``, whose kind fetched through the article seam, and
whose engine is 0.2.1 or older:

- ``web`` — every landing; the web driver reads every page as an
  article.
- ``paper`` — decided by what the paper driver writes into the stored
  file's frontmatter. An arXiv landing records its ``arxiv_id``, and one
  whose full text never arrived records ``note: abstract only`` beside
  it; every engine since the first has written both. The first is a
  member only without the note: its stored text came from arxiv.org/html
  or ar5iv, through the seam. An abstract-only landing never reached the
  extractor, since its rendering was missing or extracted under the 2,000
  characters a full text needs, and a paper's rendering extracts to tens
  of thousands. A landing with no ``arxiv_id`` came from openreview or
  huggingface's papers pages, which the paper driver reads as articles
  through the same seam as a web page, so it is a member too.

A ``manual`` or ``skipped`` unit is not a landing and stays parked for
judgment, as migration 2 left its manual units; a unit still queued,
waiting or blocked will reach the fixed extractor on its own.

**The engine version on the landing line is the vintage, and it is
exact.** The fix and this migration ship together in the first release
after 0.2.1, so every ``done`` line an engine at 0.2.1 or older wrote
came out of the unfixed seam, and every line the fixed engine writes —
this migration's reruns landing, a fresh capture's landing — carries a
newer version. Nothing else separates the two. The stored body cannot
(the losses left no mark, above), and the rerun's provenance cannot
either: ``via: migration-18`` rides the rerun's landing but not a fresh
capture's, so a re-application after a log race would reseed every page
the fixed engine had since fetched on its own. The version keeps that
re-application a no-op, and so does the queued seed itself until it
lands. A line whose engine does not parse cannot be dated, so it is
skipped with why rather than guessed at.

**What the owner pays.** One seed per member, ``queued, rerun, via:
migration-18``, its lineage and http-shared license carried. Nothing is
deleted first: every stored body is merely incomplete, so it stays as the
copy a rerun keeps when its re-fetch fails — a page gone since landing
keeps what it had. The drain takes reruns after all fresh work, at most
``RERUN_DRAIN_CAP`` per run, so a large cohort spreads over runs and
never starves new captures. Each rerun is one fetch of the page (plus
its declared markdown source; for an arXiv paper, the export API and the
rendering). A re-fetch byte-identical to what is stored, ``fetched:``
aside, lands under Already stored and owes nothing; so does one that
shrank under half the stored body, which the drain keeps rather than
trade the article for a stub. Only a page whose stored form changes
comes back under Needs writing up, because its digest was written from
the text extraction lost and the session must read it again. That is
every page this fix changes, every page edited since it landed, and
every page an older engine stored in a form the current one no longer
writes: page preparation arrived in 0.1.1 and the ``description`` line
in 0.1.6, so landings older than those come back rewritten whatever
this fix did to them — which is the point, since a fresh capture of
them would read the same way.

Which live item a seed writes under follows migration 2's rule, asked
of the corpus's own resolution: the stored ``item`` where its corpus file
still exists, else the live item that claims the unit — a harvested page
through its parent chain, since no frontmatter lists it. A unit nothing
live claims is a purge honored on the record: skipped with why, naming
the ``state/exclusions.tsv`` entry where one exists.

Idempotent: a seeded unit's live line is the queued seed, not ``done``,
until it lands, and it lands stamped by the fixed engine, outside the
cohort for good.
"""

import datetime
from collections.abc import Callable, Mapping
from pathlib import Path

from dex_engine.pipeline.enrichment import read_enrichment_fields
from dex_engine.pipeline.ledger import (
    LedgerSchemaError,
    append,
    from_line,
    resolution_key,
    stamp,
)
from dex_engine.pipeline.ownership import unit_owners
from dex_engine.pipeline.registry import default_drivers
from dex_engine.pipeline.types import (
    Kind,
    LedgerEntry,
    MigrationReport,
    Skipped,
    Status,
    parse_version,
)

__all__ = ["ArticleSeamRerun", "build"]

NUMBER = 18
INTENT = (
    "re-extract every page the article seam stored before it kept tables, code and math: "
    "each done web page, and each paper whose text came through it, landed by engine 0.2.1 "
    "or older requeues as {queued, rerun, via: migration-18}"
)

# The last engine whose article seam lost what the fix keeps. The fix
# ships in the release after it, so the version on a landing dates it.
_LAST_UNFIXED = (0, 2, 1)
_SEAM_KINDS = frozenset({Kind.WEB, Kind.PAPER})

# What the paper driver writes into an arXiv landing's frontmatter: the
# id always, and the note only when the full text never arrived.
_ARXIV_ID_FIELD = "arxiv_id"
_NOTE_FIELD = "note"
_ABSTRACT_ONLY = "abstract only"


def build(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> "ArticleSeamRerun":
    """Build migration 18; seeds are stamped with the injected clocks and engine."""
    return ArticleSeamRerun(today=today, now=now, engine_version=engine_version)


class ArticleSeamRerun:
    """Migration 18: see the module docstring."""

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
        """Requeue every page landing the unfixed article seam extracted.

        Args:
            root: The instance root.

        Returns:
            The report: a summary action with the counts and what the
            reruns cost, a skip for every member no live item claims and
            every line that cannot be read or dated, and an anomaly for
            every paper file whose frontmatter does not parse.
        """
        skipped: list[Skipped] = []
        anomalies: list[str] = []
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport()
        latest = _latest_per_hash(path, skipped, now=self._now())
        members = [
            entry
            for entry in latest.values()
            if _is_unfixed_landing(entry, skipped) and _went_through_seam(root, entry, anomalies)
        ]
        if not members:
            return MigrationReport(skipped=skipped, anomalies=anomalies)
        # The corpus resolution answers one question — which live item owns
        # a unit whose stored item is gone — and only such a unit asks it.
        owners = (
            unit_owners(root, latest, default_drivers())
            if any(not _item_file(root, entry.item).exists() for entry in members)
            else {}
        )
        exclusions = _exclusions(root)
        seeds: list[tuple[LedgerEntry, str]] = []
        for entry in members:
            item = _live_item(entry, root=root, owners=owners)
            if item is None:
                skipped.append(_unclaimed(entry, exclusions))
                continue
            seeds.append((entry, item))
        for entry, item in seeds:
            append(path, self._stamped(_seed(entry, item)))
        if not seeds:
            return MigrationReport(skipped=skipped, anomalies=anomalies)
        return MigrationReport(actions=[_summary(seeds)], skipped=skipped, anomalies=anomalies)

    def _stamped(self, entry: LedgerEntry) -> LedgerEntry:
        return stamp(entry, today=self._today, now=self._now, engine_version=self._engine_version)


def _is_unfixed_landing(entry: LedgerEntry, skipped: list[Skipped]) -> bool:
    """A page unit landed ``done`` through the seam's kinds by an unfixed engine."""
    if entry.job is not None or entry.status is not Status.DONE:
        return False
    if entry.kind not in _SEAM_KINDS:
        return False
    try:
        return parse_version(entry.engine) <= _LAST_UNFIXED
    except ValueError:
        skipped.append(
            Skipped(
                what=f"ledger entry {entry.hash}",
                why=f"done {entry.kind.value} landing with unparseable engine {entry.engine!r} "
                "— cannot tell whether the unfixed extractor stored it; requeue it by hand "
                "with `enrich mark` if its body lacks tables, code or math the page has",
            )
        )
        return False


def _went_through_seam(root: Path, entry: LedgerEntry, anomalies: list[str]) -> bool:
    """Whether the landing's stored text came out of the article seam."""
    if entry.kind is Kind.WEB:
        return True
    if entry.path is None or not (root / entry.path).is_file():
        return False
    try:
        fields = read_enrichment_fields(root / entry.path)
    except (OSError, ValueError) as e:
        anomalies.append(
            f"{entry.path} does not parse ({e}) — {entry.url} was left alone; repair the "
            "file, then requeue the paper with `enrich mark` if its text came from its "
            "HTML rendering"
        )
        return False
    return _ARXIV_ID_FIELD not in fields or fields.get(_NOTE_FIELD) != _ABSTRACT_ONLY


def _seed(entry: LedgerEntry, item: str) -> LedgerEntry:
    """The unit's rerun line: its own identity and lineage, queued again."""
    return LedgerEntry(
        hash=entry.hash,
        url=entry.url,
        item=item,
        kind=entry.kind,
        status=Status.QUEUED,
        # The license for the TLS-failure http fallback rides every line
        # that supersedes the admission's, or the rerun loses it.
        http_shared=entry.http_shared,
        engine="seed",  # stamped in apply
        date=datetime.date.min,
        parent=entry.parent,
        depth=entry.depth,
        via="migration-18",
        rerun=True,
    )


def _summary(seeds: list[tuple[LedgerEntry, str]]) -> str:
    pages = sum(1 for entry, _ in seeds if entry.kind is Kind.WEB)
    papers = len(seeds) - pages
    reattributed = sum(1 for entry, item in seeds if item != entry.item)
    summary = (
        f"seeded {len(seeds)} rerun(s) — {pages} web page(s), {papers} paper(s) — landed "
        "before the extractor kept tables, code blocks and math; they drain after fresh "
        "work, a page that re-fetches unchanged lands under Already stored owing nothing, "
        "and a rewritten one comes back under Needs writing up because its digest was "
        "written from what extraction lost"
    )
    if reattributed:
        summary += f"; {reattributed} re-attributed to the live item that claims the unit"
    return summary


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
                    why="does not parse — left untouched; the article-seam rerun skipped it",
                )
            )
            continue
        key = resolution_key(entry, position, now=now)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        latest[entry.hash] = entry
        winning[entry.hash] = key
    return latest


def _item_file(root: Path, item: str) -> Path:
    return root / "corpus" / item[:4] / f"{item}.md"


def _live_item(
    entry: LedgerEntry, *, root: Path, owners: Mapping[str, tuple[str, ...]]
) -> str | None:
    """The live corpus item the seed writes under, or None to refuse it."""
    if _item_file(root, entry.item).exists():
        return entry.item
    for owner in owners.get(entry.hash, ()):
        if _item_file(root, owner).exists():
            return owner
    return None


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


def _unclaimed(entry: LedgerEntry, exclusions: Mapping[str, str]) -> Skipped:
    item_path = f"corpus/{entry.item[:4]}/{entry.item}.md"
    if entry.item in exclusions:
        reason = exclusions[entry.item] or "no reason recorded"
        why = (
            f"{item_path} is gone and the item is excluded on the record "
            f"(state/exclusions.tsv: {reason}) — excluded, never requeued"
        )
    else:
        why = (
            f"{item_path} does not exist, no state/exclusions.tsv record names the item, and "
            f"no live corpus item claims {entry.url} — nothing would own the rerun, so it was "
            "not requeued. If the item was renamed and no longer lists the page, requeue it "
            "by hand with `enrich mark`"
        )
    return Skipped(what=f"article-seam rerun for {entry.item}", why=why)
