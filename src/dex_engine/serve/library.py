"""Working an instance from outside: search, fetch, page, the maps, capture.

Everything here is mechanical. A search is a plain case-insensitive scan of
what the instance already curated — digests first, then the corpus item
behind each one, then the wiki pages — and what comes back is raw hits in a
stated order, never a ranked answer. Judgment about which of them matter is
the caller's; the curation that makes dumb probes land somewhere structured
was spent at write time.

The three map reads — ``topics``, ``entities``, ``graph`` — read
``state/map.json``, the artifact the map compile already derived: counts
and typed edges reshaped onto the wire, member id lists left in the file,
nothing recomputed at call time. The graph read defaults to the
topic↔topic core under a cap — a real map holds more edges than a chat
model can read — and its parameters open the rest. A young instance has no
map yet, and the refusal says which command grows one.

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
import json
import re
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from dex_engine import corpus, frontmatter
from dex_engine.pipeline import enrichment
from dex_engine.pipeline.capture import write_capture
from dex_engine.pipeline.types import Instance
from dex_engine.pipeline.urls import resolve_repo_path
from dex_engine.wikitext import wikilinks as _wikilinks

from . import steering
from .roster import NotFoundError, Roster, qualify

__all__ = [
    "Capture",
    "Edge",
    "Enrichment",
    "Entities",
    "Entity",
    "Graph",
    "Hit",
    "HitType",
    "Item",
    "Page",
    "Search",
    "Topic",
    "Topics",
    "capture",
    "entities",
    "fetch",
    "graph",
    "page",
    "search",
    "topics",
]

_Rows = TypeVar("_Rows")

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

# How many edges the default graph view may put in front of the caller. A
# real instance's compiled map holds five figures of edges — a megabyte-plus
# on the wire — which is not a result a chat model can read, and most of it
# is weight-1 shared-items noise; even the topic↔topic core lands around
# four thousand there. The cut keeps every wikilink edge — each one a page
# curated by hand — and fills what room is left with the heaviest
# shared-items ones, and the caller is told the whole count either way,
# with `around`, `min_weight` and `full` opening the rest on request.
_EDGE_CAP = 2000

# `YYYY-MM-DD-<slug>-<shortid>`, the item-id shape the whole system files
# under. The slug is the only human-readable name every item carries.
_ITEM_ID_RE = re.compile(r"\d{4}-\d{2}-\d{2}-(?P<slug>.+)-[0-9a-f]{6}")
_HEADING_RE = re.compile(r"^# (.+)$", re.MULTILINE)


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
    signal: str | None
    digest: str | None
    enrichment: list[Enrichment] | None


@dataclass(frozen=True, kw_only=True)
class Page:
    """One wiki page's body, and the pages it points at."""

    name: str
    instance: str
    title: str
    path: str
    text: str
    wikilinks: list[str]
    next: str


@dataclass(frozen=True, kw_only=True)
class Topic:
    """One topic as the map states it: the judgment, its size, and where it maps."""

    name: str
    description: str
    count: int
    has_page: bool
    newest: str | None


@dataclass(frozen=True, kw_only=True)
class Topics:
    """Every topic one instance files under, and what to do with the list."""

    topics: list[Topic]
    instance: str
    next: str


@dataclass(frozen=True, kw_only=True)
class Entity:
    """One entity as the map states it: what kind of thing, its other names, its reach."""

    name: str
    kind: str
    aliases: list[str]
    count: int
    has_page: bool


@dataclass(frozen=True, kw_only=True)
class Entities:
    """Every entity one instance tracks, and what to do with the list."""

    entities: list[Entity]
    instance: str
    next: str


@dataclass(frozen=True, kw_only=True)
class Edge:
    """One typed relation: directed ``wikilink``, or weighted undirected ``shared-items``."""

    type: str
    source: str
    target: str
    weight: int | None


@dataclass(frozen=True, kw_only=True)
class Graph:
    """The edges one graph read serves, under the count of every edge in the map."""

    edges: list[Edge]
    total: int
    instance: str
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
    # The digest's frontmatter is its classification at digest time — stale
    # by design once the taxonomy moves, so it never reads as the item's
    # current topics. What surfaces is the judgment still current (`signal`)
    # and the fact body.
    digest = _read(instance.digests_dir / f"{bare}.md")
    # `bare`, never the frontmatter's `id:` — see _item_hits.
    return Item(
        id=qualify(name, bare),
        instance=name,
        title=_item_title(bare),
        date=item.date.isoformat(),
        shared_by=item.shared_by,
        urls=list(item.urls),
        note=item.body.strip(),
        signal=_signal(digest),
        digest=frontmatter.body(digest).strip() or None,
        enrichment=_enrichment(instance, bare) if full else None,
    )


def page(roster: Roster, name: str, instance: str) -> Page:
    """Read one wiki page by the name its wikilinks use.

    Args:
        roster: The served instances.
        name: The page name, with or without the ``.md`` suffix.
        instance: Which instance's wiki to read.

    Returns:
        The page's body — its frontmatter is ``generated:`` bookkeeping, not
        content — and the pages that body links to.

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
        content = frontmatter.body(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"{instance} cannot read wiki page {wanted!r}: {e}") from e
    return Page(
        name=wanted,
        instance=instance,
        title=_page_title(content, wanted),
        path=str(path.relative_to(served.root)),
        text=content,
        wikilinks=_wikilinks(content),
        next=steering.PAGE_NEXT,
    )


def topics(roster: Roster, instance: str) -> Topics:
    """Every topic one instance files under, as its compiled map states them.

    Args:
        roster: The served instances.
        instance: Which instance's map to read.

    Returns:
        The topics in the map's own order — counts on the wire, member id
        lists left in the artifact — and the one line saying what to do next.

    Raises:
        NotFoundError: ``instance`` names no served instance, or its map has
            not been compiled yet.
        ValueError: The map is there but will not parse.
    """
    payload = _compiled(instance, roster.locate(instance))
    return Topics(
        topics=_shaped(
            instance,
            lambda: [
                Topic(
                    name=name,
                    description=entry["description"],
                    count=entry["count"],
                    has_page=entry["has_page"],
                    newest=entry["newest"],
                )
                for name, entry in payload["topics"].items()
            ],
        ),
        instance=instance,
        next=steering.TOPICS_NEXT,
    )


def entities(roster: Roster, instance: str) -> Entities:
    """Every entity one instance tracks, as its compiled map states them.

    Args:
        roster: The served instances.
        instance: Which instance's map to read.

    Returns:
        The entities in the map's own order — counts on the wire, member id
        lists left in the artifact — and the one line saying what to do next.

    Raises:
        NotFoundError: ``instance`` names no served instance, or its map has
            not been compiled yet.
        ValueError: The map is there but will not parse.
    """
    payload = _compiled(instance, roster.locate(instance))
    return Entities(
        entities=_shaped(
            instance,
            lambda: [
                Entity(
                    name=name,
                    kind=entry["kind"],
                    aliases=entry["aliases"],
                    count=entry["count"],
                    has_page=entry["has_page"],
                )
                for name, entry in payload["entities"].items()
            ],
        ),
        instance=instance,
        next=steering.ENTITIES_NEXT,
    )


def graph(
    roster: Roster,
    instance: str,
    *,
    around: str | None = None,
    min_weight: int | None = None,
    full: bool,
) -> Graph:
    """How one instance's topics and entities relate, as its compiled map states it.

    Args:
        roster: The served instances.
        instance: Which instance's map to read.
        around: Serve every edge touching this name — all types, all
            endpoints, all weights, uncapped. The name must be one the
            map's topics or entities state.
        min_weight: Drop ``shared-items`` edges lighter than this;
            ``wikilink`` edges carry no weight and always pass.
        full: Serve every edge in the map, uncapped. Stated at every call
            rather than defaulted here — the tool signature is where the
            caller's default belongs.

    Returns:
        The served edges in the map's own order — ``wikilink`` directed,
        ``shared-items`` undirected and stored smaller name first — under
        the count of every edge the map holds. The bare call is the
        topic↔topic view, and past ``_EDGE_CAP`` it keeps every wikilink
        edge and the heaviest shared-items ones; the one line then says how
        to widen.

    Raises:
        NotFoundError: ``instance`` names no served instance, its map has
            not been compiled yet, or ``around`` names nothing it maps.
        ValueError: The map is there but will not parse, or ``around`` and
            ``full`` were asked for together.
    """
    if full and around is not None:
        raise ValueError(
            "around and full contradict — around is one name's neighborhood, "
            "full is every edge in the map; ask for one or the other"
        )
    payload = _compiled(instance, roster.locate(instance))
    edges = _shaped(
        instance,
        lambda: [
            Edge(
                type=entry["type"],
                source=entry["source"],
                target=entry["target"],
                weight=entry.get("weight"),
            )
            for entry in payload["graph"]
        ],
    )
    if around is not None and not _shaped(
        instance, lambda: around in payload["topics"] or around in payload["entities"]
    ):
        raise NotFoundError(
            f"{instance} maps no name {around!r} — `topics(instance)` and "
            "`entities(instance)` list the names the graph relates"
        )
    served = _shaped(
        instance,
        lambda: _view(payload, edges, around=around, min_weight=min_weight, full=full),
    )
    return Graph(
        edges=served,
        total=len(edges),
        instance=instance,
        next=steering.graph_next(shown=len(served), total=len(edges)),
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
# The compiled map
# ---------------------------------------------------------------------------


def _compiled(name: str, instance: Instance) -> dict[str, Any]:
    """One instance's ``state/map.json``, parsed and nothing more.

    Raises:
        NotFoundError: The map has not been compiled yet.
        ValueError: The file is there but will not read or parse.
    """
    path = instance.map_path
    if not path.is_file():
        raise NotFoundError(
            f"{name} has no compiled map — `bin/dex map` (or the instance's next "
            "sync) writes state/map.json"
        )
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(_recompile(name, e)) from e
    if not isinstance(parsed, dict):
        raise ValueError(_recompile(name, f"expected a JSON object, got {type(parsed).__name__}"))
    return parsed


def _shaped(name: str, reshape: Callable[[], _Rows]) -> _Rows:
    """Reshape the parsed map onto the wire, or refuse it as unparseable.

    The map is compiled, never hand-written, so a shape these reads cannot
    take rows from is a file that was not honestly compiled — and the heal
    is the same recompile as for one that is not JSON at all.
    """
    try:
        return reshape()
    except (AttributeError, KeyError, TypeError) as e:
        raise ValueError(_recompile(name, e)) from e


def _recompile(name: str, cause: object) -> str:
    """The one wording for a map that cannot be honestly served."""
    return f"{name}'s state/map.json does not parse: {cause} — `bin/dex map` recompiles it"


def _view(
    payload: dict[str, Any],
    edges: list[Edge],
    *,
    around: str | None,
    min_weight: int | None,
    full: bool,
) -> list[Edge]:
    """The edges one call's parameters select, in the order they came.

    The bare view is the topic↔topic core under ``_EDGE_CAP``; ``around``
    and ``full`` lift both the restriction and the cap, and ``min_weight``
    composes with either.
    """
    if around is not None:
        edges = [edge for edge in edges if around in (edge.source, edge.target)]
    if min_weight is not None:
        edges = [
            edge
            for edge in edges
            if edge.type != "shared-items" or (edge.weight or 0) >= min_weight
        ]
    if full or around is not None:
        return edges
    topics = payload["topics"]
    return _clipped([edge for edge in edges if edge.source in topics and edge.target in topics])


def _clipped(edges: list[Edge]) -> list[Edge]:
    """The default view cut to ``_EDGE_CAP``, in the order the edges came.

    Every wikilink edge survives, and the heaviest shared-items edges fill
    what room is left — a stable take, so equal weights keep the map's own
    deterministic order.
    """
    if len(edges) <= _EDGE_CAP:
        return edges
    shared = [i for i, edge in enumerate(edges) if edge.type == "shared-items"]
    room = _EDGE_CAP - (len(edges) - len(shared))
    kept = set(sorted(shared, key=lambda i: -(edges[i].weight or 0))[: max(room, 0)])
    return [edge for i, edge in enumerate(edges) if edge.type != "shared-items" or i in kept]


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _item_hits(name: str, instance: Instance, pattern: re.Pattern[str]) -> Iterator[Hit]:
    """Every item whose digest body or corpus file holds the pattern.

    One row per item, not one per file that matched: the digest and the
    corpus item are two halves of what ``fetch`` returns together, so a
    match in either is the same hit. The digest is tried first because a
    match in the curated facts is the more useful thing to show — and only
    its body: the frontmatter is the classification made at digest time,
    stale by design once the taxonomy moves, so a candidate topic name
    living only there must neither hit nor snippet.
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
        snippet = _snippet(
            frontmatter.body(_read(instance.digests_dir / f"{item_id}.md")), pattern
        ) or _snippet(text, pattern)
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


def _signal(digest: str) -> str | None:
    """The ``signal:`` judgment in a digest's frontmatter, or ``None`` without one.

    A one-key read, tolerant the way every digest reader is: a digest that
    predates the verb may carry a quoted value, and the quotes come off
    through the one shared rule.
    """
    if not digest.startswith("---\n"):
        return None
    end = digest.find("\n---\n", 3)
    if end == -1:
        return None
    for line in digest[len("---\n") : end].split("\n"):
        key, colon, value = line.partition(":")
        if colon and key.strip() == "signal":
            return frontmatter.unquote(value.strip()) or None
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
