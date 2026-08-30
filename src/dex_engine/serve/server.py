"""Wiring the reads onto MCP: three tools and each instance's map files.

The only thing this layer decides is how a refusal reaches the caller. Only
a :class:`ToolError`'s text is passed on; every other exception is logged
here and answered with a generic line, so the reads' anticipated refusals —
an unknown instance, an id that names nothing — are restated as that type,
and a genuine crash stays unexplained on purpose.
"""

from collections.abc import Callable
from typing import TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resources import FileResource

from dex_engine.version import engine_version

from . import library
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


def build_server(roster: Roster) -> MCPServer:
    """Build the MCP server that serves ``roster``.

    Args:
        roster: The instances to serve.

    Returns:
        The server, tools and resources attached, not yet listening.
    """
    server = MCPServer("dex", version=engine_version())
    _add_tools(server, roster)
    _add_resources(server, roster)
    return server


def _add_tools(server: MCPServer, roster: Roster) -> None:
    """Attach the three read tools, each closing over the roster."""

    @server.tool()
    def search(query: str, instance: str | None = None) -> list[library.Hit]:
        """Search dex for text and return the raw hits, never a ranked answer.

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

        Args:
            id: A namespaced item id, as a search states it.
            full: Also return the fetched source text behind the item.

        Returns:
            The item.
        """
        return _stated(lambda: library.fetch(roster, id, full=full))

    @server.tool()
    def page(name: str, instance: str) -> library.Page:
        """Read one wiki page's markdown by name.

        Args:
            name: The page name as it is spelled inside a ``[[wikilink]]``,
                with or without the ``.md`` suffix.
            instance: Which instance's wiki holds it.

        Returns:
            The page, verbatim.
        """
        return _stated(lambda: library.page(roster, name, instance))


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
