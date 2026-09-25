"""Tests for serve/steering.py: the prose that keeps an impatient caller probing.

Prose is not asserted word for word. What is contract here is that each slot
says its own thing — the doctrine in the instructions, the next move in a
footer, and neither in the other — that the roster carries every instance's
lens untouched, that the routing states the capture rule whole, and that the
procedure is the bundled skill itself rather than a copy of it.
"""

import pytest

from dex_engine.serve import steering
from dex_engine.serve.roster import build_roster

from .conftest import BOOKS, COFFEE, LENS, TEMPLATE


def block(instructions: str, name: str) -> str:
    """What the instructions say about one instance, inside its own delimiter."""
    opened = f'<instance name="{name}">\n'
    start = instructions.index(opened) + len(opened)
    return instructions[start : instructions.index("\n</instance>", start)]


@pytest.fixture
def instructions(roster) -> str:
    return steering.instructions(roster)


class TestDoctrine:
    def test_says_hits_are_probes_and_not_an_answer(self, instructions):
        assert "raw hits" in instructions
        assert "never a ranked answer" in instructions

    def test_names_the_maps_as_the_opening_move(self, instructions):
        assert "What does this instance hold?" in instructions
        assert "topics(instance)" in instructions
        assert "entities(instance)" in instructions
        assert "graph(instance)" in instructions

    def test_says_to_follow_wikilinks(self, instructions):
        assert "[[wikilinks]]" in instructions

    def test_says_to_keep_probing(self, instructions):
        assert "Reformulate and search again" in instructions
        assert "nothing relevant is left unexplored" in instructions

    def test_says_to_answer_only_from_what_was_fetched_and_cite_it(self, instructions):
        assert "Answer only from material you actually fetched" in instructions
        assert "cite the item ids" in instructions

    def test_nudges_toward_dex_before_general_knowledge(self, instructions):
        assert "search those instances before answering" in instructions
        assert "from general knowledge" in instructions

    def test_routes_a_question_to_the_lenses_it_touches(self, instructions):
        assert "When a question touches the lens of one or more of those instances" in (
            instructions
        )

    def test_never_says_scope(self, instructions):
        # An instance's lens is the angle it reads through, never a door
        # a share has to fit through.
        assert "scope" not in instructions.lower()


class TestCaptureRouting:
    """Where a capture goes is the owner's word, never the content's."""

    def test_the_instance_the_owner_names(self, instructions):
        assert '("save this to my dex"), capture it into the instance the owner names.' in (
            instructions
        )

    def test_a_single_instance_server_captures_without_asking(self, instructions):
        assert "When this server serves a single instance, capture there without asking." in (
            instructions
        )

    def test_otherwise_ask_which_instance_or_which_ones(self, instructions):
        assert (
            "Otherwise ask the owner which instance, or which ones, before saving" in instructions
        )

    def test_never_chosen_from_the_content(self, instructions):
        assert (
            "never choose from the content: any link can belong in any instance, depending on "
            "the lens the owner wants it read through."
        ) in instructions

    def test_the_rule_is_stated_in_that_order(self, instructions):
        named = instructions.index("the instance the owner names")
        single = instructions.index("serves a single instance")
        ask = instructions.index("Otherwise ask the owner")
        assert named < single < ask


class TestRoster:
    def test_an_instance_states_its_lens_verbatim(self, instructions):
        assert block(instructions, COFFEE) == LENS.strip()

    def test_the_lens_is_read_from_lens_md_and_never_claude_md(self, roots):
        (roots[0] / "CLAUDE.md").write_text("# identity only\n", encoding="utf-8")
        said = block(steering.instructions(build_roster(roots)), COFFEE)
        assert said == LENS.strip()

    def test_an_instance_with_no_lens_md_is_undeclared_in_one_line(self, instructions):
        said = block(instructions, BOOKS)
        assert "\n" not in said
        assert said == (
            "This instance has no readable lens.md, so its lens is undeclared: search it "
            "whenever a question might touch it."
        )

    def test_an_unreadable_lens_md_reads_as_undeclared(self, roots):
        # A server that refused to start over one unreadable file would take
        # every other instance down with it.
        (roots[0] / "lens.md").write_bytes(b"\xff\xfe not text")
        said = block(steering.instructions(build_roster(roots)), COFFEE)
        assert "its lens is undeclared" in said

    def test_an_empty_lens_md_reads_as_undeclared(self, roots):
        (roots[0] / "lens.md").write_text("  \n\n", encoding="utf-8")
        said = block(steering.instructions(build_roster(roots)), COFFEE)
        assert "its lens is undeclared" in said

    def test_each_instance_is_its_own_block(self, instructions):
        assert f'</instance>\n<instance name="{BOOKS}">' in instructions

    def test_the_roster_is_fenced_off_from_the_prose_around_it(self, instructions):
        # Markdown blocks: run the roster into the doctrine or the nudge and
        # the owner's own words stop reading as the owner's own words.
        assert "\n\nWhat each instance reads for, in the owner's own words:" in instructions
        assert "</instance>\n\nWhen a question touches the lens" in instructions

    def test_instances_come_in_roster_order(self, instructions):
        assert instructions.index(f'name="{COFFEE}"') < instructions.index(f'name="{BOOKS}"')

    def test_every_served_instance_is_named(self, instructions):
        assert instructions.count("<instance name=") == 2


class TestNextMove:
    def test_hits_are_told_what_to_open_and_to_search_again(self):
        said = steering.search_next(shown=4, total=4)
        assert "fetch" in said
        assert "search again" in said

    def test_no_hits_are_told_to_reformulate_or_look_elsewhere(self):
        said = steering.search_next(shown=0, total=0)
        assert "wording" in said
        assert "another instance" in said

    def test_a_cut_list_says_what_was_cut_and_to_narrow_the_term(self):
        said = steering.search_next(shown=25, total=1197)
        assert "newest 25 of 1197 matches" in said
        assert "narrow it" in said

    def test_the_last_hit_before_the_cap_is_not_a_cut_list(self):
        assert steering.search_next(shown=25, total=25) == steering.search_next(shown=1, total=1)

    def test_a_footer_is_one_line(self):
        for shown, total in ((4, 4), (0, 0), (25, 1197)):
            assert "\n" not in steering.search_next(shown=shown, total=total)

    def test_a_page_is_pointed_at_its_links_and_its_items(self):
        assert "page(name, instance)" in steering.PAGE_NEXT
        assert "fetch" in steering.PAGE_NEXT
        assert "\n" not in steering.PAGE_NEXT

    def test_a_map_read_is_pointed_back_at_pages_and_search(self):
        whole = steering.graph_next(shown=4, total=4)
        assert "page(name, instance)" in steering.TOPICS_NEXT
        assert "search" in steering.TOPICS_NEXT
        assert "search" in steering.ENTITIES_NEXT
        assert "wikilink" in whole
        assert "shared-items" in whole
        for said in (steering.TOPICS_NEXT, steering.ENTITIES_NEXT, whole):
            assert "\n" not in said

    def test_a_trimmed_graph_states_the_cut_and_the_knobs(self):
        said = steering.graph_next(shown=2000, total=14142)
        assert "2000 of the map's 14142 edges" in said
        assert "around" in said
        assert "min_weight" in said
        assert "full" in said
        assert "\n" not in said

    def test_a_graph_served_whole_is_not_reported_as_trimmed(self):
        assert steering.graph_next(shown=7, total=7) == steering.graph_next(shown=1, total=1)
        assert "Showing" not in steering.graph_next(shown=7, total=7)

    def test_a_footer_does_not_restate_the_doctrine(self, instructions):
        # Every result carries one; the connection carries the doctrine once.
        for said in (
            steering.search_next(shown=4, total=4),
            steering.PAGE_NEXT,
            steering.TOPICS_NEXT,
            steering.ENTITIES_NEXT,
            steering.graph_next(shown=4, total=4),
            steering.graph_next(shown=1, total=9),
        ):
            assert said not in instructions


class TestProcedure:
    def test_is_the_bundled_skill_body(self):
        skill = (TEMPLATE / "skills" / "dex-query" / "SKILL.md").read_text(encoding="utf-8")
        assert steering.procedure(TEMPLATE) == skill.split("---\n", 2)[2].lstrip("\n")

    def test_the_skill_frontmatter_is_not_served(self):
        assert "name: dex-query" not in steering.procedure(TEMPLATE)
