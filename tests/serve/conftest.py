"""Fixtures for the serve tests: two toy instances and an in-process client.

The instances are shaped like the repo's `example/` — a corpus item, its
digest, its enrichment, a wiki page — times two, so fan-out, the instance
filter and the id namespace all have something to be wrong about. `dex-books`
is deliberately younger: no wiki, no taxonomy, one item with no digest.

Every call goes through the SDK's in-memory transport: a real client session
against a real server, no subprocess and no socket.
"""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from dex_engine.serve.roster import Roster, build_roster
from dex_engine.serve.server import build_server

COFFEE = "dex-coffee"
BOOKS = "dex-books"
V60 = "2026-01-15-james-hoffmann-v60-technique-a1b2c3"
GRINDER = "2026-02-20-grinder-burr-alignment-b2c3d4"
BORGES = "2026-03-01-borges-labyrinths-c3d4e5"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _coffee(root: Path) -> None:
    write(
        root / "corpus" / "2026" / f"{V60}.md",
        f"""---
id: {V60}
source: manual
channel: inbox
shared_by: Lee
date: 2026-01-15
urls:
  - https://www.youtube.com/watch?v=example
kinds: [youtube]
status: enriched
enrichment: [youtube-f80fab.md]
---

**Lee** (2026-01-15):
The V60 technique video everyone cites.
""",
    )
    write(
        root / "state" / "digests" / f"{V60}.md",
        f"""---
id: {V60}
date: 2026-01-15
signal: high
topics: [pour-over, brewing-technique]
entities: [james-hoffmann, v60]
---
- The Hoffmann V60 method (2026-01): 60g/L dose, ~3:00 total brew, swirl instead of stir.
""",
    )
    write(
        root / "enrichment" / V60 / "youtube-f80fab.md",
        """---
url: https://www.youtube.com/watch?v=example
fetched: 2026-01-15
title: The Ultimate V60 Technique
---

## Transcript

Swirl the brewer gently after the bloom instead of stirring it.
""",
    )
    write(
        root / "corpus" / "2026" / f"{GRINDER}.md",
        f"""---
id: {GRINDER}
source: inbox
channel: inbox
shared_by: Lee
date: 2026-02-20
kinds: [text]
status: raw
enrichment: []
---

Burr alignment matters more than the grinder's price.
""",
    )
    write(root / "wiki" / "index.md", "# Index\n\n- [[pour-over]]\n")
    write(
        root / "wiki" / "topics" / "pour-over.md",
        f"""---
topic: pour-over
generated: 2026-01-16
items: 1
---
# Pour-over

The starting recipe is 60g/L with a post-bloom swirl rather than a stir
`{V60}`. See [[brewing-technique]].
""",
    )
    write(
        root / "state" / "taxonomy.json",
        json.dumps({"topics": {"pour-over": {"description": "Filter brewing.", "items": [V60]}}}),
    )


def _books(root: Path) -> None:
    write(
        root / "corpus" / "2026" / f"{BORGES}.md",
        f"""---
id: {BORGES}
source: inbox
channel: inbox
shared_by: Nat
date: 2026-03-01
kinds: [text]
status: raw
enrichment: []
---

Notes on Borges.
""",
    )
    write(
        root / "state" / "digests" / f"{BORGES}.md",
        f"""---
id: {BORGES}
date: 2026-03-01
signal: medium
topics: [short-fiction]
---
- The labyrinth is Borges' method for making a metaphysical claim legible.
""",
    )


@pytest.fixture
def roots(tmp_path: Path) -> list[Path]:
    """The two instance roots, coffee first."""
    _coffee(tmp_path / COFFEE)
    _books(tmp_path / BOOKS)
    return [tmp_path / COFFEE, tmp_path / BOOKS]


@pytest.fixture
def roster(roots: list[Path]) -> Roster:
    return build_roster(roots)


@pytest.fixture
def server(roster: Roster) -> MCPServer:
    return build_server(roster)


def drive(server: MCPServer, work: Callable[[Client], Awaitable[Any]]) -> Any:
    """Run one client conversation against the server, in this process."""

    async def conversation() -> Any:
        async with Client(server) as client:
            return await work(client)

    return anyio.run(conversation)


def call(server: MCPServer, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call one tool and return the raw CallToolResult."""
    return drive(server, lambda client: client.call_tool(tool, arguments or {}))


def rows(server: MCPServer, tool: str, arguments: dict[str, Any] | None = None) -> list[Any]:
    """The list a tool returned — the SDK wraps a list result under `result`."""
    result = call(server, tool, arguments)
    assert not result.is_error, result.content[0].text
    return result.structured_content["result"]


def record(server: MCPServer, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """The object a tool returned."""
    result = call(server, tool, arguments)
    assert not result.is_error, result.content[0].text
    return result.structured_content


def refusal(server: MCPServer, tool: str, arguments: dict[str, Any] | None = None) -> str:
    """The message a tool refused with."""
    result = call(server, tool, arguments)
    assert result.is_error, result.structured_content
    return result.content[0].text
