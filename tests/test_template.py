"""Tests for template.py and the template tree it locates.

The wheel's own layout is the contract — pyproject force-includes the tree's
`instance/` at `dex_engine/instance`, and a command that looked anywhere else
would refresh an instance's machinery from nothing.
"""

import re
from pathlib import Path

from dex_engine.template import bundled_template

# The repo's template tree — what the wheel bundles as dex_engine/instance.
TEMPLATE = Path(__file__).resolve().parent.parent / "instance"


class TestBundledTemplate:
    def test_is_the_instance_tree_inside_the_installed_package(self):
        found = Path(str(bundled_template()))
        assert found.name == "instance"
        assert found.parent.name == "dex_engine"


class TestClaudeMd:
    """Every instance carries this file unchanged, so it is written for all of them."""

    def lines(self) -> list[str]:
        return (TEMPLATE / "CLAUDE.md").read_text(encoding="utf-8").splitlines()

    def test_imports_the_contract_and_the_lens(self):
        # Whole lines: an `@path` inside a code span is only text, never an import.
        assert "@.claude/dex-contract.md" in self.lines()
        assert "@lens.md" in self.lines()

    def test_holds_no_placeholder_for_anyone_to_fill(self):
        assert [line for line in self.lines() if re.search(r"<[^<>]+>", line)] == []
