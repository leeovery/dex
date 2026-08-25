"""Tests for render/kernel.py: the markdown composition primitives."""

import pytest

from dex_engine.render import kernel
from dex_engine.render.kernel import (
    bold,
    bullet,
    code,
    detail,
    document,
    heading,
    inline,
    plural,
)

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

    def test_the_kernel_exports_no_width_no_wrap_no_elision(self):
        # The elision path is deleted, not bounded — a bound is a setting,
        # a deletion is a guarantee.
        assert not [name for name in dir(kernel) if "wrap" in name or "elide" in name]
        assert not [name for name in dir(kernel) if "width" in name.lower()]


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
