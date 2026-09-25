"""Tests for directive 1: the owner's CLAUDE.md, rehomed into lens.md and config."""

import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from dex_engine import seeds
from dex_engine.directive import MATERIALS_BEGIN, MATERIALS_END
from dex_engine.directives import directive_1, discover
from dex_engine.directives.directive_1 import (
    DISCORD_BEGIN,
    DISCORD_END,
    ENGINE_OWNED,
    EXPORT_HEAD_LIMIT,
    NO_EXPORTS,
    NO_OWNER,
    NO_SCOPE,
    OWNER_BEGIN,
    OWNER_END,
    SEED_BEGIN,
    SEED_END,
    DiscordExport,
    OwnerClaude,
    check,
    discord_exports,
    materials,
    owner_claude,
    render,
    states_scope,
    unmet,
)

TEMPLATE = Path(__file__).resolve().parents[2] / "instance"
ENGINE_CLAUDE_MD = (TEMPLATE / "CLAUDE.md").read_text(encoding="utf-8")

# The pre-lens template's CLAUDE.md, as an instance that never filled it in
# still has it.
OLD_TEMPLATE = """\
# <instance> — <Domain> Knowledge Base (a dex instance)

A personal, LLM-maintained knowledge base. You (Claude) ARE the application:
you operate this repo per its contract — operations, dataflow, invariants,
conventions — which is engine-synced:

@.claude/dex-contract.md

## In scope

- <topic>
- <topic>

Anything not listed is out of scope. When unsure, ask the owner rather
than guessing. The list is mirrored in README.md — any scope change must
update both files.
"""

OWNER = """\
# dex-cooking — Home Cooking Knowledge Base (a dex instance)

@.claude/dex-contract.md

## In scope

- weeknight recipes, and the technique behind them
"""

LENS = """\
# dex-cooking

Home Cooking Knowledge Base

## Reads for

- weeknight recipes, and the technique behind them
"""

DISCORD = {"guild": "1000", "channels": {"general": "2000"}}

NO_IDS = "no guild.id and channel.id as strings of digits ahead of its messages"

# Every history with no stated scope to carry: none of the owner's, or the
# owner's still the unfilled pre-lens template, however many engine copies
# stand in front of it.
NO_SCOPE_HISTORIES = pytest.mark.parametrize(
    "history",
    [[], [("t1", OLD_TEMPLATE)], [("e1", ENGINE_CLAUDE_MD), ("t1", OLD_TEMPLATE)]],
    ids=["no-owner", "unfilled-template", "unfilled-behind-engine"],
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "dex-cooking"
    (root / "state").mkdir(parents=True)
    return root


def write(root: Path, rel: str, text: str) -> None:
    (root / rel).write_text(text, encoding="utf-8")


def config(root: Path, value: object) -> None:
    write(root, "state/config.json", json.dumps(value))


def export_text(guild: object = "1000", channel: object = "2000", *, preamble: int = 0) -> str:
    """An export as DiscordChatExporter writes one, behind ``preamble`` characters of padding.

    The padding is a top-level member ahead of the guild, so the ids sit
    that much further in.
    """
    head = {"preamble": "x" * preamble} if preamble else {}
    return json.dumps(
        {
            **head,
            "guild": {"id": guild, "name": "a server", "iconUrl": "messages.json_Files/icon.png"},
            "channel": {"id": channel, "type": "GuildTextChat", "name": "general", "topic": None},
            "exportedAt": "2026-09-01T00:00:00+00:00",
            "messages": [{"id": "3000", "type": "Default", "content": "never read"}],
        },
        indent=2,
    )


def ids_end(preamble: int) -> int:
    """How many characters of :func:`export_text` run up to the channel object's close."""
    text = export_text(preamble=preamble)
    return text.index("}", text.index('"channel"')) + 1


def preamble_ending_ids_at(end: int) -> int:
    """The preamble that closes the channel object at character ``end`` of the export."""
    return end - ids_end(1) + 1


def put_export(root: Path, name: str, content: str | bytes) -> None:
    path = root / "raw" / "discord" / name / "messages.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


class ScriptedGit:
    """A git seam answering ``log`` and ``show`` from a scripted history, newest first."""

    def __init__(self, history: Sequence[tuple[str, str | None]], *, log: bool = True) -> None:
        self.history = dict(history)
        self.order = [commit for commit, _ in history]
        self.log = log
        self.asked: list[tuple[Path, list[str]]] = []

    def __call__(self, root: Path, args: Sequence[str]) -> str | None:
        self.asked.append((root, list(args)))
        if args[0] == "log":
            return "".join(f"{commit}\n" for commit in self.order) if self.log else None
        commit = args[1].removesuffix(":./CLAUDE.md")
        return self.history[commit]


def owner_of(text: str) -> OwnerClaude:
    return OwnerClaude(commit="c0ffee", text=text)


class History:
    """A real repository under ``root`` whose CLAUDE.md commits a test lays down."""

    def __init__(self, root: Path, git) -> None:
        self.root = root
        self.git = git
        git(root, "init", "-q")

    def commit(self, text: str | None, message: str) -> str:
        path = self.root / "CLAUDE.md"
        if text is None:
            path.unlink()
        else:
            path.write_text(text, encoding="utf-8")
        self.git(self.root, "add", "-A")
        self.git(self.root, "commit", "-q", "-m", message)
        return subprocess.run(  # noqa: S603 — test-built args, no shell
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],  # noqa: S607 — PATH resolution is the dependency contract
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()


@pytest.fixture
def history(root: Path, own_git) -> History:
    return History(root, own_git)


def engine_copy(release: int) -> str:
    """An engine CLAUDE.md as a given release shipped it: each says it is engine-owned."""
    return f"{ENGINE_CLAUDE_MD}\n<!-- release {release} -->\n"


class TestOwnerClaude:
    def test_with_no_history_there_is_none(self, history):
        assert owner_claude(history.root) is None

    @pytest.mark.usefixtures("own_git")
    def test_outside_a_repository_there_is_none(self, root):
        assert owner_claude(root) is None

    def test_only_engine_versions_is_none(self, history):
        history.commit(engine_copy(1), "sync: engine v1")
        history.commit(engine_copy(2), "sync: engine v2")
        assert owner_claude(history.root) is None

    def test_the_owner_s_version_behind_one_engine_version(self, history):
        owned = history.commit(OWNER, "personalise")
        history.commit(ENGINE_CLAUDE_MD, "sync: machinery refresh")
        assert owner_claude(history.root) == OwnerClaude(commit=owned, text=OWNER)

    def test_the_owner_s_version_behind_two_engine_versions(self, history):
        owned = history.commit(OWNER, "personalise")
        history.commit(engine_copy(1), "sync: engine v1")
        history.commit(engine_copy(2), "sync: engine v2")
        assert owner_claude(history.root) == OwnerClaude(commit=owned, text=OWNER)

    def test_the_newest_owner_s_version_wins(self, history):
        history.commit(OLD_TEMPLATE, "initial instance")
        edited = history.commit(OWNER, "personalise")
        history.commit(ENGINE_CLAUDE_MD, "sync: machinery refresh")
        assert owner_claude(history.root) == OwnerClaude(commit=edited, text=OWNER)

    def test_a_deleted_then_restored_file_is_read_past_its_deletion(self, history):
        owned = history.commit(OWNER, "personalise")
        history.commit(None, "drop CLAUDE.md")
        history.commit(ENGINE_CLAUDE_MD, "sync: machinery refresh")
        assert owner_claude(history.root) == OwnerClaude(commit=owned, text=OWNER)

    def test_an_unfilled_template_is_the_owner_s_version(self, history):
        owned = history.commit(OLD_TEMPLATE, "initial instance")
        history.commit(ENGINE_CLAUDE_MD, "sync: machinery refresh")
        found = owner_claude(history.root)
        assert found == OwnerClaude(commit=owned, text=OLD_TEMPLATE)
        assert not states_scope(found)

    def test_an_uncommitted_owner_version_is_not_history(self, history):
        owned = history.commit(OWNER, "personalise")
        write(history.root, "CLAUDE.md", OWNER + "- an edit nobody committed\n")
        assert owner_claude(history.root) == OwnerClaude(commit=owned, text=OWNER)

    def test_the_instance_s_own_claude_md_is_read_inside_a_larger_repository(
        self, tmp_path, own_git
    ):
        outer = tmp_path / "outer"
        outer.mkdir()
        own_git(outer, "init", "-q")
        (outer / "CLAUDE.md").write_text(OWNER.replace("cooking", "outer"), encoding="utf-8")
        inner = outer / "dex-cooking"
        inner.mkdir()
        (inner / "CLAUDE.md").write_text(OWNER, encoding="utf-8")
        own_git(outer, "add", "-A")
        own_git(outer, "commit", "-q", "-m", "both")
        found = owner_claude(inner)
        assert found is not None
        assert found.text == OWNER

    def test_the_engine_mark_is_read_across_a_rewrap(self):
        head, tail = ENGINE_OWNED.split(": ", 1)
        rewrapped = f"# dex instance\n\n{head}:\n{tail}, so never write into it.\n"
        git = ScriptedGit([("e1", rewrapped), ("o1", OWNER)])
        assert owner_claude(Path("/instance"), git=git) == OwnerClaude(commit="o1", text=OWNER)

    def test_a_version_git_cannot_show_is_passed_over(self):
        git = ScriptedGit([("gone", None), ("o1", OWNER)])
        assert owner_claude(Path("/instance"), git=git) == OwnerClaude(commit="o1", text=OWNER)

    def test_a_log_git_cannot_give_is_no_history(self):
        git = ScriptedGit([("o1", OWNER)], log=False)
        assert owner_claude(Path("/instance"), git=git) is None

    def test_git_is_asked_about_the_instance_s_own_claude_md(self):
        git = ScriptedGit([("o1", OWNER)])
        owner_claude(Path("/instance"), git=git)
        assert git.asked == [
            (Path("/instance"), ["log", "--format=%H", "--", "CLAUDE.md"]),
            (Path("/instance"), ["show", "o1:./CLAUDE.md"]),
        ]


class TestStatesScope:
    def test_no_owner_s_version_states_no_scope(self):
        assert not states_scope(None)

    def test_the_unfilled_template_states_no_scope(self):
        assert not states_scope(owner_of(OLD_TEMPLATE))

    @pytest.mark.parametrize(
        "line", ["- <topic>", "  - <topic>  ", "- **In**: <define>"], ids=["topic", "padded", "in"]
    )
    def test_any_line_still_the_template_s_placeholder_states_no_scope(self, line):
        assert not states_scope(owner_of(f"# dex-cooking\n\n## In scope\n\n{line}\n"))

    def test_a_filled_scope_states_one(self):
        assert states_scope(owner_of(OWNER))

    def test_a_placeholder_word_inside_an_owner_s_line_is_not_the_placeholder(self):
        assert states_scope(owner_of("## In scope\n\n- recipes, not <topic> pages\n"))


class TestDiscordExports:
    def test_one_export_gives_its_directory_and_ids(self, root):
        put_export(root, "general", export_text())
        assert discord_exports(root) == [
            DiscordExport(name="general", guild="1000", channel="2000")
        ]

    def test_several_exports_are_listed_by_directory_name(self, root):
        put_export(root, "zeta", export_text("1000", "2003"))
        put_export(root, "alpha", export_text("1000", "2001"))
        put_export(root, "mid", export_text("1001", "2002"))
        assert discord_exports(root) == [
            DiscordExport(name="alpha", guild="1000", channel="2001"),
            DiscordExport(name="mid", guild="1001", channel="2002"),
            DiscordExport(name="zeta", guild="1000", channel="2003"),
        ]

    def test_no_raw_discord_directory_is_no_exports(self, root):
        assert discord_exports(root) == []

    def test_an_empty_raw_discord_directory_is_no_exports(self, root):
        (root / "raw" / "discord").mkdir(parents=True)
        assert discord_exports(root) == []

    def test_a_file_beside_the_exports_is_not_one(self, root):
        put_export(root, "general", export_text())
        (root / "raw" / "discord" / ".DS_Store").write_bytes(b"\x00")
        assert [export.name for export in discord_exports(root)] == ["general"]

    def test_a_raw_discord_that_is_a_file_is_no_exports(self, root):
        (root / "raw").mkdir()
        (root / "raw" / "discord").write_text("", encoding="utf-8")
        assert discord_exports(root) == []

    def test_a_directory_with_no_messages_json_is_unreadable(self, root):
        (root / "raw" / "discord" / "general" / "messages.json_Files").mkdir(parents=True)
        assert discord_exports(root) == [
            DiscordExport(name="general", unreadable="no messages.json")
        ]

    def test_a_messages_json_that_cannot_be_read_is_unreadable(self, root):
        (root / "raw" / "discord" / "general" / "messages.json").mkdir(parents=True)
        assert discord_exports(root) == [
            DiscordExport(name="general", unreadable="IsADirectoryError")
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "not json",
            "[]",
            '{"guild": {"id": "1000"',
            '{"guild" {"id": "1000"}, "channel": {"id": "2000"}}',
            '{"guild": {"id": "1000"} "channel": {"id": "2000"}}',
            '{"guild": {"id": "1000"}; "channel": {"id": "2000"}}',
            '{[1]: 2, "guild": {"id": "1000"}, "channel": {"id": "2000"}}',
        ],
        ids=[
            "empty",
            "not-json",
            "array",
            "truncated",
            "no-colon",
            "no-comma",
            "wrong-separator",
            "key-not-a-string",
        ],
    )
    def test_malformed_json_is_unreadable(self, root, text):
        put_export(root, "general", text)
        assert discord_exports(root) == [
            DiscordExport(name="general", unreadable="JSONDecodeError")
        ]

    def test_a_head_that_is_not_utf8_is_unreadable(self, root):
        put_export(root, "general", b'{"guild": {"id": "1000", "name": "\xff"}}')
        assert discord_exports(root) == [
            DiscordExport(name="general", unreadable="UnicodeDecodeError")
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "{}",
            " { } ",
            '{"guild": {"id": "1000"}}',
            '{"channel": {"id": "2000"}}',
            '{"guild": {"id": "1000"}, "messages": [], "channel": {"id": "2000"}}',
            export_text(1000, "2000"),
            export_text("1000", 2000),
            export_text("", "2000"),
            export_text("1000", "2000x"),
            export_text("1000", "٢٠٠٠"),
            '{"guild": "1000", "channel": {"id": "2000"}}',
            '{"guild": {"id": "1000"}, "channel": ["2000"]}',
        ],
        ids=[
            "empty-object",
            "spaced-empty-object",
            "no-channel",
            "no-guild",
            "channel-after-messages",
            "numeric-guild-id",
            "numeric-channel-id",
            "empty-guild-id",
            "non-digit-channel-id",
            "non-ascii-digits",
            "guild-not-an-object",
            "channel-not-an-object",
        ],
    )
    def test_a_head_without_both_ids_as_digit_strings_is_unreadable(self, root, text):
        put_export(root, "general", text)
        assert discord_exports(root) == [DiscordExport(name="general", unreadable=NO_IDS)]

    def test_members_in_any_order_and_any_spacing_are_read(self, root):
        put_export(
            root,
            "general",
            '\n\t{"exportedAt":"2026","channel":{"id":"2000"} ,\r\n"guild" : {"id" : "1000"},'
            '"messages":[]}',
        )
        assert discord_exports(root) == [
            DiscordExport(name="general", guild="1000", channel="2000")
        ]

    def test_an_export_cut_off_right_after_its_channel_still_gives_its_ids(self, root):
        put_export(root, "general", '{"guild": {"id": "1000"}, "channel": {"id": "2000"}')
        assert discord_exports(root) == [
            DiscordExport(name="general", guild="1000", channel="2000")
        ]

    def test_ids_after_a_large_preamble_are_read(self, root):
        # Far past `head -c 2000`, and past the first chunk the read takes.
        put_export(root, "general", export_text(preamble=100_000))
        assert discord_exports(root) == [
            DiscordExport(name="general", guild="1000", channel="2000")
        ]

    def test_ids_that_end_at_the_limit_are_read(self, root):
        preamble = preamble_ending_ids_at(EXPORT_HEAD_LIMIT)
        assert ids_end(preamble) == EXPORT_HEAD_LIMIT
        put_export(root, "general", export_text(preamble=preamble))
        assert discord_exports(root) == [
            DiscordExport(name="general", guild="1000", channel="2000")
        ]

    def test_ids_that_end_one_character_past_the_limit_are_unreadable(self, root):
        preamble = preamble_ending_ids_at(EXPORT_HEAD_LIMIT + 1)
        assert ids_end(preamble) == EXPORT_HEAD_LIMIT + 1
        put_export(root, "general", export_text(preamble=preamble))
        assert discord_exports(root) == [
            DiscordExport(name="general", unreadable="JSONDecodeError")
        ]

    def test_the_messages_behind_the_ids_are_never_read(self, root):
        # Bytes no UTF-8 read survives, a limit's length into the messages:
        # any read that reached them would list the export as unreadable.
        head, _ = export_text().split('"messages"')
        filler = "y" * EXPORT_HEAD_LIMIT
        put_export(
            root,
            "general",
            f'{head}"messages": [{{"content": "{filler}'.encode() + b'\xff"}]}',
        )
        assert discord_exports(root) == [
            DiscordExport(name="general", guild="1000", channel="2000")
        ]


class TestRender:
    def seed(self, root: Path) -> str:
        return seeds.lens(TEMPLATE, root.name)

    def test_the_owner_s_version_then_the_seed_lens(self, root):
        text = render(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)]))
        assert text == (
            f"{OWNER_BEGIN.format(commit='o1')}\n{OWNER}{OWNER_END}\n\n"
            f"{SEED_BEGIN}\n{self.seed(root)}{SEED_END}\n\n{NO_EXPORTS}\n"
        )

    def test_a_version_stating_no_scope_says_so_and_carries_no_seed(self, root):
        # With no scope to carry nothing is written into lens.md, so the
        # layout a scope is written into has no use.
        text = render(root, TEMPLATE, git=ScriptedGit([("t1", OLD_TEMPLATE)]))
        assert text == (
            f"{OWNER_BEGIN.format(commit='t1')}\n{OLD_TEMPLATE}{OWNER_END}\n\n{NO_SCOPE}\n\n"
            f"{NO_EXPORTS}\n"
        )

    def test_no_owner_s_version_is_said_in_its_place_with_no_seed(self, root):
        assert render(root, TEMPLATE, git=ScriptedGit([])) == f"{NO_OWNER}\n\n{NO_EXPORTS}\n"

    def test_the_exports_follow_the_seed_lens_between_their_delimiters(self, root):
        put_export(root, "general", export_text())
        put_export(root, "links", export_text("1000", "2001"))
        (root / "raw" / "discord" / "broken").mkdir()
        text = render(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)]))
        assert text == (
            f"{OWNER_BEGIN.format(commit='o1')}\n{OWNER}{OWNER_END}\n\n"
            f"{SEED_BEGIN}\n{self.seed(root)}{SEED_END}\n\n"
            f"{DISCORD_BEGIN}\n"
            "- broken: unreadable (no messages.json)\n"
            "- general: guild.id 1000, channel.id 2000\n"
            "- links: guild.id 1000, channel.id 2001\n"
            f"{DISCORD_END}\n"
        )

    @NO_SCOPE_HISTORIES
    def test_the_exports_are_listed_with_no_scope_to_carry(self, root, history):
        # An owner's CLAUDE.md with no scope may still name Discord facts,
        # and with none at all the list costs nothing.
        put_export(root, "general", export_text())
        text = render(root, TEMPLATE, git=ScriptedGit(history))
        assert text.endswith(
            f"\n\n{DISCORD_BEGIN}\n- general: guild.id 1000, channel.id 2000\n{DISCORD_END}\n"
        )

    def test_an_owner_s_version_without_a_final_newline_still_closes_on_its_own_line(self, root):
        text = render(root, TEMPLATE, git=ScriptedGit([("o1", OWNER.rstrip("\n"))]))
        assert f"{OWNER.rstrip(chr(10))}\n{OWNER_END}\n" in text

    @pytest.mark.parametrize(
        "tail", ["ends in TeX\n", "ends in a line break  \n"], ids=["letter", "spaces"]
    )
    def test_only_the_final_newline_is_trimmed_from_either_part(self, root, tmp_path, tail):
        template = tmp_path / "other-template"
        template.mkdir()
        write(template, "lens.md", f"# <instance name>\n\n{tail}")
        owner = f"# dex-cooking\n\n- {tail}"
        text = render(root, template, git=ScriptedGit([("o1", owner)]))
        assert f"- {tail}{OWNER_END}\n" in text
        assert text.endswith(f"\n{tail}{SEED_END}\n\n{NO_EXPORTS}\n")


class TestUnmet:
    def test_a_stated_scope_needs_a_stated_lens_and_parsing_config(self, root):
        git = ScriptedGit([("o1", OWNER)])
        write(root, "lens.md", LENS)
        config(root, {"internal_domains": [], "discord": DISCORD})
        assert unmet(root, TEMPLATE, git=git) == []

    def test_a_missing_config_is_fine(self, root):
        write(root, "lens.md", LENS)
        assert unmet(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)])) == []

    def test_with_a_stated_scope_a_missing_lens_is_unmet(self, root):
        assert unmet(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)])) == ["`lens.md` is missing"]

    def test_with_a_stated_scope_an_empty_lens_is_unmet(self, root):
        # No lens is a general knowledge dex, legitimate on its own, but the
        # owner stated a scope here, and carrying it is the directive's job.
        write(root, "lens.md", "\n  \n")
        assert unmet(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)])) == ["`lens.md` is empty"]

    def test_with_a_stated_scope_an_unreadable_lens_is_unmet(self, root):
        (root / "lens.md").write_bytes(b"\xff\xfe not text")
        assert unmet(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)])) == [
            "`lens.md` is unreadable (UnicodeDecodeError)"
        ]

    def test_with_a_stated_scope_the_unfilled_seed_is_unmet(self, root):
        write(root, "lens.md", seeds.lens(TEMPLATE, root.name))
        [finding] = unmet(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)]))
        assert finding.startswith("`lens.md` still holds the seed's placeholder text")

    def test_the_seed_placeholders_are_read_from_the_template_given(self, root, tmp_path):
        template = tmp_path / "other-template"
        template.mkdir()
        write(template, "lens.md", "# <instance name>\n\n<only this one>\n")
        write(root, "lens.md", "# dex-cooking\n\n<only this one>\n")
        assert unmet(root, template, git=ScriptedGit([("o1", OWNER)])) == [
            "`lens.md` still holds the seed's placeholder text (`<only this one>`)"
        ]

    @NO_SCOPE_HISTORIES
    def test_with_no_scope_to_carry_no_lens_completes_it(self, root, history):
        # Absent, lens.md makes a general knowledge dex: nothing to write.
        assert unmet(root, TEMPLATE, git=ScriptedGit(history)) == []

    @NO_SCOPE_HISTORIES
    @pytest.mark.parametrize(
        "lens",
        ["", "seed", LENS, b"\xff\xfe not text"],
        ids=["empty", "seed", "stated", "unreadable"],
    )
    def test_with_no_scope_to_carry_an_existing_lens_is_left_as_it_is(self, root, history, lens):
        if isinstance(lens, bytes):
            (root / "lens.md").write_bytes(lens)
        else:
            write(root, "lens.md", seeds.lens(TEMPLATE, root.name) if lens == "seed" else lens)
        assert unmet(root, TEMPLATE, git=ScriptedGit(history)) == []

    def test_config_that_is_not_json_is_unmet(self, root):
        write(root, "lens.md", LENS)
        write(root, "state/config.json", '{"discord": {"guild": "1000",')
        [finding] = unmet(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)]))
        assert finding.startswith("`state/config.json` does not parse: ")
        assert "invalid JSON" in finding

    def test_with_no_scope_to_carry_config_must_still_parse(self, root):
        write(root, "lens.md", seeds.lens(TEMPLATE, root.name))
        config(root, {"discord": {"guild": 1000, "channels": {"general": "2000"}}})
        [finding] = unmet(root, TEMPLATE, git=ScriptedGit([("t1", OLD_TEMPLATE)]))
        assert finding.startswith("`state/config.json` does not parse: ")

    @pytest.mark.parametrize(
        "discord",
        [
            {"guild": 1000, "channels": {"general": "2000"}},
            {"guild": "1000", "channels": {}},
            {"guild": "1000", "channels": {"general": "2000"}, "token": "x"},
        ],
        ids=["numeric-id", "no-channel", "extra-key"],
    )
    def test_a_discord_key_the_parser_refuses_is_unmet(self, root, discord):
        write(root, "lens.md", LENS)
        config(root, {"discord": discord})
        [finding] = unmet(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)]))
        assert finding.startswith("`state/config.json` does not parse: ")
        assert "discord" in finding

    def test_a_config_that_cannot_be_read_is_unmet(self, root):
        write(root, "lens.md", LENS)
        (root / "state" / "config.json").mkdir()
        [finding] = unmet(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)]))
        assert finding.startswith("`state/config.json` does not parse: ")

    def test_every_unmet_condition_is_named_lens_first(self, root):
        config(root, ["not", "an", "object"])
        path = root / "state" / "config.json"
        assert unmet(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)])) == [
            "`lens.md` is missing",
            f"`state/config.json` does not parse: {path}: expected a JSON object, got list",
        ]

    def test_the_owner_s_version_is_found_in_the_instance_asked_about(self, root):
        git = ScriptedGit([("o1", OWNER)])
        unmet(root, TEMPLATE, git=git)
        assert {asked_root for asked_root, _ in git.asked} == {root}


def directive_1_instructions() -> str:
    [shipped] = [directive for directive in discover() if directive.number == 1]
    return shipped.instructions


class TestShipped:
    def test_it_ships_as_directive_1_with_its_check_and_materials(self):
        [directive] = [directive for directive in discover() if directive.number == 1]
        assert directive.intent == directive_1.INTENT
        assert directive.check is check
        assert directive.materials is materials

    def test_its_instructions_name_every_delimiter_its_materials_print(self):
        instructions = directive_1_instructions()
        assert MATERIALS_BEGIN.startswith("===== materials")
        assert "`===== materials`" in instructions
        assert f"`{MATERIALS_END}`" in instructions
        assert f"`{OWNER_BEGIN.format(commit='<hash>')}`" in instructions
        for delimiter in (OWNER_END, SEED_BEGIN, SEED_END, DISCORD_BEGIN, DISCORD_END):
            assert f"`{delimiter}`" in instructions

    def test_its_instructions_leave_the_exports_to_the_materials(self):
        # Reading an export by hand prints its messages into the transcript
        # and leaves the ids to be parsed out of them.
        assert re.search(r"\bhead\b", directive_1_instructions()) is None

    def test_the_engine_s_claude_md_carries_the_mark_the_finder_passes_over(self):
        assert ENGINE_OWNED in " ".join(ENGINE_CLAUDE_MD.split())
        assert ENGINE_OWNED not in " ".join(OLD_TEMPLATE.split())


class TestTheShippedFunctions:
    @pytest.fixture(autouse=True)
    def _the_tree_s_template(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The wheel bundles instance/ as dex_engine/instance; a source
        # checkout has it at the repo root.
        monkeypatch.setattr(directive_1, "bundled_template", lambda: TEMPLATE)

    def test_check_and_materials_read_the_instance_s_own_history(self, history):
        owned = history.commit(OWNER, "personalise")
        history.commit(ENGINE_CLAUDE_MD, "sync: machinery refresh")
        root = history.root
        assert materials(root).startswith(f"{OWNER_BEGIN.format(commit=owned)}\n{OWNER}")
        assert check(root) == ["`lens.md` is missing"]
        write(root, "lens.md", LENS)
        assert check(root) == []

    def test_an_instance_that_never_stated_a_scope_completes_with_no_lens(self, history):
        history.commit(OLD_TEMPLATE, "initial instance")
        history.commit(ENGINE_CLAUDE_MD, "sync: machinery refresh")
        root = history.root
        assert NO_SCOPE in materials(root)
        assert SEED_BEGIN not in materials(root)
        assert not (root / "lens.md").exists()
        assert check(root) == []

    def test_materials_list_the_instance_s_own_exports(self, history):
        history.commit(OWNER, "personalise")
        root = history.root
        assert materials(root).endswith(f"\n\n{NO_EXPORTS}\n")
        put_export(root, "general", export_text())
        assert materials(root).endswith(
            f"\n\n{DISCORD_BEGIN}\n- general: guild.id 1000, channel.id 2000\n{DISCORD_END}\n"
        )
