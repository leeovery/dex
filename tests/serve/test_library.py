"""Tests for serve/library.py's own seams — the ones a session-level call underspecifies."""

from dex_engine.serve.library import _page_body


class TestPageBody:
    """Exact outputs: snippets are built from this, so the edges are contract."""

    def test_no_frontmatter_is_all_body(self):
        assert _page_body("# Title\n\nBody.\n") == "# Title\n\nBody.\n"

    def test_frontmatter_is_stripped_to_the_first_fence(self):
        assert _page_body("---\ntopic: t\n---\n# Title\nBody.\n") == "# Title\nBody.\n"

    def test_a_later_rule_stays_in_the_body(self):
        text = "---\ntopic: t\n---\nAbove.\n\n---\n\nBelow.\n"
        assert _page_body(text) == "Above.\n\n---\n\nBelow.\n"

    def test_an_unterminated_fence_returns_the_whole_page(self):
        text = "---\ntopic: t\nnever closed\n"
        assert _page_body(text) == text

    def test_empty_frontmatter_still_strips(self):
        assert _page_body("---\n---\nBody.\n") == "Body.\n"

    def test_only_leading_newlines_are_trimmed(self):
        assert _page_body("---\na: b\n---\n\n\n  indented\nX kept\n") == "  indented\nX kept\n"
        assert _page_body("---\na: b\n---\nXylophone\n") == "Xylophone\n"
