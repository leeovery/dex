"""Tests for serve/server.py: the tools, the maps, the steering, over a real session."""

import datetime
import json
from pathlib import Path

from dex_engine.serve.library import _EDGE_CAP, _HITS_PER_INSTANCE
from dex_engine.serve.roster import build_roster
from dex_engine.serve.server import build_server
from dex_engine.version import engine_version

from .conftest import (
    BOOKS,
    BORGES,
    COFFEE,
    GRINDER,
    SCOPE,
    TEMPLATE,
    V60,
    call,
    drive,
    hits,
    record,
    refusal,
    write,
)


async def _identity(client):
    return client.server_info


async def _instructions(client):
    return client.instructions


def flood(root: Path, count: int, word: str) -> list[str]:
    """`count` items in one instance's corpus, each holding `word`, a day apart.

    Returns the ids it wrote, oldest first — which is the order a search must
    hand back reversed, and the end of it a cap must be the part that survives.
    """
    written = []
    for n in range(count):
        day = datetime.date(2026, 6, 1) + datetime.timedelta(days=n)
        item_id = f"{day}-flood-{n:06d}"
        write(
            root / "corpus" / "2026" / f"{item_id}.md",
            f"---\nid: {item_id}\nsource: inbox\nchannel: inbox\nshared_by: Nat\n"
            f"date: {day}\nkinds: [text]\nstatus: raw\nenrichment: []\n---\n\n"
            f"A note about {word}.\n",
        )
        written.append(item_id)
    return written


def described(server, name: str) -> str:
    """One tool's description — what the client holds in context on every call."""
    listed = drive(server, lambda client: client.list_tools())
    return next(tool.description for tool in listed.tools if tool.name == name)


class TestTools:
    def test_the_surface_is_six_reads_and_one_capture(self, server):
        listed = drive(server, lambda client: client.list_tools())
        assert [tool.name for tool in listed.tools] == [
            "search",
            "fetch",
            "page",
            "topics",
            "entities",
            "graph",
            "capture",
        ]
        assert all(tool.description for tool in listed.tools)

    def test_the_server_names_itself_and_the_engine_it_runs(self, server):
        info = drive(server, _identity)
        assert (info.name, info.version) == ("dex", engine_version())

    def test_search_says_a_hit_is_a_probe(self, server):
        said = described(server, "search")
        assert "rarely the answer" in said
        assert "reformulate and call again" in said

    def test_fetch_says_it_is_where_a_probe_ends(self, server):
        assert "Where a probe ends" in described(server, "fetch")

    def test_page_says_to_keep_following_it_outward(self, server):
        assert "keep following it outward" in described(server, "page")

    def test_capture_says_it_writes_a_suggestion(self, server):
        assert "A suggestion, not a corpus entry" in described(server, "capture")

    def test_topics_says_it_is_the_opening_move(self, server):
        assert "opening move" in described(server, "topics")

    def test_entities_says_aliases_are_search_terms(self, server):
        assert "`search` terms" in described(server, "entities")

    def test_graph_says_what_each_edge_type_means(self, server):
        said = described(server, "graph")
        assert "wikilink" in said
        assert "shared-items" in said

    def test_graph_describes_the_widening_parameters(self, server):
        # The docstring is the description the client sees, so the three
        # parameters that open a trimmed view have to be stated there.
        said = described(server, "graph")
        assert "around" in said
        assert "min_weight" in said
        assert "full" in said


class TestInstructions:
    def test_the_doctrine_reaches_the_client_at_connect_time(self, server):
        said = drive(server, _instructions)
        assert "never a ranked answer" in said
        assert "cite the item ids" in said

    def test_every_instance_declares_its_own_scope(self, server):
        said = drive(server, _instructions)
        assert SCOPE.strip() in said
        assert f'<instance name="{BOOKS}">' in said


class TestPrompt:
    def test_the_procedure_is_offered_by_name(self, server):
        listed = drive(server, lambda client: client.list_prompts())
        assert [(prompt.name, prompt.title) for prompt in listed.prompts] == [
            ("dex-query", "Query dex")
        ]
        assert all(prompt.description for prompt in listed.prompts)

    def test_the_procedure_is_the_bundled_skill_itself(self, server):
        # Served from the template, never copied: an edit to the skill has to
        # reach this surface with no code change at all.
        got = drive(server, lambda client: client.get_prompt("dex-query"))
        skill = (TEMPLATE / "skills" / "dex-query" / "SKILL.md").read_text(encoding="utf-8")
        assert got.messages[0].content.text == skill.split("---\n", 2)[2].lstrip("\n")


class TestSearch:
    def test_hits_travel_with_the_move_that_follows_them(self, server):
        found = record(server, "search", {"query": "V60"})
        assert found["hits"]
        assert found["total"] == len(found["hits"])
        assert "fetch the promising ids" in found["next"]
        assert "search again" in found["next"]

    def test_no_hits_travel_with_the_move_that_follows_none(self, server):
        found = record(server, "search", {"query": "kombucha"})
        assert found["hits"] == []
        assert found["total"] == 0
        assert "another instance" in found["next"]

    def test_a_flood_is_cut_to_the_newest_and_says_it_was(self, server, roots):
        # A broad word on a real instance matches four figures of items, which
        # is not something a chat model can be handed.
        wrote = flood(roots[0], _HITS_PER_INSTANCE + 5, "chartreuse")
        found = record(server, "search", {"query": "chartreuse"})
        assert [hit["id"] for hit in found["hits"]] == [
            f"{COFFEE}/{item_id}" for item_id in reversed(wrote[-_HITS_PER_INSTANCE:])
        ]
        assert found["total"] == _HITS_PER_INSTANCE + 5
        assert f"newest {_HITS_PER_INSTANCE} of {_HITS_PER_INSTANCE + 5} matches" in found["next"]
        assert "narrow it" in found["next"]

    def test_the_cap_is_per_instance_so_a_fan_out_still_reaches_the_rest(self, server, roots):
        flood(roots[0], _HITS_PER_INSTANCE + 5, "chartreuse")
        flood(roots[1], 3, "chartreuse")
        found = record(server, "search", {"query": "chartreuse"})
        assert len(found["hits"]) == _HITS_PER_INSTANCE + 3
        assert found["total"] == _HITS_PER_INSTANCE + 8
        assert [hit["instance"] for hit in found["hits"][-3:]] == [BOOKS] * 3

    def test_a_search_inside_the_cap_is_not_reported_as_cut(self, server, roots):
        flood(roots[0], _HITS_PER_INSTANCE, "chartreuse")
        found = record(server, "search", {"query": "chartreuse"})
        assert found["total"] == len(found["hits"]) == _HITS_PER_INSTANCE
        assert "newest" not in found["next"]

    def test_matches_the_digest_behind_an_item(self, server):
        (hit,) = hits(server, {"query": "swirl instead of stir"})
        assert hit["id"] == f"{COFFEE}/{V60}"
        assert hit["type"] == "item"
        assert hit["title"] == "james hoffmann v60 technique"
        assert hit["date"] == "2026-01-15"
        assert hit["url"] == "https://www.youtube.com/watch?v=example"
        assert hit["instance"] == COFFEE
        assert "swirl instead of stir" in hit["snippet"]

    def test_matches_a_corpus_note(self, server):
        (hit,) = hits(server, {"query": "everyone cites"})
        assert hit["id"] == f"{COFFEE}/{V60}"
        assert "The V60 technique video everyone cites." in hit["snippet"]

    def test_one_row_per_item_when_digest_and_note_both_match(self, server):
        # The digest and the note are two halves of one fetch, so a match in
        # either is the same hit — and the digest's fact body is the half shown.
        found = hits(server, {"query": "V60"})
        assert [hit["id"] for hit in found] == [f"{COFFEE}/{V60}", f"{COFFEE}/pour-over"]
        assert "Hoffmann V60 method" in found[0]["snippet"]
        assert "signal:" not in found[0]["snippet"]

    def test_digest_frontmatter_can_neither_hit_nor_snippet(self, server, roots):
        # A digest's `topics:` is the classification made at digest time —
        # candidate names allowed, never rewritten as the taxonomy moves — so
        # a name living only there must stay unfindable, while the fact body
        # stays a real search surface.
        item_id = "2026-04-10-aeropress-inversion-d4e5f6"
        write(
            roots[0] / "corpus" / "2026" / f"{item_id}.md",
            f"---\nid: {item_id}\nsource: inbox\nchannel: inbox\nshared_by: Lee\n"
            "date: 2026-04-10\nkinds: [text]\nstatus: raw\nenrichment: []\n---\n\n"
            "A brewing note.\n",
        )
        write(
            roots[0] / "state" / "digests" / f"{item_id}.md",
            f"---\nid: {item_id}\ndate: 2026-04-10\nsignal: low\n"
            "topics: [percolation-theory]\n---\n- Inverted, ninety-second steep.\n",
        )
        assert hits(server, {"query": "percolation-theory"}) == []
        (hit,) = hits(server, {"query": "ninety-second steep"})
        assert hit["id"] == f"{COFFEE}/{item_id}"

    def test_a_snippet_is_one_line_around_the_match(self, server):
        (hit,) = hits(server, {"query": "swirl instead of stir"})
        assert hit["snippet"].startswith("…")
        assert "\n" not in hit["snippet"]
        assert hit["snippet"].endswith("swirl instead of stir.")

    def test_a_snippet_only_marks_the_ends_it_cut(self, server):
        # This one runs off the end of the window but starts at byte zero.
        (hit,) = hits(server, {"query": "id: 2026-01-15"})
        assert hit["snippet"].startswith("--- id: 2026-01-15")
        assert hit["snippet"].endswith("…")

    def test_an_item_that_does_not_parse_is_skipped(self, server, roots):
        broken = roots[0] / "corpus" / "2026" / "2026-01-01-broken-000000.md"
        broken.write_text("this file has no frontmatter at all")
        (hit,) = hits(server, {"query": "Burr alignment"})
        assert hit["id"] == f"{COFFEE}/{GRINDER}"

    def test_an_earlier_item_missing_does_not_stop_the_scan(self, server):
        (hit,) = hits(server, {"query": "Burr alignment"})
        assert hit["id"] == f"{COFFEE}/{GRINDER}"

    def test_a_page_with_no_heading_is_titled_by_its_name(self, server, roots):
        (roots[0] / "wiki" / "topics" / "decaf.md").write_text("Swiss water process only.\n")
        (hit,) = hits(server, {"query": "Swiss water"})
        assert (hit["id"], hit["title"]) == (f"{COFFEE}/decaf", "decaf")

    def test_a_frontmatter_id_never_becomes_a_path(self, server, roots, tmp_path):
        # Frontmatter is authored text; the filename is what keys an item.
        (tmp_path / "secret.md").write_text("chartreuse")
        (roots[0] / "corpus" / "2026" / "2026-05-05-stray-000000.md").write_text(
            "---\nid: ../../../secret\nsource: inbox\nchannel: inbox\nshared_by: Lee\n"
            "date: 2026-05-05\nkinds: [text]\nstatus: raw\nenrichment: []\n---\n\nA note.\n"
        )
        assert hits(server, {"query": "chartreuse"}) == []

    def test_is_case_insensitive(self, server):
        assert hits(server, {"query": "HOFFMANN"})

    def test_a_miss_is_no_rows(self, server):
        assert hits(server, {"query": "kombucha"}) == []

    def test_wiki_pages_are_their_own_rows(self, server):
        (hit,) = hits(server, {"query": "post-bloom"})
        assert hit["id"] == f"{COFFEE}/pour-over"
        assert hit["type"] == "page"
        assert hit["title"] == "Pour-over"
        assert hit["date"] is None
        assert hit["url"] is None

    def test_page_frontmatter_is_not_searched(self, server):
        # `generated:` lives only in page frontmatter — bookkeeping, not content.
        assert hits(server, {"query": "generated"}) == []

    def test_a_page_snippet_never_drags_frontmatter_in(self, server):
        # The match sits at the very top of the body, where the lead window
        # would otherwise reach back into the frontmatter fence.
        (hit,) = hits(server, {"query": "starting recipe"})
        assert hit["snippet"].startswith("# Pour-over")
        assert "items: 1" not in hit["snippet"]

    def test_a_horizontal_rule_in_a_body_is_not_a_fence(self, server, roots):
        write(
            roots[0] / "wiki" / "topics" / "ruled.md",
            "---\ntopic: ruled\n---\nAbove the rule.\n\n---\n\nBelow the rule.\n",
        )
        (hit,) = hits(server, {"query": "Below the rule"})
        assert hit["id"] == f"{COFFEE}/ruled"

    def test_dated_rows_come_newest_first(self, server):
        # `shared_by: Lee` is frontmatter both coffee items carry and neither
        # book does — the corpus scan reads the whole file, frontmatter included.
        found = hits(server, {"query": "Lee"})
        assert [hit["id"] for hit in found] == [f"{COFFEE}/{GRINDER}", f"{COFFEE}/{V60}"]

    def test_undated_rows_come_after_dated_ones(self, server):
        found = hits(server, {"query": "stir"})
        assert [hit["type"] for hit in found] == ["item", "page"]

    def test_fans_out_across_instances_in_roster_order(self, server):
        # Coffee answers twice — a digest, and the rendered index carrying a
        # topic description — and both still come before every books hit.
        found = hits(server, {"query": "method"})
        assert [hit["instance"] for hit in found] == [COFFEE, COFFEE, BOOKS]

    def test_naming_an_instance_restricts_to_it(self, server):
        found = hits(server, {"query": "method", "instance": BOOKS})
        assert [hit["id"] for hit in found] == [f"{BOOKS}/{BORGES}"]

    def test_an_unknown_instance_names_the_served_ones(self, server):
        message = refusal(server, "search", {"query": "method", "instance": "dex-nope"})
        assert "no instance named 'dex-nope'" in message
        assert f"{COFFEE}, {BOOKS}" in message

    def test_an_empty_query_is_refused(self, server):
        assert "search needs a query" in refusal(server, "search", {"query": "   "})

    def test_a_query_is_plain_text_not_a_pattern(self, server):
        assert hits(server, {"query": "60g/L"})
        assert hits(server, {"query": ".*"}) == []


class TestFetch:
    def test_returns_the_digest_the_note_and_the_provenance(self, server):
        item = record(server, "fetch", {"id": f"{COFFEE}/{V60}"})
        assert item["id"] == f"{COFFEE}/{V60}"
        assert item["instance"] == COFFEE
        assert item["title"] == "james hoffmann v60 technique"
        assert item["date"] == "2026-01-15"
        assert item["shared_by"] == "Lee"
        assert item["urls"] == ["https://www.youtube.com/watch?v=example"]
        assert item["note"] == "**Lee** (2026-01-15):\nThe V60 technique video everyone cites."
        assert item["signal"] == "high"
        assert item["digest"] == (
            "- The Hoffmann V60 method (2026-01): 60g/L dose, ~3:00 total brew, "
            "swirl instead of stir."
        )

    def test_digest_frontmatter_never_reaches_the_caller(self, server):
        # `topics:`/`entities:` in a digest are the classification made at
        # digest time — stale by design once the taxonomy moves — so only
        # the fact body and the lifted `signal` go out.
        item = record(server, "fetch", {"id": f"{COFFEE}/{V60}"})
        assert "topics:" not in item["digest"]
        assert "signal:" not in item["digest"]

    def test_a_quoted_signal_reads_unquoted(self, server, roots):
        # Digests are written freehand; `signal: "low"` is the same judgment
        # as `signal: low`.
        write(
            roots[0] / "state" / "digests" / f"{GRINDER}.md",
            f'---\nid: {GRINDER}\ndate: 2026-02-20\nsignal: "low"\ntopics: [gear]\n---\n'
            "- Alignment beats price.\n",
        )
        item = record(server, "fetch", {"id": f"{COFFEE}/{GRINDER}"})
        assert item["signal"] == "low"
        assert item["digest"] == "- Alignment beats price."

    def test_an_unterminated_fence_is_no_frontmatter_at_all(self, server, roots):
        # A fence that opens and never closes makes nothing frontmatter: no
        # signal is claimed, and the text stands whole rather than half-read.
        write(
            roots[0] / "state" / "digests" / f"{GRINDER}.md",
            f"---\nid: {GRINDER}\nsignal: high\n- A fact under a broken fence.\n",
        )
        item = record(server, "fetch", {"id": f"{COFFEE}/{GRINDER}"})
        assert item["signal"] is None
        assert item["digest"].startswith("---")
        assert "A fact under a broken fence." in item["digest"]

    def test_a_signal_looking_line_in_the_body_is_not_a_signal(self, server, roots):
        # The first closing fence ends the frontmatter; a later `---` is a
        # horizontal rule and everything under it is content, not judgment.
        write(
            roots[0] / "state" / "digests" / f"{GRINDER}.md",
            f"---\nid: {GRINDER}\ndate: 2026-02-20\ntopics: [gear]\n---\n"
            "signal: noise\n\n---\n\n- Alignment beats price.\n",
        )
        item = record(server, "fetch", {"id": f"{COFFEE}/{GRINDER}"})
        assert item["signal"] is None
        assert "- Alignment beats price." in item["digest"]

    def test_the_fetched_source_is_withheld_unless_asked_for(self, server):
        assert record(server, "fetch", {"id": f"{COFFEE}/{V60}"})["enrichment"] is None

    def test_full_adds_the_fetched_source(self, server):
        (fetched,) = record(server, "fetch", {"id": f"{COFFEE}/{V60}", "full": True})["enrichment"]
        assert fetched["name"] == "youtube-f80fab.md"
        assert fetched["url"] == "https://www.youtube.com/watch?v=example"
        assert fetched["text"].startswith("## Transcript")

    def test_an_item_with_no_digest_or_enrichment_still_reads(self, server):
        item = record(server, "fetch", {"id": f"{COFFEE}/{GRINDER}", "full": True})
        assert item["digest"] is None
        assert item["signal"] is None
        assert item["enrichment"] == []
        assert item["note"] == "Burr alignment matters more than the grinder's price."
        assert item["urls"] == []

    def test_an_unreadable_fetched_source_is_skipped(self, server, roots):
        (roots[0] / "enrichment" / V60 / "aaa-interrupted.md").write_bytes(b"\xff\xfe not text")
        (fetched,) = record(server, "fetch", {"id": f"{COFFEE}/{V60}", "full": True})["enrichment"]
        assert fetched["name"] == "youtube-f80fab.md"

    def test_an_unknown_item_is_refused_by_name(self, server):
        message = refusal(server, "fetch", {"id": f"{COFFEE}/2026-01-01-nope-000000"})
        assert "dex-coffee holds no item '2026-01-01-nope-000000'" in message
        assert "corpus/2026/2026-01-01-nope-000000.md" in message

    def test_an_item_that_does_not_parse_says_so(self, server, roots):
        broken = "2026-04-04-broken-000000"
        (roots[0] / "corpus" / "2026" / f"{broken}.md").write_text("no frontmatter here")
        message = refusal(server, "fetch", {"id": f"{COFFEE}/{broken}"})
        assert f"item '{COFFEE}/{broken}' does not parse" in message
        assert "frontmatter fence" in message

    def test_an_unnamespaced_id_says_what_an_id_looks_like(self, server):
        assert "not a dex item id" in refusal(server, "fetch", {"id": V60})

    def test_an_id_cannot_reach_across_instances(self, server):
        assert "holds no item" in refusal(server, "fetch", {"id": f"{BOOKS}/{V60}"})

    def test_an_id_cannot_become_a_path(self, server):
        assert "not a dex item id" in refusal(server, "fetch", {"id": f"{COFFEE}/../../etc/passwd"})


class TestPage:
    def test_reads_a_page_by_its_wikilink_name(self, server):
        page = record(server, "page", {"name": "pour-over", "instance": COFFEE})
        assert page["name"] == "pour-over"
        assert page["instance"] == COFFEE
        assert page["title"] == "Pour-over"
        assert page["path"] == "wiki/topics/pour-over.md"
        assert page["text"].startswith("# Pour-over")
        assert "[[brewing-technique]]" in page["text"]

    def test_frontmatter_is_bookkeeping_not_page_content(self, server):
        # The search side already refuses to snippet `generated:` lines; the
        # page read says the same thing.
        page = record(server, "page", {"name": "pour-over", "instance": COFFEE})
        assert "generated:" not in page["text"]
        assert "topic: pour-over" not in page["text"]

    def test_a_page_with_no_frontmatter_is_served_whole(self, server, roots):
        (roots[0] / "wiki" / "topics" / "decaf.md").write_text("Swiss water process only.\n")
        page = record(server, "page", {"name": "decaf", "instance": COFFEE})
        assert page["text"] == "Swiss water process only.\n"

    def test_lists_the_links_the_body_makes(self, server):
        page = record(server, "page", {"name": "pour-over", "instance": COFFEE})
        assert page["wikilinks"] == ["brewing-technique"]
        assert "page(name, instance)" in page["next"]

    def test_a_link_in_the_frontmatter_is_not_one_of_the_page_s(self, server, roots):
        write(
            roots[0] / "wiki" / "topics" / "gear.md",
            "---\ntopic: gear\nrelated: [[pour-over]]\n---\n# Gear\n\nSee [[grinders]].\n",
        )
        page = record(server, "page", {"name": "gear", "instance": COFFEE})
        assert page["wikilinks"] == ["grinders"]

    def test_the_index_hands_back_the_pages_it_points_at(self, server):
        # The whole index — rendered by the compile — read as one list of
        # `page` calls to make next.
        assert record(server, "page", {"name": "index", "instance": COFFEE})["wikilinks"] == [
            "brewing-technique",
            "pour-over",
            "james-hoffmann",
        ]

    def test_the_md_suffix_is_optional(self, server):
        assert record(server, "page", {"name": "pour-over.md", "instance": COFFEE}) == record(
            server, "page", {"name": "pour-over", "instance": COFFEE}
        )

    def test_the_index_is_a_page_like_any_other(self, server):
        assert record(server, "page", {"name": "index", "instance": COFFEE})["title"] == "Index"

    def test_a_page_with_no_heading_is_titled_by_its_name(self, server, roots):
        (roots[0] / "wiki" / "topics" / "decaf.md").write_text("Swiss water process only.\n")
        assert record(server, "page", {"name": "decaf", "instance": COFFEE})["title"] == "decaf"

    def test_an_unknown_page_says_what_a_page_name_is(self, server):
        message = refusal(server, "page", {"name": "espresso", "instance": COFFEE})
        assert "dex-coffee has no wiki page named 'espresso'" in message
        assert "[[wikilinks]]" in message

    def test_an_instance_with_no_wiki_refuses_rather_than_crashing(self, server):
        assert "has no wiki page" in refusal(
            server, "page", {"name": "pour-over", "instance": BOOKS}
        )

    def test_an_unknown_instance_names_the_served_ones(self, server):
        assert "no instance named" in refusal(
            server, "page", {"name": "pour-over", "instance": "dex-nope"}
        )

    def test_a_page_that_cannot_be_read_says_so(self, server, roots):
        (roots[0] / "wiki" / "topics" / "corrupt.md").write_bytes(b"\xff\xfe not text")
        message = refusal(server, "page", {"name": "corrupt", "instance": COFFEE})
        assert "dex-coffee cannot read wiki page 'corrupt'" in message


class TestTopics:
    def test_states_every_topic_as_the_map_holds_it(self, server):
        listed = record(server, "topics", {"instance": COFFEE})
        assert listed["instance"] == COFFEE
        assert listed["topics"] == [
            {
                "name": "brewing-technique",
                "description": "Method across brew styles.",
                "count": 2,
                "has_page": False,
                "newest": "2026-02-20",
            },
            {
                "name": "pour-over",
                "description": "Filter brewing.",
                "count": 1,
                "has_page": True,
                "newest": "2026-01-15",
            },
        ]

    def test_member_ids_stay_in_the_artifact(self, server):
        # Counts on the wire: ninety topics times full id lists would bury a
        # chat model, and a topic's curated membership is its page's citations.
        for topic in record(server, "topics", {"instance": COFFEE})["topics"]:
            assert "items" not in topic

    def test_travels_with_the_move_that_follows_it(self, server):
        assert "page(name, instance)" in record(server, "topics", {"instance": COFFEE})["next"]

    def test_the_map_is_served_as_stated_never_recomputed(self, server, roots):
        # The read is a cached-file read: a map that disagrees with the
        # taxonomy on disk is served as it stands — freshness is the
        # compile's job, not this call's.
        write(
            roots[0] / "state" / "map.json",
            json.dumps(
                {
                    "topics": {
                        "phantom": {
                            "description": "In no taxonomy at all.",
                            "items": [],
                            "count": 7,
                            "has_page": True,
                            "newest": "2031-01-01",
                        }
                    },
                    "entities": {},
                    "graph": [],
                }
            ),
        )
        listed = record(server, "topics", {"instance": COFFEE})
        assert [(topic["name"], topic["count"]) for topic in listed["topics"]] == [("phantom", 7)]

    def test_an_uncompiled_instance_is_told_how_the_map_grows(self, server):
        message = refusal(server, "topics", {"instance": BOOKS})
        assert f"{BOOKS} has no compiled map" in message
        assert "bin/dex map" in message
        assert "sync" in message

    def test_a_map_that_will_not_parse_names_the_recompile(self, server, roots):
        (roots[0] / "state" / "map.json").write_text("{not json")
        message = refusal(server, "topics", {"instance": COFFEE})
        assert f"{COFFEE}'s state/map.json does not parse" in message
        assert "line 1" in message
        assert "`bin/dex map` recompiles" in message

    def test_a_map_with_the_wrong_shape_reads_as_unparseable(self, server, roots):
        (roots[0] / "state" / "map.json").write_text('{"topics": ["not", "a", "mapping"]}')
        message = refusal(server, "topics", {"instance": COFFEE})
        assert f"{COFFEE}'s state/map.json does not parse" in message
        assert "list" in message
        assert "recompiles" in message

    def test_a_map_that_is_not_an_object_reads_as_unparseable(self, server, roots):
        (roots[0] / "state" / "map.json").write_text('["a", "list"]')
        message = refusal(server, "topics", {"instance": COFFEE})
        assert f"{COFFEE}'s state/map.json does not parse" in message
        assert "expected a JSON object, got list" in message

    def test_an_unknown_instance_names_the_served_ones(self, server):
        assert "no instance named" in refusal(server, "topics", {"instance": "dex-nope"})


class TestEntities:
    def test_states_every_entity_as_the_map_holds_it(self, server):
        listed = record(server, "entities", {"instance": COFFEE})
        assert listed["instance"] == COFFEE
        assert listed["entities"] == [
            {
                "name": "james-hoffmann",
                "kind": "person",
                "aliases": ["Hoffmann", "James Hoffmann"],
                "count": 1,
                "has_page": False,
            }
        ]

    def test_member_ids_stay_in_the_artifact(self, server):
        for entity in record(server, "entities", {"instance": COFFEE})["entities"]:
            assert "items" not in entity

    def test_travels_with_the_move_that_follows_it(self, server):
        assert "`search` terms" in record(server, "entities", {"instance": COFFEE})["next"]

    def test_an_uncompiled_instance_is_refused(self, server):
        assert f"{BOOKS} has no compiled map" in refusal(server, "entities", {"instance": BOOKS})

    def test_a_map_with_the_wrong_shape_reads_as_unparseable(self, server, roots):
        write(
            roots[0] / "state" / "map.json",
            json.dumps({"topics": {}, "entities": "nope", "graph": []}),
        )
        message = refusal(server, "entities", {"instance": COFFEE})
        assert f"{COFFEE}'s state/map.json does not parse" in message


def edge_map(root: Path, edges: list[dict]) -> None:
    """Write a map whose graph is exactly ``edges``, every endpoint a topic."""
    names = {edge["source"] for edge in edges} | {edge["target"] for edge in edges}
    write(
        root / "state" / "map.json",
        json.dumps(
            {"topics": {name: {} for name in sorted(names)}, "entities": {}, "graph": edges}
        ),
    )


class TestGraph:
    def test_the_bare_call_is_the_topic_to_topic_core(self, server):
        # Both endpoints must be topics: the entity edges stay in the map,
        # counted in `total`, reachable via `around` or `full`.
        graph = record(server, "graph", {"instance": COFFEE})
        assert graph["instance"] == COFFEE
        assert [
            (edge["type"], edge["source"], edge["target"], edge["weight"])
            for edge in graph["edges"]
        ] == [
            ("wikilink", "pour-over", "brewing-technique", None),
            ("shared-items", "brewing-technique", "pour-over", 1),
        ]
        assert graph["total"] == 4

    def test_a_trimmed_view_says_how_to_widen(self, server):
        said = record(server, "graph", {"instance": COFFEE})["next"]
        assert "2 of the map's 4 edges" in said
        assert "around" in said
        assert "min_weight" in said
        assert "full" in said

    def test_a_view_served_whole_is_not_reported_as_trimmed(self, server, roots):
        edge_map(
            roots[0],
            [
                {"type": "wikilink", "source": "a", "target": "b"},
                {"type": "shared-items", "source": "a", "target": "b", "weight": 2},
            ],
        )
        graph = record(server, "graph", {"instance": COFFEE})
        assert len(graph["edges"]) == graph["total"] == 2
        assert "Showing" not in graph["next"]
        assert "wikilink" in graph["next"]

    def test_full_serves_every_edge_in_the_map_s_own_order(self, server):
        graph = record(server, "graph", {"instance": COFFEE, "full": True})
        assert [
            (edge["type"], edge["source"], edge["target"], edge["weight"])
            for edge in graph["edges"]
        ] == [
            ("wikilink", "pour-over", "brewing-technique", None),
            ("shared-items", "brewing-technique", "james-hoffmann", 1),
            ("shared-items", "brewing-technique", "pour-over", 1),
            ("shared-items", "james-hoffmann", "pour-over", 1),
        ]
        assert graph["total"] == 4
        assert "Showing" not in graph["next"]

    def test_full_ignores_the_cap(self, server, roots):
        edge_map(
            roots[0],
            [
                {"type": "shared-items", "source": f"a{n:04d}", "target": f"b{n:04d}", "weight": 1}
                for n in range(_EDGE_CAP + 4)
            ],
        )
        graph = record(server, "graph", {"instance": COFFEE, "full": True})
        assert len(graph["edges"]) == graph["total"] == _EDGE_CAP + 4

    def test_the_cap_keeps_every_wikilink_and_the_heaviest_shared(self, server, roots):
        # Weights ascend with the map order here, so the lightest shared
        # edges are the front ones — the cut must drop exactly those.
        edge_map(
            roots[0],
            [{"type": "wikilink", "source": "hub", "target": f"w{n}"} for n in range(3)]
            + [
                {"type": "shared-items", "source": "hub", "target": f"s{n:04d}", "weight": n + 1}
                for n in range(_EDGE_CAP + 5)
            ],
        )
        graph = record(server, "graph", {"instance": COFFEE})
        assert len(graph["edges"]) == _EDGE_CAP
        assert graph["total"] == _EDGE_CAP + 8
        assert [edge["target"] for edge in graph["edges"][:3]] == ["w0", "w1", "w2"]
        assert [edge["weight"] for edge in graph["edges"][3:]] == list(range(9, _EDGE_CAP + 6))
        assert f"{_EDGE_CAP} of the map's {_EDGE_CAP + 8} edges" in graph["next"]

    def test_equal_weights_cut_on_the_map_s_own_order(self, server, roots):
        # Nothing distinguishes weight-1 edges but their position, so the
        # deterministic map order is the tie-break — same map, same cut. The
        # weightless edge is a broken map's: it weighs nothing, so it goes
        # before any weighted one does.
        edge_map(
            roots[0],
            [{"type": "shared-items", "source": "z-broken", "target": "z2"}]
            + [
                {"type": "shared-items", "source": f"a{n:04d}", "target": f"b{n:04d}", "weight": 1}
                for n in range(_EDGE_CAP + 4)
            ],
        )
        graph = record(server, "graph", {"instance": COFFEE})
        assert [edge["source"] for edge in graph["edges"]] == [
            f"a{n:04d}" for n in range(_EDGE_CAP)
        ]

    def test_wikilink_edges_alone_can_exceed_the_cap(self, server, roots):
        # Every wikilink edge survives the cut, even past the cap — and no
        # shared-items edge then earns a place at all.
        edge_map(
            roots[0],
            [
                {"type": "wikilink", "source": "hub", "target": f"w{n:04d}"}
                for n in range(_EDGE_CAP + 1)
            ]
            + [{"type": "shared-items", "source": "hub", "target": "w0000", "weight": 9}],
        )
        graph = record(server, "graph", {"instance": COFFEE})
        assert [edge["type"] for edge in graph["edges"]] == ["wikilink"] * (_EDGE_CAP + 1)

    def test_around_pulls_every_edge_touching_the_name(self, server):
        # An entity name: its edges never survive the bare call, and here
        # they all do — endpoint kind stops mattering.
        graph = record(server, "graph", {"instance": COFFEE, "around": "james-hoffmann"})
        assert [(edge["source"], edge["target"]) for edge in graph["edges"]] == [
            ("brewing-technique", "james-hoffmann"),
            ("james-hoffmann", "pour-over"),
        ]
        assert graph["total"] == 4

    def test_around_serves_all_types(self, server):
        graph = record(server, "graph", {"instance": COFFEE, "around": "pour-over"})
        assert [(edge["type"], edge["source"], edge["target"]) for edge in graph["edges"]] == [
            ("wikilink", "pour-over", "brewing-technique"),
            ("shared-items", "brewing-technique", "pour-over"),
            ("shared-items", "james-hoffmann", "pour-over"),
        ]

    def test_around_is_uncapped(self, server, roots):
        edge_map(
            roots[0],
            [
                {"type": "shared-items", "source": "hub", "target": f"s{n:04d}", "weight": 1}
                for n in range(_EDGE_CAP + 1)
            ],
        )
        graph = record(server, "graph", {"instance": COFFEE, "around": "hub"})
        assert len(graph["edges"]) == _EDGE_CAP + 1

    def test_an_unknown_around_name_is_refused_by_name(self, server):
        message = refusal(server, "graph", {"instance": COFFEE, "around": "kombucha"})
        assert f"{COFFEE} maps no name 'kombucha'" in message
        assert "topics(instance)" in message
        assert "entities(instance)" in message

    def test_min_weight_drops_lighter_shared_edges_only(self, server, roots):
        # An edge exactly at the threshold stays: below N is dropped, N is not.
        edge_map(
            roots[0],
            [
                {"type": "wikilink", "source": "a", "target": "b"},
                {"type": "shared-items", "source": "a", "target": "b", "weight": 1},
                {"type": "shared-items", "source": "a", "target": "c", "weight": 2},
            ],
        )
        graph = record(server, "graph", {"instance": COFFEE, "min_weight": 2})
        assert [(edge["type"], edge["weight"]) for edge in graph["edges"]] == [
            ("wikilink", None),
            ("shared-items", 2),
        ]
        assert graph["total"] == 3

    def test_a_weightless_shared_edge_reads_as_the_lightest(self, server, roots):
        # No compile writes a shared-items edge without a weight; a broken
        # map's is treated as weighing nothing rather than crashing the read.
        edge_map(
            roots[0],
            [
                {"type": "shared-items", "source": "a", "target": "b", "weight": 1},
                {"type": "shared-items", "source": "b", "target": "c"},
            ],
        )
        graph = record(server, "graph", {"instance": COFFEE, "min_weight": 1})
        assert [(edge["source"], edge["weight"]) for edge in graph["edges"]] == [("a", 1)]

    def test_min_weight_composes_with_around(self, server):
        graph = record(
            server, "graph", {"instance": COFFEE, "around": "james-hoffmann", "min_weight": 2}
        )
        assert graph["edges"] == []
        assert graph["total"] == 4

    def test_min_weight_composes_with_full(self, server, roots):
        # Endpoints outside the topics on purpose: `full` lifts the
        # topic↔topic restriction, not just the cap.
        write(
            roots[0] / "state" / "map.json",
            json.dumps(
                {
                    "topics": {},
                    "entities": {},
                    "graph": [
                        {"type": "wikilink", "source": "a", "target": "b"},
                        {"type": "shared-items", "source": "a", "target": "b", "weight": 1},
                        {"type": "shared-items", "source": "a", "target": "c", "weight": 5},
                    ],
                }
            ),
        )
        graph = record(server, "graph", {"instance": COFFEE, "full": True, "min_weight": 3})
        assert [(edge["type"], edge["weight"]) for edge in graph["edges"]] == [
            ("wikilink", None),
            ("shared-items", 5),
        ]

    def test_full_and_around_together_are_refused(self, server):
        message = refusal(
            server, "graph", {"instance": COFFEE, "around": "pour-over", "full": True}
        )
        assert "contradict" in message

    def test_an_edge_is_served_as_stated_never_rechecked(self, server, roots):
        # A weight is what the file says it is; nothing counts members here.
        write(
            roots[0] / "state" / "map.json",
            json.dumps(
                {
                    "topics": {},
                    "entities": {},
                    "graph": [
                        {"type": "wikilink", "source": "a", "target": "b"},
                        {"type": "shared-items", "source": "a", "target": "b", "weight": 41},
                    ],
                }
            ),
        )
        edges = record(server, "graph", {"instance": COFFEE, "full": True})["edges"]
        assert [edge["weight"] for edge in edges] == [None, 41]

    def test_an_uncompiled_instance_is_refused(self, server):
        assert f"{BOOKS} has no compiled map" in refusal(server, "graph", {"instance": BOOKS})

    def test_a_map_with_the_wrong_shape_reads_as_unparseable(self, server, roots):
        write(
            roots[0] / "state" / "map.json",
            json.dumps({"topics": {}, "entities": {}, "graph": 42}),
        )
        message = refusal(server, "graph", {"instance": COFFEE})
        assert f"{COFFEE}'s state/map.json does not parse" in message

    def test_a_map_missing_its_topics_reads_as_unparseable(self, server, roots):
        write(roots[0] / "state" / "map.json", json.dumps({"entities": {}, "graph": []}))
        message = refusal(server, "graph", {"instance": COFFEE})
        assert f"{COFFEE}'s state/map.json does not parse" in message

    def test_a_wrong_shaped_map_under_around_still_names_the_instance(self, server, roots):
        # The recompile refusal has to say whose map broke — one server
        # serves several instances.
        write(
            roots[0] / "state" / "map.json",
            json.dumps({"topics": 42, "entities": {}, "graph": []}),
        )
        message = refusal(server, "graph", {"instance": COFFEE, "around": "x"})
        assert f"{COFFEE}'s state/map.json does not parse" in message


class TestResources:
    def test_each_instance_offers_the_maps_it_has(self, server):
        listed = drive(server, lambda client: client.list_resources())
        assert [
            (str(resource.uri), resource.name, resource.mime_type) for resource in listed.resources
        ] == [
            (f"dex://{COFFEE}/wiki/index.md", f"{COFFEE}/wiki/index.md", "text/markdown"),
            (
                f"dex://{COFFEE}/state/taxonomy.json",
                f"{COFFEE}/state/taxonomy.json",
                "application/json",
            ),
            (
                f"dex://{COFFEE}/state/map.json",
                f"{COFFEE}/state/map.json",
                "application/json",
            ),
        ]
        assert all(COFFEE in resource.description for resource in listed.resources)

    def test_a_map_is_offered_even_when_the_others_are_absent(self, roots):
        # A young instance grows a taxonomy before it grows a wiki or a
        # compiled map.
        (roots[1] / "state" / "taxonomy.json").write_text("{}")
        server = build_server(build_roster(roots))
        listed = drive(server, lambda client: client.list_resources())
        uris = [str(resource.uri) for resource in listed.resources]
        assert f"dex://{BOOKS}/state/taxonomy.json" in uris
        assert f"dex://{BOOKS}/state/map.json" not in uris

    def test_the_compiled_map_is_served_whole(self, server):
        # The full artifact, entity edges and member ids included — what a
        # resource-capable client reads instead of the graph tool's view.
        read = drive(server, lambda client: client.read_resource(f"dex://{COFFEE}/state/map.json"))
        payload = json.loads(read.contents[0].text)
        assert {
            "type": "shared-items",
            "source": "james-hoffmann",
            "target": "pour-over",
            "weight": 1,
        } in payload["graph"]

    def test_a_map_reads_the_file_on_disk(self, server):
        read = drive(
            server, lambda client: client.read_resource(f"dex://{COFFEE}/state/taxonomy.json")
        )
        assert json.loads(read.contents[0].text)["topics"]["pour-over"]["items"] == [V60]

    def test_a_map_answers_with_what_is_there_now(self, server, roots):
        (roots[0] / "wiki" / "index.md").write_text("# Index\n\n- [[espresso]]\n")
        read = drive(server, lambda client: client.read_resource(f"dex://{COFFEE}/wiki/index.md"))
        assert "[[espresso]]" in read.contents[0].text


class TestCrossInstanceBoundary:
    def test_every_row_names_the_instance_it_came_from(self, server):
        for hit in hits(server, {"query": "e"}):
            assert hit["instance"] in {COFFEE, BOOKS}
            assert hit["id"].startswith(f"{hit['instance']}/")

    def test_a_tool_result_carries_text_as_well_as_structure(self, server):
        # Clients that read only the text block still get the whole answer.
        result = call(server, "page", {"name": "pour-over", "instance": COFFEE})
        assert "wiki/topics/pour-over.md" in result.content[0].text
