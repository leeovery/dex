"""Fixtures for the serve tests: two toy instances and an in-process client.

The instances are shaped like the repo's `example/` — a corpus item, its
digest, its enrichment, a wiki page, the CLAUDE.md declaring its scope, the
taxonomy and its compiled map — times two, so fan-out, the instance filter
and the id namespace all have something to be wrong about. `dex-books` is
deliberately younger: no wiki, no taxonomy, no compiled map, no CLAUDE.md,
one item and its digest.

Every call goes through the SDK's in-memory transport: a real client session
against a real server, no subprocess and no socket. The one exception is
git: a capture commits, so the `repos` fixture makes the instances real
repositories and the assertions read real history.
"""

import json
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from dex_engine.instance_map import write_map
from dex_engine.pipeline.types import Instance
from dex_engine.serve.roster import Roster, build_roster
from dex_engine.serve.server import build_server

COFFEE = "dex-coffee"
BOOKS = "dex-books"
# The tree's own instance/ — the same files the wheel bundles at
# `dex_engine/instance`, which a source checkout has nowhere else.
TEMPLATE = Path(__file__).resolve().parents[2] / "instance"
SCOPE = """\
# dex-coffee — Coffee Knowledge Base (a dex instance)

## In scope

- brewing technique and recipes
- grinders and gear
"""
V60 = "2026-01-15-james-hoffmann-v60-technique-a1b2c3"
GRINDER = "2026-02-20-grinder-burr-alignment-b2c3d4"
BORGES = "2026-03-01-borges-labyrinths-c3d4e5"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _coffee(root: Path) -> None:
    write(root / "CLAUDE.md", SCOPE)
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
        json.dumps(
            {
                "topics": {
                    "pour-over": {"description": "Filter brewing.", "items": [V60]},
                    "brewing-technique": {
                        "description": "Method across brew styles.",
                        "items": [V60, GRINDER],
                    },
                },
                "entities": {
                    "james-hoffmann": {"kind": "person", "raw": ["James Hoffmann", "Hoffmann"]}
                },
            }
        ),
    )
    write(root / "state" / "entity-members.json", json.dumps({"james-hoffmann": [V60]}))
    # The compiled map, from the same files the compile always reads — the
    # serve tools read it back verbatim. The same call renders
    # wiki/index.md, exactly as it stands in a live instance.
    write_map(Instance(root=root))


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


def git(root: Path, *args: str) -> str:
    """One git command in a fixture repo, run by the test itself."""
    return subprocess.run(  # noqa: S603 — test-built args, no shell
        ["git", "-C", str(root), *args],  # noqa: S607 — PATH resolution is the dependency contract
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repos(roots: list[Path]) -> list[Path]:
    """The same roots, each a repository with the instance already committed."""
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    for root in roots:
        git(root, "init", "-q")
        git(root, "config", "user.name", "Test Owner")
        git(root, "config", "user.email", "owner@dex.test")
        # A capture commits as the owner, under whatever git config they
        # keep — so a maintainer running the suite with global commit
        # signing on would fail every one of these without this line.
        git(root, "config", "commit.gpgsign", "false")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "the instance so far")
    return roots


@pytest.fixture
def roster(roots: list[Path]) -> Roster:
    return build_roster(roots)


@pytest.fixture
def server(roster: Roster) -> MCPServer:
    return build_server(roster, template=TEMPLATE)


def drive(server: MCPServer, work: Callable[[Client], Awaitable[Any]]) -> Any:
    """Run one client conversation against the server, in this process."""

    async def conversation() -> Any:
        async with Client(server) as client:
            return await work(client)

    return anyio.run(conversation)


def call(server: MCPServer, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call one tool and return the raw CallToolResult."""
    return drive(server, lambda client: client.call_tool(tool, arguments or {}))


def record(server: MCPServer, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """The object a tool returned."""
    result = call(server, tool, arguments)
    assert not result.is_error, result.content[0].text
    return result.structured_content


def hits(server: MCPServer, arguments: dict[str, Any]) -> list[Any]:
    """The hit rows one search returned, out from under the next line they ride with."""
    return record(server, "search", arguments)["hits"]


def refusal(server: MCPServer, tool: str, arguments: dict[str, Any] | None = None) -> str:
    """The message a tool refused with."""
    result = call(server, tool, arguments)
    assert result.is_error, result.structured_content
    return result.content[0].text
