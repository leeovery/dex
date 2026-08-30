"""Working an instance from outside: search, fetch, page, capture.

Everything here is mechanical. A search is a plain case-insensitive scan of
what the instance already curated — digests first, then the corpus item
behind each one, then the wiki pages — and what comes back is raw hits in a
stated order, never a ranked answer. Judgment about which of them matter is
the caller's; the curation that makes dumb probes land somewhere structured
was spent at write time.

Instance boundaries survive the read: every row is instance-tagged, every id
is namespaced, and a fan-out concatenates per-instance results in roster
order rather than interleaving them.

Files that will not read or parse are skipped rather than reported: search
answers about the instance as it stands, and lint is what tells the owner a
file is broken.

A read travels back with the one-line next move for it. The words are
``steering``'s; what is decided here is only which of them a result gets.

The one write is a capture, and it is the file every other capture client
writes: one markdown file in ``inbox/``, committed where it landed and
judged by the instance when the next run processes it. Nothing here decides
whether a capture belongs in the instance, and nothing here pushes — the
run that processes the inbox is what carries the commit to the remote.
"""

import datetime
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dex_engine import corpus, frontmatter
from dex_engine.pipeline import enrichment
from dex_engine.pipeline.capture import write_capture
from dex_engine.pipeline.types import Instance
from dex_engine.pipeline.urls import resolve_repo_path

from . import steering
from .roster import NotFoundError, Roster, qualify

__all__ = [
    "Capture",
    "Enrichment",
    "Hit",
    "HitType",
    "Item",
    "Page",
    "Search",
    "capture",
    "fetch",
    "page",
    "search",
]

# The snippet window around a match: enough of the sentence in front of it to
# place the hit, more behind it because that is where the claim usually runs.
_SNIPPET_LEAD = 60
_SNIPPET_TAIL = 120

# How many hits one instance may put in front of the caller. A literal scan of
# a real instance answers a broad word like "context" with four figures, which
# is not a result a chat model can read — and the cap is per instance so one
# noisy corpus cannot crowd the others out of a fan-out. Not a ranking: the
# rows are already newest-first, so the cut keeps the newest, and the caller is
# told the whole count either way.
_HITS_PER_INSTANCE = 25

# `YYYY-MM-DD-<slug>-<shortid>`, the item-id shape the whole system files
# under. The slug is the only human-readable name every item carries.
_ITEM_ID_RE = re.compile(r"\d{4}-\d{2}-\d{2}-(?P<slug>.+)-[0-9a-f]{6}")
_HEADING_RE = re.compile(r"^# (.+)$", re.MULTILINE)
# `[[name]]`, and the `[[name|shown as this]]` form a page may be written in
# — the name is what resolves to a file, the alias is display text.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


class HitType(StrEnum):
    """What a search row points at — and therefore which tool opens it."""

    ITEM = "item"
    PAGE = "page"


# The result shapes, unslotted unlike every other frozen dataclass in
# this tree: the SDK builds a tool's output schema by asking the class for
# each field's default, a slot descriptor answers that question, and the
# schema then fails to build — leaving the tool returning text with no
# structured content at all.
@dataclass(frozen=True, kw_only=True)
class Hit:
    """One search row: where the match was, and enough to decide whether to open it."""

    id: str
    type: HitType
    title: str
    date: str | None
    url: str | None
    snippet: str
    instance: str


@dataclass(frozen=True, kw_only=True)
class Search:
    """The hits one query is shown, how many it actually found, and what to do next."""

    hits: list[Hit]
    total: int
    next: str


@dataclass(frozen=True, kw_only=True)
class Enrichment:
    """One fetched source behind an item: what the pipeline pulled and from where."""

    name: str
    url: str | None
    text: str


@dataclass(frozen=True, kw_only=True)
class Item:
    """One corpus item: the instance's reading of it, and where it came from."""

    id: str
    instance: str
    title: str
    date: str
    shared_by: str
    urls: list[str]
    note: str
    digest: str | None
    enrichment: list[Enrichment] | None


@dataclass(frozen=True, kw_only=True)
class Page:
    """One wiki page, verbatim, and the pages it points at."""

    name: str
    instance: str
    title: str
    path: str
    text: str
    wikilinks: list[str]
    next: str


@dataclass(frozen=True, kw_only=True)
class Capture:
    """One capture: the file that landed, and whether the commit followed it.

    ``detail`` is empty when it did and carries git's own words when it did
    not — the file is on disk either way, and a caller told only that the
    commit failed would have no idea the note survived.
    """

    path: str
    instance: str
    committed: bool
    detail: str


def search(roster: Roster, query: str, *, instance: str | None = None) -> Search:
    """Scan the served instances for ``query`` and return every hit.

    Args:
        roster: The served instances.
        query: Plain text, matched case-insensitively and literally.
        instance: Restrict to one instance; ``None`` fans out across all.

    Returns:
        Hits grouped by instance in roster order, newest first within each and
        the newest ``_HITS_PER_INSTANCE`` of them at most, under the count of
        everything that matched and the one line saying what to do next.

    Raises:
        ValueError: The query is empty.
        NotFoundError: ``instance`` names no served instance.
    """
    if not query.strip():
        raise ValueError("search needs a query — an empty one matches everything and says nothing")
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    hits: list[Hit] = []
    total = 0
    for name, served in roster.select(instance):
        found = _newest_first(
            [*_item_hits(name, served, pattern), *_page_hits(name, served, pattern)]
        )
        total += len(found)
        hits.extend(found[:_HITS_PER_INSTANCE])
    return Search(hits=hits, total=total, next=steering.search_next(shown=len(hits), total=total))


def fetch(roster: Roster, item_id: str, *, full: bool) -> Item:
    """Read one item: its digest, the owner's note, and its provenance.

    Args:
        roster: The served instances.
        item_id: A namespaced id, as a search states it.
        full: Also return the fetched source text under ``enrichment/``.
            Stated at every call rather than defaulted here: the tool
            signature is where the caller's default belongs, and two
            defaults for one flag is one too many.

    Returns:
        The item.

    Raises:
        NotFoundError: The id is malformed, names no served instance, or names
            no item in the one it does name.
        ValueError: The corpus file is there but will not parse.
    """
    name, instance, bare = roster.resolve(item_id)
    path = resolve_repo_path(instance.root, f"corpus/{bare[:4]}/{bare}.md")
    if path is None or not path.is_file():
        raise NotFoundError(
            f"{name} holds no item {bare!r} — there is no corpus/{bare[:4]}/{bare}.md"
        )
    try:
        item = corpus.read_item(path)
    except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError) as e:
        raise ValueError(f"item {item_id!r} does not parse: {e}") from e
    # `bare`, never the frontmatter's `id:` — see _item_hits.
    return Item(
        id=qualify(name, bare),
        instance=name,
        title=_item_title(bare),
        date=item.date.isoformat(),
        shared_by=item.shared_by,
        urls=list(item.urls),
        note=item.body.strip(),
        digest=_read(instance.digests_dir / f"{bare}.md") or None,
        enrichment=_enrichment(instance, bare) if full else None,
    )


def page(roster: Roster, name: str, instance: str) -> Page:
    """Read one wiki page by the name its wikilinks use.

    Args:
        roster: The served instances.
        name: The page name, with or without the ``.md`` suffix.
        instance: Which instance's wiki to read.

    Returns:
        The page, and the pages its body links to.

    Raises:
        NotFoundError: That instance's wiki holds no page of that name.
        ValueError: The file is there but cannot be read.
    """
    served = roster.locate(instance)
    wanted = name.removesuffix(".md")
    path = _pages(served).get(wanted)
    if path is None:
        raise NotFoundError(
            f"{instance} has no wiki page named {wanted!r} — page names are the "
            "taxonomy's kebab-case names, spelled as they are inside [[wikilinks]]"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"{instance} cannot read wiki page {wanted!r}: {e}") from e
    return Page(
        name=wanted,
        instance=instance,
        title=_page_title(text, wanted),
        path=str(path.relative_to(served.root)),
        text=text,
        wikilinks=_wikilinks(frontmatter.body(text)),
        next=steering.PAGE_NEXT,
    )


def capture(
    roster: Roster, *, instance: str, url: str, note: str, now: datetime.datetime
) -> Capture:
    """Write one capture into an instance's inbox and commit it there.

    Args:
        roster: The served instances.
        instance: Which instance to capture into.
        url: The URL being captured, or ``""`` for a note-only capture.
        note: Why it was worth saving, or ``""`` for a bare link.
        now: The capture moment, stamped into the filename.

    Returns:
        The capture, saying whether the commit followed the file.

    Raises:
        NotFoundError: ``instance`` names no served instance.
        ValueError: There is neither a URL nor a note, or the file could not
            be written at all — the only outcome where nothing was kept.
    """
    served = roster.locate(instance)
    try:
        path = write_capture(served, url=url, note=note, now=now)
    except OSError as e:
        raise ValueError(f"{instance} cannot write the capture: {e}") from e
    relative = path.relative_to(served.root).as_posix()
    failure = _commit(served.root, relative)
    return Capture(
        path=relative,
        instance=instance,
        committed=failure is None,
        detail="" if failure is None else f"written, but not committed: {failure}",
    )


# ---------------------------------------------------------------------------
# Committing
# ---------------------------------------------------------------------------


def _commit(root: Path, relative: str) -> str | None:
    """Commit exactly the capture file; ``None`` when it committed.

    Both calls name the path: the repo is the owner's own working tree, and
    whatever they had staged when a capture arrived is theirs to commit, not
    this one's to carry. No author is set either — the capture is the
    owner's, made under the git identity they already keep here.
    """
    staged = _git(root, ["add", "--", relative])
    if staged is not None:
        return staged
    return _git(root, ["commit", "-m", f"capture: {relative}", "--", relative])


def _git(root: Path, args: list[str]) -> str | None:
    """Run one git subcommand in ``root``; ``None`` when it succeeded."""
    try:
        done = subprocess.run(  # noqa: S603 — engine-built args, no shell
            ["git", "-C", str(root), *args],  # noqa: S607 — git resolves via PATH like every dev tool
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    if done.returncode == 0:
        return None
    # Decoded tolerantly, never text=True: the output may be a failing
    # hook's, and a hook prints whatever bytes it likes — to stdout as often
    # as to stderr.
    said = (done.stderr or done.stdout).decode("utf-8", "replace").strip()
    return said or f"git {args[0]} exited {done.returncode}"


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _item_hits(name: str, instance: Instance, pattern: re.Pattern[str]) -> Iterator[Hit]:
    """Every item whose digest or corpus file holds the pattern.

    One row per item, not one per file that matched: the digest and the
    corpus item are two halves of what ``fetch`` returns together, so a
    match in either is the same hit. The digest is tried first because a
    match in the curated facts is the more useful thing to show.
    """
    for path in sorted(instance.corpus_dir.glob("*/*.md")):
        text = _read(path)
        item = _parse(text)
        if item is None:
            continue
        # The filename, never the frontmatter's `id:`. The two agree on
        # everything the engine wrote (a disagreement is a lint failure the
        # digest verb refuses to write under), the filename is what keys the
        # digest, and it is the id a caller can hand back to `fetch` — while
        # frontmatter is authored text that must never become a path.
        item_id = path.stem
        snippet = _snippet(_read(instance.digests_dir / f"{item_id}.md"), pattern) or _snippet(
            text, pattern
        )
        if snippet is None:
            continue
        yield Hit(
            id=qualify(name, item_id),
            type=HitType.ITEM,
            title=_item_title(item_id),
            date=item.date.isoformat(),
            url=item.urls[0] if item.urls else None,
            snippet=snippet,
            instance=name,
        )


def _page_hits(name: str, instance: Instance, pattern: re.Pattern[str]) -> Iterator[Hit]:
    """Every wiki page whose body holds the pattern; a page carries no share date.

    The body, not the file: page frontmatter is `generated:` bookkeeping,
    and a match near the top of the body must not drag it into the snippet.
    Corpus files deliberately keep the whole-file scan — their frontmatter
    holds URLs, and finding an item by its source is a real search.
    """
    for page_name, path in _pages(instance).items():
        text = _read(path)
        snippet = _snippet(frontmatter.body(text), pattern)
        if snippet is None:
            continue
        yield Hit(
            id=qualify(name, page_name),
            type=HitType.PAGE,
            title=_page_title(text, page_name),
            date=None,
            url=None,
            snippet=snippet,
            instance=name,
        )


def _wikilinks(body: str) -> list[str]:
    """The pages a body links to, in the order it links them, each named once.

    The name only: a `[[name|shown as this]]` link resolves by its name, and
    the alias is text for a human reader. A link with no name is not one.
    """
    names = (match[1].partition("|")[0].strip() for match in _WIKILINK_RE.finditer(body))
    return list(dict.fromkeys(name for name in names if name))


def _pages(instance: Instance) -> dict[str, Path]:
    """Every wiki page by name — the name a wikilink uses and ``page`` resolves.

    Sorted so that a name held by two files (which lint reports as its own
    fault) resolves to the same one on every call.
    """
    pages: dict[str, Path] = {}
    for path in sorted(instance.wiki_dir.rglob("*.md")):
        pages.setdefault(path.stem, path)
    return pages


def _newest_first(hits: list[Hit]) -> list[Hit]:
    """Dated hits newest first, then the undated ones; ties by id.

    Two stable passes rather than one key: the order is descending on date
    and ascending on id, and `reverse=True` leaves ties in the order the
    first pass put them, which no single tuple key expresses.
    """
    hits.sort(key=lambda hit: hit.id)
    hits.sort(key=lambda hit: (hit.date is not None, hit.date or ""), reverse=True)
    return hits


def _snippet(text: str, pattern: re.Pattern[str]) -> str | None:
    """A one-line window around the first match, or ``None`` when there is none.

    Matched on the text itself rather than a case-folded copy: folding
    changes length for some characters, and the offsets would then point
    into a string that no longer exists.
    """
    match = pattern.search(text)
    if match is None:
        return None
    start = max(0, match.start() - _SNIPPET_LEAD)
    end = min(len(text), match.end() + _SNIPPET_TAIL)
    window = " ".join(text[start:end].split())
    return f"{'…' if start else ''}{window}{'…' if end < len(text) else ''}"


# ---------------------------------------------------------------------------
# Reading one file
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """One file's text, or "" when it is absent or unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _parse(text: str) -> corpus.CorpusItem | None:
    """One corpus item, or ``None`` when the text is not one."""
    try:
        return corpus.parse(text)
    except corpus.CorpusSchemaError:
        return None


def _enrichment(instance: Instance, item_id: str) -> list[Enrichment]:
    """The fetched sources behind one item, by filename.

    The directory listing rather than the item's ``enrichment:`` field: the
    field is derived and can lag what is on disk, and the same directory
    holds downloaded media, which is not text at all.
    """
    files: list[Enrichment] = []
    for path in sorted((instance.enrichment_dir / item_id).glob("*.md")):
        try:
            fields, body = enrichment.read_enrichment(path)
        except (OSError, UnicodeDecodeError):
            continue
        files.append(Enrichment(name=path.name, url=fields.get("url"), text=body))
    return files


def _item_title(item_id: str) -> str:
    """The item id read as a title: the slug between its date and its shortid.

    An id is the only name every item has — no file on disk holds a title
    for the ones the pipeline never fetched — and the slug in it is derived
    from the note or the source, so it reads as one.
    """
    match = _ITEM_ID_RE.fullmatch(item_id)
    return match["slug"].replace("-", " ") if match else item_id


def _page_title(text: str, name: str) -> str:
    """The page's own top-level heading, or its name when it has none."""
    match = _HEADING_RE.search(text)
    return match[1].strip() if match else name
