"""Tests for serve/server.py: the three tools and the map resources, over a real session."""

import json

from dex_engine.serve.roster import build_roster
from dex_engine.serve.server import build_server
from dex_engine.version import engine_version

from .conftest import (
    BOOKS,
    BORGES,
    COFFEE,
    GRINDER,
    V60,
    call,
    drive,
    record,
    refusal,
    rows,
    write,
)


async def _identity(client):
    return client.server_info


class TestTools:
    def test_the_surface_is_three_reads_and_one_capture(self, server):
        listed = drive(server, lambda client: client.list_tools())
        assert [tool.name for tool in listed.tools] == ["search", "fetch", "page", "capture"]
        assert all(tool.description for tool in listed.tools)

    def test_the_server_names_itself_and_the_engine_it_runs(self, server):
        info = drive(server, _identity)
        assert (info.name, info.version) == ("dex", engine_version())


class TestSearch:
    def test_matches_the_digest_behind_an_item(self, server):
        (hit,) = rows(server, "search", {"query": "swirl instead of stir"})
        assert hit["id"] == f"{COFFEE}/{V60}"
        assert hit["type"] == "item"
        assert hit["title"] == "james hoffmann v60 technique"
        assert hit["date"] == "2026-01-15"
        assert hit["url"] == "https://www.youtube.com/watch?v=example"
        assert hit["instance"] == COFFEE
        assert "swirl instead of stir" in hit["snippet"]

    def test_matches_a_corpus_note(self, server):
        (hit,) = rows(server, "search", {"query": "everyone cites"})
        assert hit["id"] == f"{COFFEE}/{V60}"
        assert "The V60 technique video everyone cites." in hit["snippet"]

    def test_one_row_per_item_when_digest_and_note_both_match(self, server):
        # The digest and the note are two halves of one fetch, so a match in
        # either is the same hit — and the digest is the half shown.
        found = rows(server, "search", {"query": "V60"})
        assert [hit["id"] for hit in found] == [f"{COFFEE}/{V60}", f"{COFFEE}/pour-over"]
        assert "signal: high" in found[0]["snippet"]

    def test_a_snippet_is_one_line_around_the_match(self, server):
        (hit,) = rows(server, "search", {"query": "swirl instead of stir"})
        assert hit["snippet"].startswith("…")
        assert "\n" not in hit["snippet"]
        assert hit["snippet"].endswith("swirl instead of stir.")

    def test_a_snippet_only_marks_the_ends_it_cut(self, server):
        # This one runs off the end of the window but starts at byte zero.
        (hit,) = rows(server, "search", {"query": "id: 2026-01-15"})
        assert hit["snippet"].startswith("--- id: 2026-01-15")
        assert hit["snippet"].endswith("…")

    def test_an_item_that_does_not_parse_is_skipped(self, server, roots):
        broken = roots[0] / "corpus" / "2026" / "2026-01-01-broken-000000.md"
        broken.write_text("this file has no frontmatter at all")
        (hit,) = rows(server, "search", {"query": "Burr alignment"})
        assert hit["id"] == f"{COFFEE}/{GRINDER}"

    def test_an_earlier_item_missing_does_not_stop_the_scan(self, server):
        (hit,) = rows(server, "search", {"query": "Burr alignment"})
        assert hit["id"] == f"{COFFEE}/{GRINDER}"

    def test_a_page_with_no_heading_is_titled_by_its_name(self, server, roots):
        (roots[0] / "wiki" / "topics" / "decaf.md").write_text("Swiss water process only.\n")
        (hit,) = rows(server, "search", {"query": "Swiss water"})
        assert (hit["id"], hit["title"]) == (f"{COFFEE}/decaf", "decaf")

    def test_a_frontmatter_id_never_becomes_a_path(self, server, roots, tmp_path):
        # Frontmatter is authored text; the filename is what keys an item.
        (tmp_path / "secret.md").write_text("chartreuse")
        (roots[0] / "corpus" / "2026" / "2026-05-05-stray-000000.md").write_text(
            "---\nid: ../../../secret\nsource: inbox\nchannel: inbox\nshared_by: Lee\n"
            "date: 2026-05-05\nkinds: [text]\nstatus: raw\nenrichment: []\n---\n\nA note.\n"
        )
        assert rows(server, "search", {"query": "chartreuse"}) == []

    def test_is_case_insensitive(self, server):
        assert rows(server, "search", {"query": "HOFFMANN"})

    def test_a_miss_is_no_rows(self, server):
        assert rows(server, "search", {"query": "kombucha"}) == []

    def test_wiki_pages_are_their_own_rows(self, server):
        (hit,) = rows(server, "search", {"query": "post-bloom"})
        assert hit["id"] == f"{COFFEE}/pour-over"
        assert hit["type"] == "page"
        assert hit["title"] == "Pour-over"
        assert hit["date"] is None
        assert hit["url"] is None

    def test_page_frontmatter_is_not_searched(self, server):
        # `generated:` lives only in page frontmatter — bookkeeping, not content.
        assert rows(server, "search", {"query": "generated"}) == []

    def test_a_page_snippet_never_drags_frontmatter_in(self, server):
        # The match sits at the very top of the body, where the lead window
        # would otherwise reach back into the frontmatter fence.
        (hit,) = rows(server, "search", {"query": "starting recipe"})
        assert hit["snippet"].startswith("# Pour-over")
        assert "items: 1" not in hit["snippet"]

    def test_a_horizontal_rule_in_a_body_is_not_a_fence(self, server, roots):
        write(
            roots[0] / "wiki" / "topics" / "ruled.md",
            "---\ntopic: ruled\n---\nAbove the rule.\n\n---\n\nBelow the rule.\n",
        )
        (hit,) = rows(server, "search", {"query": "Below the rule"})
        assert hit["id"] == f"{COFFEE}/ruled"

    def test_dated_rows_come_newest_first(self, server):
        # `shared_by: Lee` is frontmatter both coffee items carry and neither
        # book does — the corpus scan reads the whole file, frontmatter included.
        found = rows(server, "search", {"query": "Lee"})
        assert [hit["id"] for hit in found] == [f"{COFFEE}/{GRINDER}", f"{COFFEE}/{V60}"]

    def test_undated_rows_come_after_dated_ones(self, server):
        found = rows(server, "search", {"query": "pour-over"})
        assert [hit["type"] for hit in found] == ["item", "page", "page"]

    def test_fans_out_across_instances_in_roster_order(self, server):
        found = rows(server, "search", {"query": "method"})
        assert [hit["instance"] for hit in found] == [COFFEE, BOOKS]

    def test_naming_an_instance_restricts_to_it(self, server):
        found = rows(server, "search", {"query": "method", "instance": BOOKS})
        assert [hit["id"] for hit in found] == [f"{BOOKS}/{BORGES}"]

    def test_an_unknown_instance_names_the_served_ones(self, server):
        message = refusal(server, "search", {"query": "method", "instance": "dex-nope"})
        assert "no instance named 'dex-nope'" in message
        assert f"{COFFEE}, {BOOKS}" in message

    def test_an_empty_query_is_refused(self, server):
        assert "search needs a query" in refusal(server, "search", {"query": "   "})

    def test_a_query_is_plain_text_not_a_pattern(self, server):
        assert rows(server, "search", {"query": "60g/L"})
        assert rows(server, "search", {"query": ".*"}) == []


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
        assert "swirl instead of stir" in item["digest"]

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
        assert page["text"].startswith("---\ntopic: pour-over")
        assert "[[brewing-technique]]" in page["text"]

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
        ]
        assert all(COFFEE in resource.description for resource in listed.resources)

    def test_a_map_is_offered_even_when_the_other_is_absent(self, roots):
        # A young instance grows a taxonomy before it grows a wiki.
        (roots[1] / "state" / "taxonomy.json").write_text("{}")
        server = build_server(build_roster(roots))
        listed = drive(server, lambda client: client.list_resources())
        assert f"dex://{BOOKS}/state/taxonomy.json" in [str(r.uri) for r in listed.resources]

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
        for hit in rows(server, "search", {"query": "e"}):
            assert hit["instance"] in {COFFEE, BOOKS}
            assert hit["id"].startswith(f"{hit['instance']}/")

    def test_a_tool_result_carries_text_as_well_as_structure(self, server):
        # Clients that read only the text block still get the whole answer.
        result = call(server, "page", {"name": "pour-over", "instance": COFFEE})
        assert "wiki/topics/pour-over.md" in result.content[0].text
