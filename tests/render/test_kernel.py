"""Tests for render/kernel.py: the column/wrap math."""

import pytest

from dex_engine.render import kernel
from dex_engine.render.kernel import (
    TreeNode,
    fill_to,
    kv_block,
    table,
    tree,
    wrap,
    wrap_with_prefix,
)


class TestFillTo:
    def test_fills_the_deficit(self):
        assert fill_to("── head ", "─", 12) == "── head ────"

    def test_never_truncates(self):
        assert fill_to("longer than width", "─", 5) == "longer than width"

    def test_exact_width_unchanged(self):
        assert fill_to("12345", "·", 5) == "12345"

    def test_multichar_fill_rejected(self):
        with pytest.raises(ValueError, match="single character"):
            fill_to("x", "──", 10)


class TestWrap:
    def test_simple_wrap(self):
        assert wrap("aa bb cc dd", 5) == ["aa bb", "cc dd"]

    def test_exact_fit_stays_on_one_line(self):
        assert wrap("aa bb", 5) == ["aa bb"]

    def test_budget_is_never_exceeded(self):
        for budget in range(1, 12):
            for seg in wrap("the quick brown fox jumps over supercalifragilistic", budget):
                assert len(seg) <= budget

    def test_oversized_token_is_hard_split(self):
        assert wrap("abcdefgh", 3) == ["abc", "def", "gh"]

    def test_hard_split_mid_line(self):
        assert wrap("aa abcdefgh", 4) == ["aa", "abcd", "efgh"]

    def test_whitespace_collapses(self):
        assert wrap("a   b\t c", 10) == ["a b c"]

    def test_empty_text_yields_one_empty_segment(self):
        assert wrap("", 10) == [""]

    def test_nonpositive_budget_is_loud(self):
        with pytest.raises(ValueError, match="budget"):
            wrap("x", 0)


class TestWrapWithPrefix:
    def test_prefix_on_every_line(self):
        assert wrap_with_prefix("aa bb cc", width=8, prefix="> ") == ["> aa bb", "> cc"]

    def test_the_budget_subtracts_the_prefix_never_the_bare_width(self):
        # THE budget bug: wrapping to the bare width would emit 12-col lines.
        for line in wrap_with_prefix("word " * 20, width=12, prefix="    "):
            assert len(line) <= 12

    def test_hang_indents_continuations_under_the_text(self):
        lines = wrap_with_prefix("- aa bb cc", width=8, prefix="  ", hang=2)
        assert lines[0] == "  - aa"
        assert lines[1:] == ["    bb", "    cc"]

    def test_no_room_is_loud(self):
        with pytest.raises(ValueError, match="room"):
            wrap_with_prefix("x", width=4, prefix="    ", hang=1)


class TestTable:
    def test_columns_align_to_the_widest_cell(self):
        out = table([["done", "4"], ["blocked", "12"]])
        assert out == "done     4\nblocked  12\n"

    def test_right_alignment(self):
        out = table([["done", "4"], ["blocked", "12"]], aligns=["l", "r"])
        assert out == "done      4\nblocked  12\n"

    def test_header_gets_a_rule(self):
        out = table([["a", "bb"]], header=["col1", "c2"])
        assert out.splitlines() == ["col1  c2", "────  ──", "a     bb"]

    def test_indent_applies_to_every_line(self):
        out = table([["a", "b"]], indent=2)
        assert out == "  a  b\n"

    def test_no_trailing_spaces(self):
        out = table([["a", "b"], ["wider", "x"]])
        for line in out.splitlines():
            assert line == line.rstrip()

    def test_empty_rows_are_loud(self):
        with pytest.raises(ValueError, match="non-empty"):
            table([])

    def test_ragged_rows_are_loud(self):
        with pytest.raises(ValueError, match="ragged"):
            table([["a", "b"], ["c"]])

    def test_multiline_cells_are_loud(self):
        with pytest.raises(ValueError, match="single-line"):
            table([["a\nb"]])

    def test_bad_aligns_are_loud(self):
        with pytest.raises(ValueError, match="aligns"):
            table([["a", "b"]], aligns=["l", "c"])


class TestKvBlock:
    def test_keys_pad_to_one_shared_column(self):
        out = kv_block([("kind", "web"), ("status", "done")])
        assert out == "kind    web\nstatus  done\n"

    def test_values_wrap_and_continuations_align_under_the_value_column(self):
        out = kv_block([("key", "aa bb cc")], width=9)
        assert out.splitlines() == ["key  aa", "     bb", "     cc"]

    def test_empty_value_renders_the_bare_key(self):
        assert kv_block([("key", "")]) == "key\n"

    def test_indent(self):
        assert kv_block([("k", "v")], indent=2) == "  k  v\n"

    def test_empty_pairs_are_loud(self):
        with pytest.raises(ValueError, match="non-empty"):
            kv_block([])

    def test_no_room_for_values_is_loud(self):
        with pytest.raises(ValueError, match="budget"):
            kv_block([("a-very-long-key", "value")], width=10)


class TestTree:
    def test_sibling_glyphs(self):
        out = tree([TreeNode(title="a"), TreeNode(title="b")])
        assert out == "├─ a\n└─ b\n"

    def test_gutter_runs_through_children(self):
        out = tree(
            [
                TreeNode(title="a", children=[TreeNode(title="a1"), TreeNode(title="a2")]),
                TreeNode(title="b", children=[TreeNode(title="b1")]),
            ]
        )
        assert out.splitlines() == [
            "├─ a",
            "│  ├─ a1",
            "│  └─ a2",
            "└─ b",
            "   └─ b1",
        ]

    def test_indent(self):
        assert tree([TreeNode(title="a")], indent=2) == "  └─ a\n"

    def test_empty_nodes_are_loud(self):
        with pytest.raises(ValueError, match="non-empty"):
            tree([])

    def test_node_titles_must_be_single_line(self):
        with pytest.raises(ValueError, match="title"):
            TreeNode(title="a\nb")


def test_default_width_is_a_sane_terminal_width():
    assert kernel.DEFAULT_WIDTH == 72
