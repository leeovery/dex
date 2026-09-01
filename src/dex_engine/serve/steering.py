"""What the server says about itself: the doctrine, the roster, the next move.

A chat model is a less patient caller than a session holding Grep and Read —
two or three calls where a session makes ten — and the counter is prose, not
machinery. Three of MCP's four steering slots are that prose, and they are
owned here: the instructions handed over at connect time, and the one-line
next move a search or a page result travels back with. Each slot says its own
thing exactly once. The instructions carry the doctrine; a footer carries only
what to do next. A footer that restated the doctrine on every result would tax
the context until the model skimmed both.

The fourth slot, the dex-query procedure, is not written here at all: it is
read out of the bundled skill template. The skill is that procedure's one
home, and a second statement of it in this tree would drift from the first
with nothing to notice.
"""

from importlib.resources.abc import Traversable

from dex_engine import frontmatter
from dex_engine.pipeline.types import Instance

from .roster import Roster

__all__ = [
    "ENTITIES_NEXT",
    "PAGE_NEXT",
    "TOPICS_NEXT",
    "graph_next",
    "instructions",
    "procedure",
    "search_next",
]

_DOCTRINE = """\
dex is this owner's own knowledge base: what they saved, plus the digests and \
wiki pages written from it. These tools are hands, not an answer service — the \
searching is yours to do, the way you would do it over a folder of files.

- "What does this instance hold?" opens with the maps: `topics(instance)`, \
`entities(instance)` and `graph(instance)` state the catalog in one call \
each, and a name with a page opens via `page(name, instance)`.
- `search` returns raw hits in the instance's own order, never a ranked \
answer. Read a hit as a probe, not as the answer.
- Open what looks promising: `fetch(id)` for an item, `page(name, instance)` \
for a wiki page, then follow that page's `[[wikilinks]]` with further `page` \
calls.
- Reformulate and search again — the owner's likely wording, narrower terms, \
adjacent ideas — until nothing relevant is left unexplored. Two or three \
calls is rarely the end of it.
- Answer only from material you actually fetched, and cite the item ids it \
came from."""

_SCOPES_LEAD = "What is in each instance, in its own words:"

_NUDGE = """\
If a question falls inside one of those scopes, search dex before answering \
from general knowledge: what the owner saved and concluded outranks what you \
already know. A capture — "save this to my dex" — routes by the same scopes, \
so name the instance whose scope it belongs to."""

# A tag rather than a markdown heading: what it wraps is the owner's own
# CLAUDE.md, headings and fences included, and the delimiter has to be one the
# enclosed markdown cannot accidentally close.
_SCOPE_OPEN = '<instance name="{name}">'
_SCOPE_CLOSE = "</instance>"
_UNDECLARED = (
    "This instance ships no CLAUDE.md, so its scope is undeclared — "
    "search it when a question might belong to it."
)

_SEARCH_NEXT = (
    "Raw hits, not an answer — fetch the promising ids, open the wiki pages and "
    "follow their wikilinks, then search again with different terms until "
    "nothing relevant is left unexplored."
)
_SEARCH_EMPTY = (
    "Nothing matched, and the match is literal text — try the owner's likely "
    "wording, a broader or narrower term, or another instance before "
    "concluding dex is silent on this."
)
_SEARCH_CAPPED = (
    "Showing the newest {shown} of {total} matches — a term this broad tells a "
    "literal scan nothing, so add words or narrow it and search again rather "
    "than reading down this list."
)

PAGE_NEXT = (
    "A page is a map, not a source: resolve its `wikilinks` with "
    "`page(name, instance)` and open the item ids it cites with `fetch` — the "
    "items are what an answer cites."
)

TOPICS_NEXT = (
    "A catalog, not content: open a topic that has a page with "
    "`page(name, instance)` — the page cites its member items — and `search` "
    "the names that have none."
)

ENTITIES_NEXT = (
    "A catalog, not content: an entity's aliases are the owner's own "
    "spellings — ready-made `search` terms — and one with a page opens with "
    "`page(name, instance)`."
)

_GRAPH_NEXT = (
    "Edges are leads, not answers: follow a `wikilink` edge with "
    "`page(name, instance)`, and read a `shared-items` weight as how many "
    "items two names file together."
)

_GRAPH_TRIMMED = (
    "Showing {shown} of the map's {total} edges — `around=<name>` pulls every "
    "edge touching one name, `min_weight=N` drops `shared-items` ties below N, "
    "and `full=true` is the whole graph."
)


def instructions(roster: Roster) -> str:
    """The connect-time instructions for a server holding ``roster``.

    Args:
        roster: The served instances, whose scopes the model routes by.

    Returns:
        The doctrine, then every instance's declared scope, then the nudge.
    """
    return "\n\n".join((_DOCTRINE, _SCOPES_LEAD, _scopes(roster), _NUDGE))


def search_next(*, shown: int, total: int) -> str:
    """The move that follows a search: work the hits, narrow the query, or reformulate.

    Args:
        shown: How many hits the caller is holding.
        total: How many matched before the per-instance cap.

    Returns:
        The one line for the case this search landed in.
    """
    if not total:
        return _SEARCH_EMPTY
    if shown < total:
        return _SEARCH_CAPPED.format(shown=shown, total=total)
    return _SEARCH_NEXT


def graph_next(*, shown: int, total: int) -> str:
    """The move that follows a graph read: work the edges shown, or widen the view.

    Args:
        shown: How many edges the caller is holding.
        total: How many the whole map holds.

    Returns:
        The one line for the case this read landed in.
    """
    if shown < total:
        return _GRAPH_TRIMMED.format(shown=shown, total=total)
    return _GRAPH_NEXT


def procedure(template: Traversable) -> str:
    """The dex-query procedure, read from the bundled skill.

    Read on demand rather than composed at build time: the skill file is the
    procedure's one home, so serving its body verbatim is what keeps a change
    to the skill reaching this surface with no code change at all.

    Args:
        template: The instance template tree holding the skill.

    Returns:
        The skill's body, below its frontmatter.
    """
    skill = template / "skills" / "dex-query" / "SKILL.md"
    return frontmatter.body(skill.read_text(encoding="utf-8"))


def _scopes(roster: Roster) -> str:
    """Every served instance's scope, in roster order."""
    return "\n".join(_scope(name, instance) for name, instance in roster.instances.items())


def _scope(name: str, instance: Instance) -> str:
    """One instance's CLAUDE.md verbatim, delimited and named."""
    return "\n".join(
        (_SCOPE_OPEN.format(name=name), _declared(instance) or _UNDECLARED, _SCOPE_CLOSE)
    )


def _declared(instance: Instance) -> str:
    """What an instance says it is, or "" when it says nothing.

    An instance that will not read is an instance with no declared scope: a
    server that refused to start over one unreadable CLAUDE.md would take
    every other instance down with it.
    """
    try:
        return (instance.root / "CLAUDE.md").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
