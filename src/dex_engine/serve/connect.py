"""dex-connect: put the launcher for this machine's instances in a chat client's config.

``bin/dex connect --instance <path> [--instance <path> …]`` — one entry,
``mcpServers.dex``, merged into the Claude desktop app's config file. The app
spawns the server at launch, and the owner can ask their dex from any chat.

The launcher is deliberately version-free. A tag in the config would be a
second pin: sync bumps every instance's ``.dex-engine-pin`` while the config
stays frozen at the day it was written, and nothing would say so. The command
is an anchor instance's own ``bin/dex`` instead, so that instance's pin stays
the one version authority and the next app launch runs whatever sync last
pinned.

Two device-verified facts about the desktop app decide the rest. The config
file already exists on a fresh install — an empty ``mcpServers`` beside
unrelated app preferences — so writing is a merge into a live file and never a
create; clobbering the owner's other servers is the failure this command
exists to prevent, which is why the whole file is round-tripped and only one
key is touched. And a GUI-spawned child inherits launchd's bare PATH, which
holds no Homebrew, so the entry carries the installing shell's PATH or the
``uvx`` inside the shim never resolves.
"""

import argparse
import itertools
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dex_engine.atomic import write_text

from .roster import build_roster

__all__ = [
    "SERVER",
    "build_parser",
    "connect",
    "connect_gap",
    "default_config_path",
    "main",
    "server_entry",
]

# The name the entry is filed under, and the name the owner sees in the app.
SERVER = "dex"

# Verified on a device; the only location that is. Every other platform is
# asked outright rather than guessed at, because a guess writes a file the app
# never reads and nothing says so.
_MACOS_CONFIG = "Library/Application Support/Claude/claude_desktop_config.json"


def default_config_path() -> Path:
    """The Claude desktop app's config file on macOS.

    Raises:
        ValueError: This is not macOS, where the location is unverified.
    """
    if sys.platform != "darwin":
        raise ValueError(
            "the Claude desktop config location is only known for macOS (this is "
            f"{sys.platform}) — pass --config with the path to this machine's "
            "claude_desktop_config.json"
        )
    return Path.home() / _MACOS_CONFIG


def server_entry(*, anchor: Path, roots: Sequence[Path], path: str) -> dict[str, object]:
    """The ``mcpServers.dex`` value: what the app runs, over what, with which PATH.

    Args:
        anchor: The instance whose shim launches the server — its pin is the
            version authority.
        roots: The instance roots to serve, absolute and resolved, in order.
        path: The PATH the client must spawn the command with.

    Returns:
        The entry, ready to be filed under :data:`SERVER`.
    """
    args = ["serve"]
    for root in roots:
        args += ["--instance", str(root)]
    return {"command": str(_shim(anchor)), "args": args, "env": {"PATH": path}}


def connect(config: Path, *, anchor: Path, roots: Sequence[Path], path: str) -> str:
    """Merge the dex server entry into the client config at ``config``.

    Args:
        config: The client's config file, which must already exist.
        anchor: The instance whose ``bin/dex`` the client will run.
        roots: The instance roots to serve, absolute and resolved, in order.
        path: The PATH to write into the entry's environment.

    Returns:
        The one-line summary.

    Raises:
        ValueError: The file is absent, is not JSON, is not a JSON object, or
            holds an ``mcpServers`` that is not one. Nothing is written in any
            of those cases.
        OSError: The file could not be read or written.
    """
    data = _config(config)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(
            f"{config}: 'mcpServers' is not a JSON object, so there is nothing to merge "
            "into — nothing was written"
        )
    verb = "replaced" if SERVER in servers else "added"
    servers[SERVER] = server_entry(anchor=anchor, roots=roots, path=path)
    # The app's own formatting, so a re-serialized file reads as it did: two
    # spaces, and non-ASCII left as the characters it wrote rather than
    # escapes. Every key but ours makes this round trip, so it has to be the
    # owner's file that comes back, not a normalized one.
    write_text(config, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    served = ", ".join(root.name for root in roots)
    return (
        f"{verb} the '{SERVER}' server in {config}: {len(roots)} instance(s) ({served}), "
        f"launched through {_shim(anchor)}. Quit and relaunch the Claude desktop app — "
        "it spawns the server at launch."
    )


def _shim(root: Path) -> Path:
    """The instance shim: the command a client runs, and the version authority."""
    return root / "bin" / "dex"


def _config(config: Path) -> dict[str, object]:
    """The client's config, parsed, or a refusal that leaves the file alone.

    Every failure here is reported rather than repaired: the file belongs to
    another application, and writing over one this command could not read
    would take the owner's other servers with it.

    Raises:
        ValueError: Absent, unparseable, or not a JSON object.
    """
    try:
        raw = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(
            f"{config}: no such file — the Claude desktop app writes it itself, so "
            "install the app and launch it once before connecting"
        ) from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{config}: is not valid JSON ({e}) — nothing was written") from None
    if not isinstance(data, dict):
        raise ValueError(
            f"{config}: expected a JSON object, got {type(data).__name__} — nothing was written"
        )
    return data


def connect_gap(root: Path, *, config: Path | None = None) -> str | None:
    """What stands between the instance at ``root`` and the desktop app's chat.

    A read, never a write, and silent wherever it cannot be certain: this
    answers a report rather than a command, and it runs wherever sync runs —
    Linux boxes, cloud runners, machines with no desktop app on them. An
    unknown config location, an absent file, one that is not JSON or not an
    object, an ``mcpServers`` that is not one: each is no answer at all. A
    config this cannot read is a config ``connect`` would refuse, and saying
    so is ``connect``'s job in front of someone who asked for it, not a
    sync report's.

    Args:
        root: The instance root to look for.
        config: The client config to read; ``None`` resolves the platform's
            known location.

    Returns:
        ``"unconnected"`` when the config holds no dex server at all,
        ``"unlisted"`` when its dex server does not serve ``root``, and
        ``None`` when there is no gap — or nothing that can be read.
    """
    try:
        # Both the location and the parse refuse with ValueError, and an
        # unreadable file with OSError. Every one of them means the same
        # thing here: nothing to say.
        data = _config(config if config is not None else default_config_path())
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return None
    if SERVER not in servers:
        return "unconnected"
    return None if root.resolve() in _served_roots(servers[SERVER]) else "unlisted"


def _served_roots(entry: object) -> set[Path]:
    """The roots an entry serves, read back as :func:`server_entry` writes them.

    The value is whatever stands in a file another application owns, so its
    shape is checked rather than trusted — and an entry that cannot be read
    serves no root this can name, which is the honest answer either way.
    """
    if not isinstance(entry, dict):
        return set()
    args = entry.get("args")
    if not isinstance(args, list):
        return set()
    return {
        Path(value).resolve()
        for flag, value in itertools.pairwise(args)
        if flag == "--instance" and isinstance(value, str)
    }


def _instance_roots(paths: Sequence[Path]) -> list[Path]:
    """The roots to serve, validated exactly as the server validates them.

    The same :func:`~dex_engine.serve.roster.build_roster` the server runs at
    startup, so a roster the app could not serve is refused here — in front of
    someone reading the output — rather than at a launch nobody watches.

    Raises:
        ValueError: A path is not an instance root, or two share a basename.
    """
    return [instance.root for instance in build_roster(paths).instances.values()]


def _anchor_root(path: Path) -> Path:
    """The anchor instance, resolved, with the shim the client will execute.

    An instance like any other, plus the one file the app actually runs: a
    config naming a command that is not there fails inside a GUI process that
    reports nothing back.

    Raises:
        ValueError: Not an instance root, or it carries no shim.
    """
    (root,) = _instance_roots([path])
    if not _shim(root).is_file():
        raise ValueError(
            f"{path}: no bin/dex to launch — the anchor is the instance whose shim runs "
            "the server, and `bin/dex sync` is what writes one"
        )
    return root


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-connect --instance PATH [--anchor PATH] [--config PATH]."""
    parser = argparse.ArgumentParser(
        prog="dex-connect",
        description="Merge a 'dex' MCP server entry into the Claude desktop app's "
        "config so its chats can search, read and capture into these instances.",
    )
    parser.add_argument(
        "--instance",
        dest="instances",
        action="append",
        required=True,
        metavar="PATH",
        type=Path,
        help="an instance root to serve; repeat for each one (re-running with a "
        "different list is how the served set changes)",
    )
    parser.add_argument(
        "--anchor",
        metavar="PATH",
        type=Path,
        help="the instance whose bin/dex launches the server, and whose engine pin "
        "therefore governs it (default: the first --instance)",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        type=Path,
        help="the client config file to merge into (default: the Claude desktop app's, on macOS)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse, validate the roster and the anchor, merge the entry, print the summary."""
    args = build_parser().parse_args(argv)
    try:
        summary = connect(
            args.config or default_config_path(),
            anchor=_anchor_root(args.anchor or args.instances[0]),
            roots=_instance_roots(args.instances),
            # Captured from the installing shell: a GUI-spawned server gets
            # launchd's bare PATH, where uvx does not exist.
            path=os.environ.get("PATH", os.defpath),
        )
    except (OSError, ValueError) as e:
        sys.exit(f"dex-connect: {e}")
    sys.stdout.write(summary + "\n")


if __name__ == "__main__":
    main()
