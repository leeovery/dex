"""Tests for frontmatter.py: the one unquoting rule every reader shares."""

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
