"""Tests for render/kernel.py: the column/wrap math."""

import pytest

from dex_engine.render import kernel
from dex_engine.render.kernel import (
    TreeNode,
    bold,
    bullet,
    code,
    detail,
    document,
    fill_to,
    heading,
    inline,
    kv_block,
    plural,
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

    def test_negative_hang_is_loud(self):
        # A negative hang would inflate the budget past the width — the one
        # overflow this module exists to prevent.
        with pytest.raises(ValueError, match="hang"):
            wrap_with_prefix("word " * 10, width=10, prefix="  ", hang=-4)


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

    @pytest.mark.parametrize("kwargs", [{"gap": -1}, {"indent": -2}])
    def test_negative_gap_or_indent_is_loud(self, kwargs):
        with pytest.raises(ValueError, match=">= 0"):
            table([["a", "b"]], **kwargs)


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

    def test_no_room_for_values_is_loud_and_names_the_key(self):
        with pytest.raises(ValueError, match=r"kv_block.*a-very-long-key"):
            kv_block([("a-very-long-key", "value")], width=10)

    def test_over_wide_key_with_empty_value_is_equally_loud(self):
        # An empty value must not smuggle an over-wide key column past the
        # width guarantee.
        with pytest.raises(ValueError, match=r"kv_block.*a-very-long-key"):
            kv_block([("a-very-long-key", "")], width=10)

    @pytest.mark.parametrize("kwargs", [{"gap": -1}, {"indent": -2}])
    def test_negative_gap_or_indent_is_loud(self, kwargs):
        with pytest.raises(ValueError, match=">= 0"):
            kv_block([("k", "v")], **kwargs)


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

    def test_negative_indent_is_loud(self):
        with pytest.raises(ValueError, match=">= 0"):
            tree([TreeNode(title="a")], indent=-1)


def test_default_width_is_a_sane_terminal_width():
    assert kernel.DEFAULT_WIDTH == 72



LONG_ID = "2026-08-19-laravel-read-through-filesystem-migrations-with-real-drivers-14bf49"
LONG_URL = "https://example.test/watch?v=abcdefghijk&" + "&".join(
    f"utm_source=partner-{n}&utm_campaign=very-long-campaign-name-{n}" for n in range(6)
)


class TestIdentityIsNeverShortened:
    """The whole point of the rewrite: nothing here may shorten a value."""

    @pytest.mark.parametrize("value", [LONG_ID, LONG_URL])
    @pytest.mark.parametrize("shape", [bold, code, heading, bullet, detail])
    def test_every_primitive_emits_the_value_whole(self, shape, value):
        out = shape(value)
        assert value in out
        assert "…" not in out
        assert len(out.splitlines()) == 1


class TestFragments:
    def test_bold_wraps_in_asterisks(self):
        assert bold("id") == "**id**"

    def test_code_wraps_in_backticks(self):
        assert code("manual") == "`manual`"

    def test_heading_defaults_to_the_report_level(self):
        assert heading("Enrich run — 6 units processed") == "## Enrich run — 6 units processed"

    def test_heading_takes_a_section_level(self):
        assert heading("Needs you", level=3) == "### Needs you"

    @pytest.mark.parametrize("level", [0, 7])
    def test_heading_refuses_a_level_outside_markdown(self, level):
        with pytest.raises(ValueError, match="level"):
            heading("x", level=level)

    def test_bullet_indents_two_columns_per_depth(self):
        assert bullet("a") == "- a"
        assert bullet("a", depth=1) == "  - a"
        assert bullet("a", depth=2) == "    - a"

    def test_detail_hangs_under_its_entry(self):
        assert detail("why") == f"  {kernel.DETAIL_GLYPH} why"
        assert detail("why", depth=1) == f"    {kernel.DETAIL_GLYPH} why"

    @pytest.mark.parametrize("shape", [bullet, detail])
    def test_negative_depth_is_loud(self, shape):
        with pytest.raises(ValueError, match="depth"):
            shape("a", depth=-1)

    @pytest.mark.parametrize("shape", [bold, code, heading, bullet, detail])
    @pytest.mark.parametrize("bad", ["", "two\nlines"])
    def test_a_multiline_or_empty_fragment_is_loud(self, shape, bad):
        with pytest.raises(ValueError, match="single line"):
            shape(bad)


class TestInline:
    def test_joins_with_the_middle_dot(self):
        assert inline(["**done** 4", "**waiting** 1"]) == "**done** 4 · **waiting** 1"

    def test_empty_parts_drop_out(self):
        assert inline(["a", "", "b"]) == "a · b"

    def test_takes_a_generator(self):
        assert inline(f"{n}" for n in range(3)) == "0 · 1 · 2"

    def test_nothing_at_all_is_loud(self):
        with pytest.raises(ValueError, match="non-empty part"):
            inline(["", ""])


class TestPlural:
    def test_one_takes_the_singular(self):
        assert plural(1, "unit") == "1 unit"

    def test_zero_and_many_take_the_plural(self):
        assert plural(0, "unit") == "0 units"
        assert plural(6, "unit") == "6 units"

    def test_an_irregular_plural_is_given(self):
        assert plural(2, "entry", "entries") == "2 entries"


class TestDocument:
    def test_joins_blocks_and_terminates_with_a_newline(self):
        assert document(["## a", "", "- b"]) == "## a\n\n- b\n"

    def test_runs_of_blank_lines_collapse(self):
        assert document(["a", "", "", "", "b"]) == "a\n\nb\n"

    def test_it_neither_opens_nor_closes_on_a_blank(self):
        assert document(["", "", "a", "", ""]) == "a\n"

    def test_multiline_blocks_come_apart_into_lines(self):
        assert document(["- a\n  ↳ b", "", "- c"]) == "- a\n  ↳ b\n\n- c\n"

    def test_trailing_whitespace_is_stripped_from_every_line(self):
        assert document(["a   ", "  ", "b\t"]) == "a\n\nb\n"

    def test_nothing_renders_as_nothing(self):
        assert document([]) == ""
        assert document(["", ""]) == ""
