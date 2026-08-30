"""dex-serve: serve one or more instances to MCP clients over stdio.

Thin by design: parse the instance roots, build the roster, build the
server, speak stdio. Zero business logic lives here.
"""

import argparse
import sys
from pathlib import Path

from .roster import build_roster
from .server import build_server

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-serve --instance PATH [--instance PATH …]."""
    parser = argparse.ArgumentParser(
        prog="dex-serve",
        description="Serve dex instances to MCP clients over stdio: mechanical text "
        "search, item reads, wiki reads, and capture into an instance's inbox. "
        "The judgment stays with the caller.",
    )
    parser.add_argument(
        "--instance",
        dest="instances",
        action="append",
        required=True,
        metavar="PATH",
        type=Path,
        help="an instance root to serve; repeat for each one (its directory "
        "name is what results are tagged with, and it namespaces their ids)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse, build the roster, serve until the client disconnects."""
    args = build_parser().parse_args(argv)
    try:
        roster = build_roster(args.instances)
    except (OSError, ValueError) as e:
        sys.exit(f"dex-serve: {e}")
    build_server(roster).run(transport="stdio")


if __name__ == "__main__":
    main()
