"""Tests for directive 1: the owner's CLAUDE.md, rehomed into lens.md and config."""

import json
from pathlib import Path

import pytest

from dex_engine import seeds
from dex_engine.directive import MATERIALS_BEGIN, MATERIALS_END
from dex_engine.directives import directive_1, discover
from dex_engine.directives.directive_1 import check, materials, unmet

TEMPLATE = Path(__file__).resolve().parents[2] / "instance"

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


class TestUnmet:
    def test_a_stated_lens_and_parsing_config_meet_every_condition(self, root):
        write(root, "lens.md", LENS)
        config(root, {"internal_domains": [], "discord": DISCORD})
        assert unmet(root, TEMPLATE) == []

    def test_a_missing_config_is_fine(self, root):
        write(root, "lens.md", LENS)
        assert unmet(root, TEMPLATE) == []

    def test_a_missing_lens_is_unmet(self, root):
        assert unmet(root, TEMPLATE) == ["`lens.md` is missing"]

    def test_the_unfilled_seed_is_unmet(self, root):
        write(root, "lens.md", seeds.lens(TEMPLATE, root.name))
        [finding] = unmet(root, TEMPLATE)
        assert finding.startswith("`lens.md` still holds the seed's placeholder text")

    def test_the_seed_placeholders_are_read_from_the_template_given(self, root, tmp_path):
        template = tmp_path / "other-template"
        template.mkdir()
        write(template, "lens.md", "# <instance name>\n\n<only this one>\n")
        write(root, "lens.md", "# dex-cooking\n\n<only this one>\n")
        assert unmet(root, template) == [
            "`lens.md` still holds the seed's placeholder text: `<only this one>`"
        ]

    def test_config_that_is_not_json_is_unmet(self, root):
        write(root, "lens.md", LENS)
        write(root, "state/config.json", '{"discord": {"guild": "1000",')
        [finding] = unmet(root, TEMPLATE)
        assert finding.startswith("`state/config.json` does not parse: ")
        assert "invalid JSON" in finding

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
        [finding] = unmet(root, TEMPLATE)
        assert finding.startswith("`state/config.json` does not parse: ")
        assert "discord" in finding

    def test_a_config_that_cannot_be_read_is_unmet(self, root):
        write(root, "lens.md", LENS)
        (root / "state" / "config.json").mkdir()
        [finding] = unmet(root, TEMPLATE)
        assert finding.startswith("`state/config.json` does not parse: ")

    def test_every_unmet_condition_is_named_lens_first(self, root):
        config(root, ["not", "an", "object"])
        path = root / "state" / "config.json"
        assert unmet(root, TEMPLATE) == [
            "`lens.md` is missing",
            f"`state/config.json` does not parse: {path}: expected a JSON object, got list",
        ]


class TestShipped:
    def directive(self):
        [shipped] = [directive for directive in discover() if directive.number == 1]
        return shipped

    def test_it_ships_as_directive_1_with_its_check_and_materials(self):
        directive = self.directive()
        assert directive.intent == directive_1.INTENT
        assert directive.check is check
        assert directive.materials is materials

    def test_its_instructions_name_where_the_materials_begin_and_end(self):
        instructions = self.directive().instructions
        assert MATERIALS_BEGIN.startswith("===== materials")
        assert "`===== materials`" in instructions
        assert f"`{MATERIALS_END}`" in instructions

    def test_the_engine_s_claude_md_says_what_the_instructions_know_it_by(self):
        # Step 1 tells every engine copy of CLAUDE.md from the owner's by
        # this statement, so the template must keep making it.
        instructions = " ".join(self.directive().instructions.split())
        claude_md = " ".join((TEMPLATE / "CLAUDE.md").read_text(encoding="utf-8").split())
        assert "the file is engine-owned and that `bin/dex sync` overwrites it" in instructions
        assert "This file is engine-owned: `bin/dex sync` overwrites it" in claude_md


class TestTheShippedFunctions:
    @pytest.fixture(autouse=True)
    def _the_tree_s_template(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The wheel bundles instance/ as dex_engine/instance; a source
        # checkout has it at the repo root.
        monkeypatch.setattr(directive_1, "bundled_template", lambda: TEMPLATE)

    def test_check_reads_the_running_engine_s_template(self, root):
        write(root, "lens.md", seeds.lens(TEMPLATE, root.name))
        assert check(root) == unmet(root, TEMPLATE)
        assert len(check(root)) == 1
        write(root, "lens.md", LENS)
        assert check(root) == []

    def test_the_materials_are_the_seed_lens_named_for_the_instance(self, root):
        assert materials(root) == seeds.lens(TEMPLATE, "dex-cooking")
        assert materials(root).startswith("# dex-cooking\n")
