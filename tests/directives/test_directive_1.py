"""Tests for directive 1: the owner's CLAUDE.md, rehomed into lens.md and config."""

import json
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from dex_engine import seeds
from dex_engine.directive import MATERIALS_BEGIN, MATERIALS_END
from dex_engine.directives import directive_1, discover
from dex_engine.directives.directive_1 import (
    ENGINE_OWNED,
    NO_OWNER,
    NO_SCOPE,
    OWNER_BEGIN,
    OWNER_END,
    SEED_BEGIN,
    SEED_END,
    OwnerClaude,
    check,
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


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "dex-cooking"
    (root / "state").mkdir(parents=True)
    return root


def write(root: Path, rel: str, text: str) -> None:
    (root / rel).write_text(text, encoding="utf-8")


def config(root: Path, value: object) -> None:
    write(root, "state/config.json", json.dumps(value))


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


class TestRender:
    def seed(self, root: Path) -> str:
        return seeds.lens(TEMPLATE, root.name)

    def test_the_owner_s_version_then_the_seed_lens(self, root):
        text = render(root, TEMPLATE, git=ScriptedGit([("o1", OWNER)]))
        assert text == (
            f"{OWNER_BEGIN.format(commit='o1')}\n{OWNER}{OWNER_END}\n\n"
            f"{SEED_BEGIN}\n{self.seed(root)}{SEED_END}\n"
        )

    def test_a_version_stating_no_scope_says_so_after_it(self, root):
        text = render(root, TEMPLATE, git=ScriptedGit([("t1", OLD_TEMPLATE)]))
        assert text == (
            f"{OWNER_BEGIN.format(commit='t1')}\n{OLD_TEMPLATE}{OWNER_END}\n\n{NO_SCOPE}\n\n"
            f"{SEED_BEGIN}\n{self.seed(root)}{SEED_END}\n"
        )

    def test_no_owner_s_version_is_said_in_its_place(self, root):
        text = render(root, TEMPLATE, git=ScriptedGit([]))
        assert text == f"{NO_OWNER}\n\n{SEED_BEGIN}\n{self.seed(root)}{SEED_END}\n"

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
        assert text.endswith(f"\n{tail}{SEED_END}\n")

    def test_the_instructions_command_writes_the_seed_lens_exactly(self, root):
        # The command the instructions give for writing the seed, run on the
        # materials, must produce the seed byte for byte.
        if shutil.which("awk") is None:
            pytest.skip("awk is not on PATH")
        [line] = [
            line
            for line in directive_1_instructions().splitlines()
            if line.startswith("bin/dex directive show 1 --materials | awk ")
        ]
        argv = shlex.split(line.split(" | ", 1)[1].rsplit(" > ", 1)[0])
        text = render(root, TEMPLATE, git=ScriptedGit([("t1", OLD_TEMPLATE)]))
        written = subprocess.run(  # noqa: S603 — the instructions' own awk program
            argv, input=text, capture_output=True, text=True, check=True
        ).stdout
        assert written == self.seed(root)


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
            "`lens.md` still holds the seed's placeholder text: `<only this one>`"
        ]

    @pytest.mark.parametrize(
        "history",
        [[], [("t1", OLD_TEMPLATE)], [("e1", ENGINE_CLAUDE_MD), ("t1", OLD_TEMPLATE)]],
        ids=["no-owner", "unfilled-template", "unfilled-behind-engine"],
    )
    def test_with_no_scope_to_carry_the_seed_lens_completes_it(self, root, history):
        write(root, "lens.md", seeds.lens(TEMPLATE, root.name))
        assert unmet(root, TEMPLATE, git=ScriptedGit(history)) == []

    @pytest.mark.parametrize(
        "history", [[], [("t1", OLD_TEMPLATE)]], ids=["no-owner", "unfilled-template"]
    )
    def test_with_no_scope_to_carry_an_existing_lens_is_left_as_it_is(self, root, history):
        write(root, "lens.md", "")
        assert unmet(root, TEMPLATE, git=ScriptedGit(history)) == []

    @pytest.mark.parametrize(
        "history", [[], [("t1", OLD_TEMPLATE)]], ids=["no-owner", "unfilled-template"]
    )
    def test_with_no_scope_to_carry_a_missing_lens_is_still_unmet(self, root, history):
        assert unmet(root, TEMPLATE, git=ScriptedGit(history)) == [
            "`lens.md` is missing: with no stated scope to carry, write the materials' seed lens"
        ]

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
        for delimiter in (OWNER_END, SEED_BEGIN, SEED_END):
            assert f"`{delimiter}`" in instructions

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

    def test_an_instance_that_never_stated_a_scope_completes_on_the_seed(self, history):
        history.commit(OLD_TEMPLATE, "initial instance")
        history.commit(ENGINE_CLAUDE_MD, "sync: machinery refresh")
        root = history.root
        assert NO_SCOPE in materials(root)
        write(root, "lens.md", seeds.lens(TEMPLATE, root.name))
        assert check(root) == []
