"""Tests for serve/library.py's own seams — the ones a session-level call underspecifies."""

from dex_engine.serve.library import _wikilinks


class TestWikilinks:
    """A page's outbound links: what `page` hands back for the caller to resolve."""

    def test_links_come_back_in_the_order_the_page_makes_them(self):
        assert _wikilinks("See [[pour-over]] then [[grinders]].") == ["pour-over", "grinders"]

    def test_a_page_with_no_links_has_none(self):
        assert _wikilinks("# Pour-over\n\nProse with no links at all.\n") == []

    def test_a_name_linked_twice_is_listed_once(self):
        assert _wikilinks("[[pour-over]] and again [[pour-over]]") == ["pour-over"]

    def test_an_alias_resolves_by_its_name(self):
        # The alias is display text for a human; the name is what names a file.
        assert _wikilinks("[[pour-over|the pour-over page]]") == ["pour-over"]

    def test_surrounding_whitespace_is_not_part_of_the_name(self):
        assert _wikilinks("[[ pour-over ]]") == ["pour-over"]

    def test_an_empty_link_is_not_a_link(self):
        assert _wikilinks("[[]] and [[   ]] and [[|alias]]") == []

    def test_a_single_bracket_is_a_markdown_link_not_a_wikilink(self):
        assert _wikilinks("[pour-over](wiki/topics/pour-over.md)") == []
