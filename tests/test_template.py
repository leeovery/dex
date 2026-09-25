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


# Every text a session runs commands from: the synced template, the directive
# instructions the engine prints, and the guides fetched raw by setup.
INSTRUCTIONS = [
    *sorted(TEMPLATE.rglob("*.md")),
    *sorted((TEMPLATE.parent / "src" / "dex_engine" / "directives").glob("directive_*.md")),
    *sorted((TEMPLATE.parent / "docs").glob("*.md")),
]
_FENCE_RE = re.compile(r"^\s*```[^\n]*\n(.*?)^\s*```", re.DOTALL | re.MULTILINE)
_SPAN_RE = re.compile(r"`([^`]+)`")
# A tool owners commonly alias to another program (diff to `git diff`, ls to
# eza, cat to bat, find to fd), in command position: first in the code, or
# after a pipe, a separator or `$(`.
_ALIASED_RE = re.compile(r"(?:^|[|;&(])\s*(diff|ls|cat|grep|egrep|find)\b", re.MULTILINE)


class TestCommandsSurviveAliases:
    """A command the instructions give is run past the shell's aliases of its name."""

    def test_the_scan_reaches_every_kind_of_instruction(self):
        names = {path.name for path in INSTRUCTIONS}
        assert {"ingest-item.md", "directive_2.md", "connect.md", "start.md"} <= names

    def test_no_commonly_aliased_tool_is_invoked_bare(self):
        bare = []
        for path in INSTRUCTIONS:
            text = path.read_text(encoding="utf-8")
            fenced = [m.group(1) for m in _FENCE_RE.finditer(text)]
            spans = _SPAN_RE.findall(_FENCE_RE.sub("", text))
            bare += [
                f"{path.name}: {m.group(0).strip()}"
                for code in (*fenced, *spans)
                for m in _ALIASED_RE.finditer(code)
            ]
        assert bare == []
