"""Tests for frontmatter.py: the one unquoting rule and the one fence."""

import pytest

from dex_engine import frontmatter


class TestUnquote:
    @pytest.mark.parametrize(
        ("value", "scalar"),
        [
            ("high", "high"),  # bare: stands as written
            ('"high"', "high"),
            ("'high'", "high"),
            ('"2026-08-19"', "2026-08-19"),
            ('"Ep: one"', "Ep: one"),  # a colon inside the quotes
            ('"https://cdn.test/a.mp3?sig=1"', "https://cdn.test/a.mp3?sig=1"),
            ("'it''s'", "it's"),  # YAML's only single-quote escape
            ('"tabs\\tand\\nnewlines"', "tabs\tand\nnewlines"),
            ("", ""),
            ("'", "'"),  # one quote is not a quoted empty string
        ],
    )
    def test_the_scalar_the_writer_meant(self, value, scalar):
        assert frontmatter.unquote(value) == scalar

    def test_a_malformed_quote_stands_as_written(self):
        # A pointer beats a crash: the raw text still names what broke.
        assert frontmatter.unquote('"unterminated') == '"unterminated'


class TestBody:
    """Exact outputs: search snippets are built from this, so the edges are contract."""

    def test_no_frontmatter_is_all_body(self):
        assert frontmatter.body("# Title\n\nBody.\n") == "# Title\n\nBody.\n"

    def test_frontmatter_is_stripped_to_the_first_fence(self):
        assert frontmatter.body("---\ntopic: t\n---\n# Title\nBody.\n") == "# Title\nBody.\n"

    def test_a_later_rule_stays_in_the_body(self):
        text = "---\ntopic: t\n---\nAbove.\n\n---\n\nBelow.\n"
        assert frontmatter.body(text) == "Above.\n\n---\n\nBelow.\n"

    def test_an_unterminated_fence_returns_the_whole_page(self):
        text = "---\ntopic: t\nnever closed\n"
        assert frontmatter.body(text) == text

    def test_empty_frontmatter_still_strips(self):
        assert frontmatter.body("---\n---\nBody.\n") == "Body.\n"

    def test_only_leading_newlines_are_trimmed(self):
        assert (
            frontmatter.body("---\na: b\n---\n\n\n  indented\nX kept\n") == "  indented\nX kept\n"
        )
        assert frontmatter.body("---\na: b\n---\nXylophone\n") == "Xylophone\n"
