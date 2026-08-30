"""The MCP server: a thin read-and-capture surface over one or more instances.

Hands, not an agent. The tools are mechanical primitives — scan for text,
read an item, read a wiki page, drop a capture in the inbox — and the
calling model does the searching with its own inference, exactly as a
session does with Grep and Read. No model runs on this side of the call, no
credential is stored, and every call is a fresh read of disk, so concurrent
conversations never interact and a pull the scheduled task just made is
visible immediately.

``roster`` decides which instances are served and how ids name them,
``library`` does the reading and the one write, ``steering`` holds the words
that keep an impatient caller probing, ``server`` wires them onto MCP, and
``cli`` is the entry point.
"""

from .roster import Roster, build_roster
from .server import build_server

__all__ = ["Roster", "build_roster", "build_server"]
