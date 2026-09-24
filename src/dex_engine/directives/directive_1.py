"""Directive 1: the owner's CLAUDE.md, rehomed now that the engine owns the file.

An instance's CLAUDE.md used to be the owner's: a title, the list of what
the instance covers, and for some the facts a Discord pull needs. Sync now
replaces it with the engine's once git history holds it. The engine finds
the owner's version in that history and hands it to the session in the
materials, beside the server and channel ids at the top of each Discord
export, and the session gives every part a home: what the instance reads
for becomes ``lens.md``, the Discord server and channels become the
``discord`` key of ``state/config.json``, and the rest is named in the
commit that records the directive.

It always completes. An instance whose history holds no version of the
owner's, or one whose scope is still the old template's placeholders, has
no scope to carry, and the directive writes nothing to ``lens.md``: with
none there, the instance is a general knowledge dex that reads for
anything, and one already there is left as it is.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path

from dex_engine import seeds
from dex_engine.gitread import git_output
from dex_engine.lens import read_lens
from dex_engine.pipeline.types import Config, Instance
from dex_engine.template import bundled_template

__all__ = [
    "DISCORD_BEGIN",
    "DISCORD_END",
    "ENGINE_OWNED",
    "EXPORT_HEAD_LIMIT",
    "INTENT",
    "NO_EXPORTS",
    "NO_OWNER",
    "NO_SCOPE",
    "OWNER_BEGIN",
    "OWNER_END",
    "SCOPE_PLACEHOLDERS",
    "SEED_BEGIN",
    "SEED_END",
    "DiscordExport",
    "OwnerClaude",
    "check",
    "discord_exports",
    "materials",
    "owner_claude",
    "render",
    "states_scope",
    "unmet",
]

INTENT = "Rehome the owner's CLAUDE.md: its scope becomes lens.md, its Discord facts become config"

# What every engine copy of CLAUDE.md says of itself, read with its
# whitespace collapsed; no version an owner wrote says it.
ENGINE_OWNED = "This file is engine-owned: `bin/dex sync` overwrites it"

# The scope lines the pre-lens template shipped for the owner to replace,
# in both forms it ever had. A version still holding one states no scope.
SCOPE_PLACEHOLDERS = frozenset({"- <topic>", "- **In**: <define>"})

OWNER_BEGIN = "----- the owner's CLAUDE.md, from commit {commit} -----"
OWNER_END = "----- end of the owner's CLAUDE.md -----"
NO_OWNER = "No committed version of CLAUDE.md is the owner's: there is no stated scope to carry."
NO_SCOPE = (
    "The owner's CLAUDE.md states no scope: it still holds the old template's scope "
    "placeholders, so there is no stated scope to carry."
)
SEED_BEGIN = "----- the seed lens for this instance -----"
SEED_END = "----- end of the seed lens -----"
DISCORD_BEGIN = "----- the Discord exports under raw/discord/ -----"
DISCORD_END = "----- end of the Discord exports -----"
NO_EXPORTS = "There are no Discord exports under raw/discord/."

# DiscordChatExporter writes the server and the channel ahead of the
# messages, so their ids sit in the first few kilobytes of an export of any
# size. A read that has not reached them by this many characters stops.
EXPORT_HEAD_LIMIT = 1 << 20
_EXPORT_FIRST_READ = 1 << 12

_DISCORD_ID_RE = re.compile(r"[0-9]+")
_JSON = json.JSONDecoder()
_STRUCTURAL_RE = re.compile(r"[ \t\n\r]*([{}:,])[ \t\n\r]*")

Git = Callable[[Path, Sequence[str]], str | None]


@dataclass(frozen=True, slots=True, kw_only=True)
class OwnerClaude:
    """The owner's CLAUDE.md as git history holds it, and the commit it is read from."""

    commit: str
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscordExport:
    """One export under ``raw/discord/``: the ids its exporter wrote, or why they cannot be read.

    Attributes:
        name: The export's directory, which is the channel's name in config.
        guild: ``guild.id`` as the exporter wrote it, or ``None`` when unreadable.
        channel: ``channel.id`` as the exporter wrote it, or ``None`` when unreadable.
        unreadable: Why the ids cannot be read, or ``None`` when they can.
    """

    name: str
    guild: str | None = None
    channel: str | None = None
    unreadable: str | None = None


def check(root: Path) -> list[str]:
    """What the instance at ``root`` still lacks, read against the running engine's template."""
    return unmet(root, bundled_template())


def materials(root: Path) -> str:
    """The owner's CLAUDE.md, the seed lens when there is a scope to carry, the Discord exports."""
    return render(root, bundled_template())


def owner_claude(root: Path, *, git: Git = git_output) -> OwnerClaude | None:
    """The newest committed CLAUDE.md that is not an engine copy, or ``None`` when there is none.

    Walks the commits that changed CLAUDE.md, newest first, passing over
    one whose file git cannot show (the commit that deleted it) and every
    engine copy, so an owner's version behind any number of engine
    releases is still the one found.

    Args:
        root: The instance root.
        git: The git seam: what ``git -C root <args>`` printed, or ``None``.
    """
    listing = git(root, ["log", "--format=%H", "--", "CLAUDE.md"]) or ""
    for commit in listing.split():
        text = git(root, ["show", f"{commit}:./CLAUDE.md"])
        if text is not None and ENGINE_OWNED not in " ".join(text.split()):
            return OwnerClaude(commit=commit, text=text)
    return None


def states_scope(owner: OwnerClaude | None) -> bool:
    """Whether there is an owner's CLAUDE.md, with a scope past the old template's placeholders."""
    if owner is None:
        return False
    return not any(line.strip() in SCOPE_PLACEHOLDERS for line in owner.text.splitlines())


def discord_exports(root: Path) -> list[DiscordExport]:
    """Every export under ``raw/discord/`` in the instance at ``root``, by directory name.

    Each ``messages.json`` is read only as far as its ``guild`` and
    ``channel``, in chunks that double up to :data:`EXPORT_HEAD_LIMIT`
    characters, so none of its messages is read. An export whose ids cannot
    be read is listed with the reason, never raised.
    """
    top = root / "raw" / "discord"
    directories = sorted(path for path in top.iterdir() if path.is_dir()) if top.is_dir() else []
    return [_read_export(directory) for directory in directories]


def render(root: Path, template: Traversable, *, git: Git = git_output) -> str:
    """The materials: the owner's CLAUDE.md, the seed lens, then the Discord exports.

    A line saying there is no owner's version stands in for the first when
    there is none. The seed lens is the layout a stated scope is written
    into, so it is printed only when there is one to carry. The exports are
    listed whatever the owner's CLAUDE.md says, with a line standing in
    when there are none.

    Args:
        root: The instance root, whose directory name is the instance's.
        template: The template tree whose ``lens.md`` is the seed.
        git: The git seam the owner's version is found through.
    """
    owner = owner_claude(root, git=git)
    parts = [*_owner_part(owner), ""]
    if states_scope(owner):
        parts += [SEED_BEGIN, seeds.lens(template, root.name).rstrip("\n"), SEED_END, ""]
    parts += [*_exports_part(discord_exports(root)), ""]
    return "\n".join(parts)


def unmet(root: Path, template: Traversable, *, git: Git = git_output) -> list[str]:
    """Every condition not yet met: the lens, and a config that parses.

    With a stated scope to carry, ``lens.md`` must state a lens: there, not
    empty, and free of the seed's placeholder lines. With none, the lens is
    no condition at all, since ``lens.md`` absent is a general knowledge dex
    and one already there is the owner's.

    Args:
        root: The instance root.
        template: The template tree whose ``lens.md`` is the seed.
        git: The git seam the owner's version is found through.

    Returns:
        One message per unmet condition, empty once the directive is done.
    """
    instance = Instance(root=root)
    owner = owner_claude(root, git=git)
    findings = (_lens_condition(instance, template, owner), _config_finding(instance))
    return [finding for finding in findings if finding is not None]


def _owner_part(owner: OwnerClaude | None) -> list[str]:
    if owner is None:
        return [NO_OWNER]
    shown = [OWNER_BEGIN.format(commit=owner.commit), owner.text.rstrip("\n"), OWNER_END]
    return shown if states_scope(owner) else [*shown, "", NO_SCOPE]


def _exports_part(exports: list[DiscordExport]) -> list[str]:
    if not exports:
        return [NO_EXPORTS]
    lines = [
        f"- {export.name}: unreadable ({export.unreadable})"
        if export.unreadable is not None
        else f"- {export.name}: guild.id {export.guild}, channel.id {export.channel}"
        for export in exports
    ]
    return [DISCORD_BEGIN, *lines, DISCORD_END]


def _read_export(directory: Path) -> DiscordExport:
    try:
        head = _export_head(directory / "messages.json")
    except FileNotFoundError:
        return DiscordExport(name=directory.name, unreadable="no messages.json")
    except (OSError, ValueError) as e:
        return DiscordExport(name=directory.name, unreadable=e.__class__.__name__)
    guild, channel = _id_in(head.get("guild")), _id_in(head.get("channel"))
    if guild is None or channel is None:
        return DiscordExport(
            name=directory.name,
            unreadable="no guild.id and channel.id as strings of digits ahead of its messages",
        )
    return DiscordExport(name=directory.name, guild=guild, channel=channel)


def _export_head(path: Path) -> dict[str, object]:
    """The export's top-level members ahead of its messages, read in chunks that double.

    Raises:
        OSError: The file cannot be read.
        ValueError: It is not UTF-8, or its head does not parse within the limit.
    """
    with path.open(encoding="utf-8") as handle:
        text = handle.read(_EXPORT_FIRST_READ)
        while True:
            try:
                return _head(text)
            except ValueError:
                more = handle.read(len(text)) if len(text) < EXPORT_HEAD_LIMIT else ""
                if not more:
                    raise
                text += more


def _head(text: str) -> dict[str, object]:
    """The top-level members ``text`` opens with, up to ``guild`` and ``channel`` both.

    Stops early at ``messages`` or at the object's end, whichever comes first.

    Raises:
        ValueError: The text ends, or stops being JSON, before that.
    """
    head: dict[str, object] = {}
    _, pos = _structural(text, 0, "{")
    if text.startswith("}", pos):
        return head
    while True:
        key, pos = _JSON.raw_decode(text, pos)
        if key == "messages":
            return head
        if not isinstance(key, str):
            raise json.JSONDecodeError("Expecting property name", text, pos)
        _, pos = _structural(text, pos, ":")
        head[key], pos = _JSON.raw_decode(text, pos)
        if {"guild", "channel"} <= head.keys():
            return head
        mark, pos = _structural(text, pos, ",", "}")
        if mark == "}":
            return head


def _structural(text: str, pos: int, *expected: str) -> tuple[str, int]:
    """The structural character at ``pos``, one of ``expected``, and where the text goes on."""
    found = _STRUCTURAL_RE.match(text, pos)
    if found is None or found.group(1) not in expected:
        raise json.JSONDecodeError(f"Expecting one of {expected!r}", text, pos)
    return found.group(1), found.end()


def _id_in(part: object) -> str | None:
    value = part.get("id") if isinstance(part, dict) else None
    return value if isinstance(value, str) and _DISCORD_ID_RE.fullmatch(value) else None


def _lens_condition(
    instance: Instance, template: Traversable, owner: OwnerClaude | None
) -> str | None:
    if not states_scope(owner):
        return None
    return read_lens(instance, template).finding


def _config_finding(instance: Instance) -> str | None:
    try:
        Config.load(instance.config_path)
    except (OSError, ValueError) as e:
        return f"`state/config.json` does not parse: {e}"
    return None
