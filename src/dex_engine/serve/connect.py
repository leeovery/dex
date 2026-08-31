"""dex-connect: put the launcher for this machine's instances in every chat client it has.

``bin/dex connect --instance <path> [--instance <path> …]`` — one ``dex``
server entry per client, so the owner can ask their dex from the desktop app's
chat and from any Claude Code session on the machine. A client that is not
installed is skipped, never asked about and never reported as a gap.

The launcher is deliberately version-free, and identical for every client. A
tag in a config would be a second pin: sync bumps every instance's
``.dex-engine-pin`` while the config stays frozen at the day it was written,
and nothing would say so. The command is an anchor instance's own ``bin/dex``
instead, so that instance's pin stays the one version authority and the next
launch runs whatever sync last pinned.

The two clients are written by different means, and the difference is the
point.

The **desktop app** owns a small JSON file it writes itself at first launch,
so writing is a merge into a live file and never a create; clobbering the
owner's other servers is the failure this command exists to prevent, which is
why the whole file is round-tripped and only one key is touched. Its entry
carries an explicit PATH because a GUI-spawned child inherits launchd's bare
PATH, which holds no Homebrew, and ``uvx`` inside the shim would never
resolve.

**Claude Code** is written by shelling out to ``claude mcp``, never by merging
its config in process. That file carries live session and project state and is
rewritten by every running session, so a read-modify-write from here can drop
a concurrent write — the one place where this codebase's "one serialization
boundary" loses to "another program owns this file and is writing to it right
now". Reading it is still safe, and the gap check does exactly that. Its entry
carries no PATH: Claude Code inherits a real user PATH, and freezing an
install-day snapshot into the entry would be a downgrade, not insurance.

One verified trap decides the write order: ``claude mcp add`` REFUSES a name
that already exists — it never replaces. Since re-running this command with a
different instance list is how the served set changes, an ``add`` alone would
fail every time but the first. Every write removes first, and the remove's own
exit code is ignored: removing a name that is not there is an error to that
CLI and the intended state to this one.
"""

import argparse
import itertools
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from dex_engine.atomic import write_text

from .roster import build_roster

__all__ = [
    "CLIENTS",
    "SERVER",
    "absence",
    "build_parser",
    "code_config_path",
    "connect",
    "connect_code",
    "connect_gaps",
    "default_config_path",
    "detect",
    "main",
    "server_entry",
]

# The name the entry is filed under, and the name the owner sees in the client.
SERVER = "dex"

# The clients this knows how to write, in the order a summary lists them.
CLIENTS = ("desktop", "code")

# Verified on a device; the only location that is. Every other platform is
# asked outright rather than guessed at, because a guess writes a file the app
# never reads and nothing says so.
_MACOS_CONFIG = "Library/Application Support/Claude/claude_desktop_config.json"

# Claude Code's user-scope config. Written by `claude mcp add -s user` and only
# ever read here; CLAUDE_CONFIG_DIR relocates it, which is also the test seam.
_CODE_CONFIG = ".claude.json"


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


def code_config_path() -> Path:
    """Claude Code's user-scope config, wherever the CLI would write it."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home()) / _CODE_CONFIG


def absence(client: str, *, config: Path | None = None) -> str | None:
    """Why this client cannot be written on this machine, or ``None`` if it can.

    The reason is the owner's next move, so the three ways a client can be
    out of reach stay three answers: not installed, never launched, and — off
    macOS, where no config location is verified — not locatable without being
    told. Collapsing the last one into "not installed" would lose the only
    flag that fixes it.

    Args:
        client: One of :data:`CLIENTS`.
        config: The desktop config path to test; ``None`` resolves it.
    """
    if client == "code":
        if shutil.which("claude") is not None:
            return None
        return "no claude CLI on PATH — Claude Code is not installed here"
    if config is None:
        try:
            config = default_config_path()
        except ValueError as e:
            return str(e)
    # The app writes this file itself on first launch, so its absence means
    # the app has never run here — or was never installed.
    if config.is_file():
        return None
    return f"{config}: no such file — install the Claude desktop app and launch it once"


def detect(client: str, *, config: Path | None = None) -> bool:
    """Is this client on this machine at all?

    Absence is not an error and not a question: nothing is written for a
    client that is not here, and :func:`connect_gaps` stays silent about one.
    """
    return absence(client, config=config) is None


def _argv(anchor: Path, roots: Sequence[Path]) -> list[str]:
    """What a client executes: the anchor's shim, serving every root in order."""
    argv = [str(_shim(anchor)), "serve"]
    for root in roots:
        argv += ["--instance", str(root)]
    return argv


def server_entry(*, anchor: Path, roots: Sequence[Path], path: str | None) -> dict[str, object]:
    """The ``mcpServers.dex`` value: what the client runs, over what, with which PATH.

    Args:
        anchor: The instance whose shim launches the server — its pin is the
            version authority.
        roots: The instance roots to serve, absolute and resolved, in order.
        path: The PATH the client must spawn the command with, or ``None`` to
            let it inherit the one it has.

    Returns:
        The entry, ready to be filed under :data:`SERVER`.
    """
    command, *args = _argv(anchor, roots)
    entry: dict[str, object] = {"command": command, "args": args}
    if path is not None:
        entry["env"] = {"PATH": path}
    return entry


def connect(config: Path, *, anchor: Path, roots: Sequence[Path], path: str | None) -> str:
    """Merge the dex server entry into the desktop app's config at ``config``.

    Args:
        config: The client's config file, which must already exist.
        anchor: The instance whose ``bin/dex`` the client will run.
        roots: The instance roots to serve, absolute and resolved, in order.
        path: The PATH to write into the entry's environment, or ``None``.

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
    return (
        f"desktop app: {verb} the '{SERVER}' server in {config} — {_served(roots)}, "
        f"launched through {_shim(anchor)}. Quit the app entirely (⌘Q) and relaunch it; "
        "it spawns the server at launch."
    )


def connect_code(*, anchor: Path, roots: Sequence[Path]) -> str:
    """File the dex server in Claude Code's user scope, through its own CLI.

    Args:
        anchor: The instance whose ``bin/dex`` Claude Code will run.
        roots: The instance roots to serve, absolute and resolved, in order.

    Returns:
        The one-line summary.

    Raises:
        ValueError: The CLI is missing, or refused. Its own stderr is the
            diagnostic — this adds nothing to a message the tool wrote about
            its own config.
    """
    existing = _entry(code_config_path())
    verb = "replaced" if existing is not None else "added"
    # Remove before add: `add` refuses a name that already exists rather than
    # replacing it, and re-running with a changed instance list is how the
    # served set changes. The remove's exit code is ignored — "no such server"
    # is an error to that CLI and exactly the state this wants.
    _claude("remove", "-s", "user", SERVER, check=False)
    _claude("add", "-s", "user", SERVER, "--", *_argv(anchor, roots))
    return (
        f"Claude Code: {verb} the '{SERVER}' server in user scope — {_served(roots)}, "
        f"launched through {_shim(anchor)}. New sessions pick it up; restart any that "
        "are open."
    )


def _claude(*args: str, check: bool = True) -> str:
    """Run one ``claude mcp`` subcommand, or raise with what it said.

    Args:
        *args: The subcommand and its arguments, after ``claude mcp``.
        check: Whether a non-zero exit is a failure. The clearing remove
            passes ``False``: "no such server" is that CLI's error and this
            command's goal, and the two are not worth telling apart when the
            add that follows reports anything that actually went wrong.

    Raises:
        ValueError: The CLI is absent, or exited non-zero while ``check``.
    """
    binary = shutil.which("claude")
    if binary is None:
        raise ValueError("the claude CLI is not on PATH — nothing was written")
    try:
        # No shell, and every argument is built here from the validated roster
        # — the owner's own --instance paths, never anything a fetch returned.
        done = subprocess.run(  # noqa: S603
            [binary, "mcp", *args], capture_output=True, text=True, check=False
        )
    except OSError as e:
        raise ValueError(f"could not run the claude CLI ({e}) — nothing was written") from None
    if check and done.returncode != 0:
        detail = (done.stderr or done.stdout).strip() or f"exit {done.returncode}"
        raise ValueError(f"`claude mcp {' '.join(args)}` failed: {detail}")
    return done.stdout


def _served(roots: Sequence[Path]) -> str:
    """The roster as the summary states it."""
    return f"{len(roots)} instance(s) ({', '.join(root.name for root in roots)})"


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


def _servers(config: Path) -> dict[str, object] | None:
    """A client's ``mcpServers`` mapping, or ``None`` where nothing can be said.

    A read that never raises: it runs wherever sync runs, including machines
    with no chat client on them, and every unreadable shape means the same
    thing — a config ``connect`` itself would refuse, whose diagnosis belongs
    to the command someone asked for and not to a sync report.
    """
    try:
        data = _config(config)
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers", {})
    return servers if isinstance(servers, dict) else None


def _entry(config: Path) -> object | None:
    """This machine's dex server entry in ``config``, or ``None`` for no answer."""
    servers = _servers(config)
    return None if servers is None else servers.get(SERVER)


def connect_gaps(root: Path, *, config: Path | None = None) -> list[dict[str, str]]:
    """What stands between the instance at ``root`` and each client's chat.

    A read, never a write, and silent wherever it cannot be certain: this
    answers a report rather than a command, and it runs wherever sync runs —
    Linux boxes, cloud runners, machines with no chat client on them. An
    unknown config location, an absent file, one that is not JSON or not an
    object: each is no answer at all. A config this cannot read is a config
    ``connect`` would refuse, and saying so is ``connect``'s job in front of
    someone who asked for it, not a sync report's.

    Args:
        root: The instance root to look for.
        config: The desktop client config to read; ``None`` resolves the
            platform's known location.

    Returns:
        One ``{"client": …, "gap": …}`` per client with something to say, in
        :data:`CLIENTS` order. The gap is ``"unconnected"`` when a client
        holds no dex server at all, ``"unlisted"`` when its dex server does
        not serve ``root``, and ``"broken"`` when the command it would launch
        is no longer on disk — listing a root is not the same as being able to
        launch it, and a missing command fails inside a process that reports
        nothing back.
    """
    gaps = []
    for client in CLIENTS:
        if not detect(client, config=config):
            continue
        gap = _gap(root, _client_config(client, config))
        if gap is not None:
            gaps.append({"client": client, "gap": gap})
    return gaps


def _client_config(client: str, config: Path | None) -> Path | None:
    """The config file to read for one client, or ``None`` if its home is unknown."""
    if client == "code":
        return code_config_path()
    if config is not None:
        return config
    try:
        return default_config_path()
    except ValueError:
        return None


def _gap(root: Path, config: Path | None) -> str | None:
    """One client's gap, read from its config, silent wherever it cannot be sure."""
    if config is None:
        return None
    if not config.exists():
        # Only a present client reaches here, so an absent config is not an
        # absent client: Claude Code writes this file the first time a server
        # is added, and never having done so is exactly "unconnected".
        return "unconnected"
    servers = _servers(config)
    if servers is None:
        return None
    entry = servers.get(SERVER)
    if entry is None:
        return "unconnected"
    if root.resolve() not in _served_roots(entry):
        return "unlisted"
    return None if _command(entry) else "broken"


def _command(entry: object) -> bool:
    """Does the entry's command still exist on disk?"""
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    return isinstance(command, str) and Path(command).is_file()


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

    An instance like any other, plus the one file the clients actually run: a
    config naming a command that is not there fails inside a process that
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
    """The argparse tree: dex-connect --instance PATH [--client C] [--anchor/--config PATH]."""
    parser = argparse.ArgumentParser(
        prog="dex-connect",
        description="Merge a 'dex' MCP server entry into this machine's chat clients "
        "so their sessions can search, read and capture into these instances.",
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
        "--client",
        dest="clients",
        action="append",
        choices=CLIENTS,
        help="a client to write; repeat for each one (default: every client found "
        "on this machine). 'desktop' is the Claude desktop app's chat, 'code' is "
        "Claude Code at user scope",
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
        help="the desktop app's config file to merge into (default: its location on macOS)",
    )
    return parser


def _write(client: str, *, anchor: Path, roots: Sequence[Path], config: Path | None) -> str:
    """One client's write, dispatched."""
    if client == "code":
        return connect_code(anchor=anchor, roots=roots)
    return connect(
        config if config is not None else default_config_path(),
        anchor=anchor,
        roots=roots,
        # Captured from the installing shell: a GUI-spawned server gets
        # launchd's bare PATH, where uvx does not exist.
        path=os.environ.get("PATH", os.defpath),
    )


def main(argv: list[str] | None = None) -> None:
    """Parse, validate the roster and the anchor, write each client, print the summary."""
    args = build_parser().parse_args(argv)
    # Named clients are written or explained; unnamed means whatever is here.
    asked = tuple(dict.fromkeys(args.clients)) if args.clients else CLIENTS
    written: list[str] = []
    skipped: list[str] = []
    try:
        anchor = _anchor_root(args.anchor or args.instances[0])
        roots = _instance_roots(args.instances)
        for client in asked:
            missing = absence(client, config=args.config)
            if missing is not None:
                skipped.append(f"{client}: skipped — {missing}")
                continue
            written.append(_write(client, anchor=anchor, roots=roots, config=args.config))
    except (OSError, ValueError) as e:
        sys.exit(f"dex-connect: {e}")
    if not written:
        # A run that reached no client wrote nothing, and a zero exit on
        # nothing is how an owner ends up believing they are connected.
        sys.exit("dex-connect: nothing was written — " + "; ".join(skipped))
    sys.stdout.write("\n".join(written + skipped) + "\n")


if __name__ == "__main__":
    main()
