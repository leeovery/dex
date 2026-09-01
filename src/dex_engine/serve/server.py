"""Wiring the library onto MCP: seven tools, the map resources, the steering, the prompt.

The only thing this layer decides is how a refusal reaches the caller. Only
a :class:`ToolError`'s text is passed on; every other exception is logged
here and answered with a generic line, so the library's anticipated
refusals — an unknown instance, an id that names nothing, a capture with
nothing in it — are restated as that type, and a genuine crash stays
unexplained on purpose.

The tool docstrings are the descriptions the client sees on every call, so
each carries its one steering sentence and stops. The longer prose — the
connect-time instructions, the next-move footers — is ``steering``'s.
"""

import datetime
from collections.abc import Callable
from importlib.resources.abc import Traversable
from typing import TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resources import FileResource

from dex_engine.template import bundled_template
from dex_engine.version import engine_version

from . import library, steering
from .roster import Roster

__all__ = ["build_server"]

_Read = TypeVar("_Read")

# The two files that hold an instance's map. Attached per instance so a
# model starts a conversation holding the shape of the corpus rather than
# probing for it.
_MAPS = (
    ("wiki/index.md", "text/markdown", "the wiki's index of pages"),
    ("state/taxonomy.json", "application/json", "the topic and entity namespace"),
)


def build_server(roster: Roster, *, template: Traversable | None = None) -> MCPServer:
    """Build the MCP server that serves ``roster``.

    Args:
        roster: The instances to serve.
        template: Template override for tests; ``None`` uses the wheel's
            bundled ``instance/`` tree, which is where the dex-query
            procedure the prompt serves lives.

    Returns:
        The server, tools, resources and prompt attached, not yet listening.
    """
    server = MCPServer("dex", version=engine_version(), instructions=steering.instructions(roster))
    _add_tools(server, roster)
    _add_resources(server, roster)
    _add_prompt(server, template if template is not None else bundled_template())
    return server


def _add_tools(server: MCPServer, roster: Roster) -> None:
    """Attach the six reads and the one write, each closing over the roster."""

    @server.tool()
    def search(query: str, instance: str | None = None) -> library.Search:
        """Search dex for text: candidate probes, rarely the answer.

        Expect to reformulate and call again.

        Args:
            query: Plain text, matched case-insensitively and literally
                across digests, corpus items and wiki pages.
            instance: Restrict to one instance; omit to search all of them.

        Returns:
            One row per hit, tagged with the instance it came from. Rows of
            type ``item`` open with ``fetch(id)``; rows of type ``page``
            open with ``page(name, instance)``.
        """
        return _stated(lambda: library.search(roster, query, instance=instance))

    @server.tool()
    def fetch(id: str, full: bool = False) -> library.Item:  # noqa: A002, FBT001, FBT002 — the wire contract's own names
        """Read one item: its digest, the owner's verbatim note, and its provenance.

        Where a probe ends: this is material to answer from, and its id is
        what the answer cites.

        Args:
            id: A namespaced item id, as a search states it.
            full: Also return the fetched source text behind the item.

        Returns:
            The item.
        """
        return _stated(lambda: library.fetch(roster, id, full=full))

    @server.tool()
    def page(name: str, instance: str) -> library.Page:
        """Read one wiki page's markdown by name: the owner's map of a subject.

        It cites the items it was written from — keep following it outward.

        Args:
            name: The page name as it is spelled inside a ``[[wikilink]]``,
                with or without the ``.md`` suffix.
            instance: Which instance's wiki holds it.

        Returns:
            The page's body, and the wikilinks in it, each resolvable by
            another ``page`` call.
        """
        return _stated(lambda: library.page(roster, name, instance))

    @server.tool()
    def topics(instance: str) -> library.Topics:
        """List every topic one instance files under, from its compiled map.

        The opening move for "what does this instance hold" — a topic with
        a page opens with `page(name, instance)`.

        Args:
            instance: Which instance's map to read.

        Returns:
            Every topic: its description, how many items it files, whether
            a wiki page maps it, and its newest member's share date.
        """
        return _stated(lambda: library.topics(roster, instance))

    @server.tool()
    def entities(instance: str) -> library.Entities:
        """List every entity one instance tracks, from its compiled map.

        Aliases are the owner's own spellings — ready-made `search` terms.

        Args:
            instance: Which instance's map to read.

        Returns:
            Every entity: its kind, its aliases, how many items it reaches,
            and whether a wiki page maps it.
        """
        return _stated(lambda: library.entities(roster, instance))

    @server.tool()
    def graph(instance: str) -> library.Graph:
        """Read how one instance's topics and entities relate, from its compiled map.

        `wikilink` edges are curated pointers between pages; a `shared-items`
        weight counts the items two names file together.

        Args:
            instance: Which instance's map to read.

        Returns:
            The typed edges: `wikilink` directed, `shared-items` undirected
            and weighted.
        """
        return _stated(lambda: library.graph(roster, instance))

    @server.tool()
    def capture(instance: str, url: str = "", note: str = "") -> library.Capture:
        """Save a link and/or a note into one instance's inbox, and commit it.

        A suggestion, not a corpus entry: the instance judges it against its
        own scope when it next processes the inbox.

        Args:
            instance: Which instance to capture into — nothing is guessed
                here, so name the one whose scope this belongs to.
            url: The link being saved, if there is one.
            note: Why it is worth saving, in the owner's own words. Often
                the more valuable half; pass it verbatim.

        Returns:
            The capture file's path, and whether the commit followed it.
        """
        return _stated(
            lambda: library.capture(roster, instance=instance, url=url, note=note, now=_now())
        )


def _now() -> datetime.datetime:
    """The local wall clock the capture stamp is written from.

    Local, where the rest of the engine stamps UTC: this one becomes a
    filename that every other capture client writes from its own wall clock,
    and that the processor reads back as the day the owner captured.
    """
    return datetime.datetime.now(tz=datetime.UTC).astimezone()


def _add_prompt(server: MCPServer, template: Traversable) -> None:
    """Offer the dex-query procedure itself, for a caller who wants the deep pull.

    Read from the bundled skill at call time, never composed here: the skill
    is the procedure's one home, and serving its text is what keeps an edit
    to it arriving on this surface with no code change at all.
    """

    @server.prompt(name="dex-query", title="Query dex")
    def dex_query() -> str:
        """The full procedure a dex session follows to answer from its instance."""
        return steering.procedure(template)


def _add_resources(server: MCPServer, roster: Roster) -> None:
    """Attach each instance's map files; an absent one is simply not offered.

    A young instance has neither, and a resource whose file is not there
    would answer every read with an error instead of being missing.
    """
    for name, instance in roster.instances.items():
        for relative, mime_type, purpose in _MAPS:
            path = instance.root / relative
            if not path.is_file():
                continue
            server.add_resource(
                FileResource(
                    uri=f"dex://{name}/{relative}",
                    name=f"{name}/{relative}",
                    description=f"{purpose}, for the {name} instance",
                    mime_type=mime_type,
                    path=path,
                )
            )


def _stated(read: Callable[[], _Read]) -> _Read:
    """Run one read, restating its refusal as the error the caller can read."""
    try:
        return read()
    except ValueError as e:
        raise ToolError(str(e)) from e
