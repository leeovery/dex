"""Tests for instance_map.py: the map compiler and the index it renders."""

import json

import pytest

from dex_engine.instance_map import _INDEX_PREAMBLE, compile_map, main, serialize_map, write_map
from dex_engine.pipeline.types import Instance

A = "2026-01-05-alpha-aaaaaa"
B = "2026-02-10-bravo-bbbbbb"
C = "2026-03-15-charlie-cccccc"
D = "2026-04-01-delta-dddddd"


def write_taxonomy(instance: Instance, topics=None, entities=None) -> None:
    payload = {"topics": topics or {}, "entities": entities or {}}
    instance.taxonomy_path.write_text(json.dumps(payload))


def write_members(instance: Instance, members) -> None:
    (instance.state_dir / "entity-members.json").write_text(json.dumps(members))


def write_item(instance: Instance, item_id: str, *, date: str) -> None:
    path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: manual\nchannel: inbox\nshared_by: alex\n"
        f"date: {date}\n"
        "kinds: [web]\nstatus: raw\nenrichment: []\n---\nnote\n"
    )


def write_page(instance: Instance, name: str, body: str = "", group: str = "topics") -> None:
    path = instance.wiki_dir / group / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntopic: {name}\ngenerated: 2026-08-01\n---\n{body}")


def write_synthesis(instance: Instance, name: str, text: str) -> None:
    path = instance.wiki_dir / "syntheses" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def index_of(instance: Instance) -> str:
    return compile_map(instance).index


def seed(instance: Instance) -> None:
    """Two topics, one entity, the uncategorized ledger, one linking page."""
    write_taxonomy(
        instance,
        topics={
            "brewing": {"description": "Brew technique.", "items": [B, A]},
            "grinders": {"description": "Grinder gear.", "items": [B, C]},
            "uncategorized-shares": {"description": "Ledger.", "items": [D]},
        },
        entities={"hoffmann": {"kind": "person", "raw": ["james-hoffmann", "hoffmann"]}},
    )
    write_members(instance, {"hoffmann": [C, A]})
    write_item(instance, A, date="2026-01-05")
    write_item(instance, B, date="2026-02-10")
    write_item(instance, C, date="2026-03-15")
    write_item(instance, D, date="2026-04-01")
    write_page(instance, "brewing", "See [[grinders]] and [[hoffmann|James]].")


def topics_of(instance: Instance) -> dict:
    topics = compile_map(instance).payload["topics"]
    assert isinstance(topics, dict)
    return topics


def entities_of(instance: Instance) -> dict:
    entities = compile_map(instance).payload["entities"]
    assert isinstance(entities, dict)
    return entities


def graph_of(instance: Instance) -> list:
    graph = compile_map(instance).payload["graph"]
    assert isinstance(graph, list)
    return graph


class TestTopics:
    def test_a_topic_carries_its_full_row(self, instance):
        seed(instance)
        assert topics_of(instance)["brewing"] == {
            "description": "Brew technique.",
            "items": [A, B],
            "count": 2,
            "has_page": True,
            "newest": "2026-02-10",
        }

    def test_topic_names_are_sorted(self, instance):
        seed(instance)
        assert list(topics_of(instance)) == ["brewing", "grinders"]

    def test_uncategorized_shares_is_not_a_topic(self, instance):
        seed(instance)
        assert "uncategorized-shares" not in topics_of(instance)

    def test_member_ids_are_sorted_and_deduplicated(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": "d", "items": [C, A, C, B, A]}})
        topic = topics_of(instance)["brewing"]
        assert topic["items"] == [A, B, C]
        assert topic["count"] == 3

    def test_an_absent_description_reads_as_empty(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": []}})
        assert topics_of(instance)["brewing"]["description"] == ""

    def test_a_bare_topic_object_reads_as_empty(self, instance):
        write_taxonomy(instance, topics={"brewing": {}})
        topic = topics_of(instance)["brewing"]
        assert topic["items"] == []
        assert topic["count"] == 0
        assert topic["newest"] is None

    def test_a_topic_without_a_page_says_so(self, instance):
        seed(instance)
        assert topics_of(instance)["grinders"]["has_page"] is False

    def test_a_page_anywhere_under_wiki_counts(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": "d", "items": []}})
        write_page(instance, "brewing", group="syntheses")
        assert topics_of(instance)["brewing"]["has_page"] is True


class TestNewest:
    def test_newest_is_the_max_share_date_across_members(self, instance):
        seed(instance)
        assert topics_of(instance)["grinders"]["newest"] == "2026-03-15"

    def test_a_member_with_no_corpus_file_is_skipped(self, instance):
        write_taxonomy(
            instance,
            topics={"brewing": {"description": "d", "items": [A, "2026-09-01-ghost-eeeeee"]}},
        )
        write_item(instance, A, date="2026-01-05")
        assert topics_of(instance)["brewing"]["newest"] == "2026-01-05"

    def test_an_unparseable_corpus_file_is_skipped(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": "d", "items": [A, B]}})
        write_item(instance, A, date="2026-01-05")
        path = instance.corpus_dir / B[:4] / f"{B}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("no frontmatter at all")
        assert topics_of(instance)["brewing"]["newest"] == "2026-01-05"

    def test_no_resolvable_member_is_null(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": "d", "items": [A]}})
        assert topics_of(instance)["brewing"]["newest"] is None

    def test_an_unparseable_file_does_not_stop_the_corpus_scan(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": "d", "items": [A]}})
        broken = instance.corpus_dir / "2026" / "2026-01-01-broken-ffffff.md"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("no frontmatter at all")  # sorts before every member
        write_item(instance, A, date="2026-01-05")
        assert topics_of(instance)["brewing"]["newest"] == "2026-01-05"


class TestEntities:
    def test_an_entity_carries_its_full_row(self, instance):
        seed(instance)
        assert entities_of(instance)["hoffmann"] == {
            "kind": "person",
            "aliases": ["hoffmann", "james-hoffmann"],
            "items": [A, C],
            "count": 2,
            "has_page": False,
        }

    def test_aliases_are_sorted_and_deduplicated(self, instance):
        write_taxonomy(
            instance, entities={"v60": {"kind": "tool", "raw": ["v60", "hario-v60", "v60"]}}
        )
        assert entities_of(instance)["v60"]["aliases"] == ["hario-v60", "v60"]

    def test_an_entity_absent_from_entity_members_has_no_items(self, instance):
        write_taxonomy(instance, entities={"v60": {"kind": "tool", "raw": ["v60"]}})
        entity = entities_of(instance)["v60"]
        assert entity["items"] == []
        assert entity["count"] == 0

    def test_an_entity_members_key_the_taxonomy_does_not_name_is_no_node(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": "d", "items": [A]}})
        write_members(instance, {"ghost": [A]})
        compiled = compile_map(instance)
        assert compiled.payload["entities"] == {}
        assert compiled.payload["graph"] == []

    def test_an_entity_page_counts_toward_has_page(self, instance):
        write_taxonomy(instance, entities={"v60": {"kind": "tool", "raw": ["v60"]}})
        write_page(instance, "v60", group="entities")
        assert entities_of(instance)["v60"]["has_page"] is True

    def test_a_bare_entity_object_reads_as_empty(self, instance):
        write_taxonomy(instance, entities={"v60": {}})
        entity = entities_of(instance)["v60"]
        assert entity["kind"] == ""
        assert entity["aliases"] == []


class TestWikilinkEdges:
    def test_edges_are_directed_from_the_linking_page(self, instance):
        seed(instance)
        wikilinks = [e for e in graph_of(instance) if e["type"] == "wikilink"]
        assert {"type": "wikilink", "source": "brewing", "target": "grinders"} in wikilinks
        assert {"type": "wikilink", "source": "grinders", "target": "brewing"} not in wikilinks

    def test_the_alias_form_resolves_by_name(self, instance):
        seed(instance)
        assert {"type": "wikilink", "source": "brewing", "target": "hoffmann"} in graph_of(instance)

    def test_a_target_outside_the_taxonomy_is_no_edge(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": "d", "items": []}})
        write_page(instance, "brewing", "See [[unbuilt-name]] and [[some-synthesis]].")
        write_page(instance, "some-synthesis", group="syntheses")
        assert graph_of(instance) == []

    def test_frontmatter_links_are_not_parsed(self, instance):
        write_taxonomy(
            instance,
            topics={
                "brewing": {"description": "d", "items": []},
                "grinders": {"description": "d", "items": []},
            },
        )
        path = instance.wiki_dir / "topics" / "brewing.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ntopic: brewing\nsee: [[grinders]]\n---\nNo links in the body.\n")
        assert graph_of(instance) == []

    def test_uncategorized_shares_is_off_the_graph(self, instance):
        write_taxonomy(
            instance,
            topics={
                "brewing": {"description": "d", "items": [D]},
                "uncategorized-shares": {"description": "Ledger.", "items": [D]},
            },
        )
        write_page(instance, "brewing", "See [[uncategorized-shares]].")
        write_page(instance, "uncategorized-shares", "See [[brewing]].")
        assert graph_of(instance) == []

    def test_a_self_link_is_no_edge(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": "d", "items": []}})
        write_page(instance, "brewing", "See [[brewing]].")
        assert graph_of(instance) == []

    def test_duplicate_links_are_one_edge(self, instance):
        write_taxonomy(
            instance,
            topics={
                "brewing": {"description": "d", "items": []},
                "grinders": {"description": "d", "items": []},
            },
        )
        write_page(instance, "brewing", "[[grinders]] and again [[grinders]].")
        assert graph_of(instance) == [
            {"type": "wikilink", "source": "brewing", "target": "grinders"}
        ]

    def test_an_unreadable_page_contributes_no_edges(self, instance):
        write_taxonomy(
            instance,
            topics={
                "brewing": {"description": "d", "items": []},
                "grinders": {"description": "d", "items": []},
            },
        )
        path = instance.wiki_dir / "topics" / "brewing.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe[[grinders]]")
        assert graph_of(instance) == []

    def test_an_unreadable_page_does_not_stop_the_others(self, instance):
        write_taxonomy(
            instance,
            topics={
                "aaa": {"description": "d", "items": []},
                "bbb": {"description": "d", "items": []},
            },
        )
        # "aaa" sorts first, so its unreadable page is met before "bbb"'s.
        broken = instance.wiki_dir / "topics" / "aaa.md"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_bytes(b"\xff\xfe[[bbb]]")
        write_page(instance, "bbb", "See [[aaa]].")
        assert graph_of(instance) == [{"type": "wikilink", "source": "bbb", "target": "aaa"}]


class TestSharedItemEdges:
    def test_topic_pairs_are_weighted_by_intersection(self, instance):
        write_taxonomy(
            instance,
            topics={
                "brewing": {"description": "d", "items": [A, B, C]},
                "grinders": {"description": "d", "items": [B, C, D]},
            },
        )
        assert graph_of(instance) == [
            {"type": "shared-items", "source": "brewing", "target": "grinders", "weight": 2}
        ]

    def test_entity_topic_pairs_are_weighted(self, instance):
        seed(instance)
        shared = [e for e in graph_of(instance) if e["type"] == "shared-items"]
        assert {
            "type": "shared-items",
            "source": "brewing",
            "target": "hoffmann",
            "weight": 1,
        } in shared

    def test_a_zero_intersection_is_no_edge(self, instance):
        write_taxonomy(
            instance,
            topics={
                "brewing": {"description": "d", "items": [A]},
                "grinders": {"description": "d", "items": [B]},
            },
        )
        assert graph_of(instance) == []

    def test_pairs_are_stored_once_smaller_name_first(self, instance):
        write_taxonomy(
            instance,
            topics={"aaa-topic": {"description": "d", "items": [A]}},
            entities={"zzz-entity": {"kind": "tool", "raw": []}},
        )
        write_members(instance, {"zzz-entity": [A]})
        assert graph_of(instance) == [
            {"type": "shared-items", "source": "aaa-topic", "target": "zzz-entity", "weight": 1}
        ]

    def test_two_entities_sharing_a_member_is_no_edge(self, instance):
        write_taxonomy(
            instance,
            entities={
                "hoffmann": {"kind": "person", "raw": []},
                "v60": {"kind": "tool", "raw": []},
            },
        )
        write_members(instance, {"hoffmann": [A], "v60": [A]})
        assert graph_of(instance) == []

    def test_a_name_that_is_both_topic_and_entity_still_weighs_its_other_pairs(self, instance):
        write_taxonomy(
            instance,
            topics={
                "shared-name": {"description": "d", "items": []},
                "zzz": {"description": "d", "items": [A]},
            },
            entities={"shared-name": {"kind": "tool", "raw": []}},
        )
        write_members(instance, {"shared-name": [A]})
        assert graph_of(instance) == [
            {"type": "shared-items", "source": "shared-name", "target": "zzz", "weight": 1}
        ]


class TestDeterminism:
    def test_recompiling_unchanged_inputs_is_byte_identical(self, instance):
        seed(instance)
        first = serialize_map(compile_map(instance).payload)
        second = serialize_map(compile_map(instance).payload)
        assert first == second
        write_map(instance)
        once = instance.map_path.read_bytes()
        write_map(instance)
        assert instance.map_path.read_bytes() == once

    def test_input_key_order_does_not_change_the_bytes(self, instance):
        seed(instance)
        first = serialize_map(compile_map(instance).payload)
        # The same taxonomy content with every object's keys reversed.
        shuffled = json.loads(instance.taxonomy_path.read_text())
        shuffled = {
            "entities": dict(reversed(shuffled["entities"].items())),
            "topics": dict(reversed(shuffled["topics"].items())),
        }
        instance.taxonomy_path.write_text(json.dumps(shuffled))
        assert serialize_map(compile_map(instance).payload) == first

    def test_edge_order_is_wikilinks_then_shared_items_each_sorted(self, instance):
        seed(instance)
        write_page(instance, "grinders", "Back to [[brewing]].")
        assert graph_of(instance) == [
            {"type": "wikilink", "source": "brewing", "target": "grinders"},
            {"type": "wikilink", "source": "brewing", "target": "hoffmann"},
            {"type": "wikilink", "source": "grinders", "target": "brewing"},
            {"type": "shared-items", "source": "brewing", "target": "grinders", "weight": 1},
            {"type": "shared-items", "source": "brewing", "target": "hoffmann", "weight": 1},
            {"type": "shared-items", "source": "grinders", "target": "hoffmann", "weight": 1},
        ]


class TestIndex:
    """The same compile's other artifact: wiki/index.md, rendered whole."""

    def test_the_index_is_written_beside_the_map(self, instance):
        seed(instance)
        compiled = write_map(instance)
        assert instance.index_path.read_text(encoding="utf-8") == compiled.index

    def test_the_header_states_what_the_file_is(self, instance):
        seed(instance)
        index = index_of(instance)
        assert index.startswith("# Index\n\n")
        # The standing preamble: rendered, regenerated, never hand-edited.
        assert "`bin/dex map`" in index
        assert "hand edit does not survive" in index

    def test_topics_render_with_description_and_count(self, instance):
        seed(instance)
        index = index_of(instance)
        assert "## Topics\n" in index
        assert "- [[brewing]] — Brew technique. (2 items)\n" in index
        assert "- [[grinders]] — Grinder gear. (2 items)\n" in index

    def test_topics_are_sorted_as_the_map_is(self, instance):
        seed(instance)
        index = index_of(instance)
        assert index.index("[[brewing]]") < index.index("[[grinders]]")

    def test_a_topic_without_a_description_drops_the_dash(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": [A]}})
        assert "- [[brewing]] (1 item)\n" in index_of(instance)

    def test_uncategorized_shares_is_off_the_index(self, instance):
        seed(instance)
        assert "uncategorized-shares" not in index_of(instance)

    def test_entities_render_with_kind_and_count(self, instance):
        seed(instance)
        index = index_of(instance)
        assert "## Entities\n" in index
        assert "- [[hoffmann]] (person, 2 items)\n" in index

    def test_an_entity_without_a_kind_drops_it(self, instance):
        write_taxonomy(instance, entities={"v60": {"raw": []}})
        assert "- [[v60]] (0 items)\n" in index_of(instance)

    def test_a_synthesis_renders_with_its_first_heading(self, instance):
        write_taxonomy(instance)
        write_synthesis(
            instance,
            "whisper-locally",
            "---\ntype: synthesis\ngenerated: 2026-08-01\n---\n\n"
            "# Can whisper run locally?\n\nYes.\n",
        )
        index = index_of(instance)
        assert "## Syntheses\n" in index
        assert "- [[whisper-locally]] — Can whisper run locally?\n" in index

    def test_a_synthesis_without_a_heading_lists_bare(self, instance):
        write_taxonomy(instance)
        write_synthesis(instance, "notes", "---\ntype: synthesis\n---\nprose only\n")
        assert "- [[notes]]\n" in index_of(instance)

    def test_an_unreadable_synthesis_still_lists(self, instance):
        # The stem is the filename's fact — the page exists, and the index
        # saying otherwise would hide it.
        write_taxonomy(instance)
        path = instance.wiki_dir / "syntheses" / "broken.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe# not utf-8")
        assert "- [[broken]]\n" in index_of(instance)

    def test_a_frontmatter_comment_is_not_the_heading(self, instance):
        write_taxonomy(instance)
        write_synthesis(
            instance,
            "commented",
            "---\ntype: synthesis\n# a yaml comment\n---\n# The real heading\n",
        )
        assert "- [[commented]] — The real heading\n" in index_of(instance)

    def test_syntheses_are_sorted_by_stem(self, instance):
        write_taxonomy(instance)
        write_synthesis(instance, "zzz", "# Z\n")
        write_synthesis(instance, "aaa", "# A\n")
        index = index_of(instance)
        assert index.index("[[aaa]]") < index.index("[[zzz]]")

    def test_empty_sections_are_omitted(self, instance):
        write_taxonomy(instance)
        index = index_of(instance)
        assert index.startswith("# Index\n")
        assert "## Topics" not in index
        assert "## Entities" not in index
        assert "## Syntheses" not in index

    def test_only_the_non_empty_sections_render(self, instance):
        write_taxonomy(instance)
        write_synthesis(instance, "one", "# One\n")
        index = index_of(instance)
        assert "## Syntheses\n" in index
        assert "## Topics" not in index
        assert "## Entities" not in index

    def test_a_bare_root_gets_an_honestly_empty_index(self, tmp_path):
        write_map(Instance(root=tmp_path))
        index = (tmp_path / "wiki" / "index.md").read_text(encoding="utf-8")
        assert index.startswith("# Index\n")
        assert "## Topics" not in index

    def test_a_multiline_description_renders_on_one_line(self, instance):
        # The description is the session's; the layout is not — a newline
        # in judgment text must never invent index lines.
        write_taxonomy(instance, topics={"brewing": {"description": "one\ntwo", "items": []}})
        assert "- [[brewing]] — one two (0 items)\n" in index_of(instance)

    def test_a_multiline_kind_renders_on_one_line(self, instance):
        write_taxonomy(instance, entities={"v60": {"kind": "brew\ntool", "raw": []}})
        assert "- [[v60]] (brew tool, 0 items)\n" in index_of(instance)

    def test_the_canonical_byte_form_is_pinned(self, instance):
        """The exact bytes matter: the freshness check diffs a recompile against them."""
        write_taxonomy(
            instance,
            topics={"brewing": {"description": "d", "items": [A]}},
            entities={"v60": {"kind": "tool", "raw": []}},
        )
        write_synthesis(instance, "why-v60", "# Why V60?\n")
        write_item(instance, A, date="2026-01-05")
        write_map(instance)
        assert instance.index_path.read_text(encoding="utf-8") == (
            "# Index\n"
            "\n"
            f"{_INDEX_PREAMBLE}\n"
            "\n"
            "## Topics\n"
            "\n"
            "- [[brewing]] — d (1 item)\n"
            "\n"
            "## Entities\n"
            "\n"
            "- [[v60]] (tool, 0 items)\n"
            "\n"
            "## Syntheses\n"
            "\n"
            "- [[why-v60]] — Why V60?\n"
        )

    def test_the_write_leaves_no_temp_file(self, instance):
        seed(instance)
        write_map(instance)
        assert list(instance.wiki_dir.glob("index.md.*")) == []

    def test_recompiling_unchanged_inputs_is_byte_identical(self, instance):
        seed(instance)
        write_map(instance)
        once = instance.index_path.read_bytes()
        write_map(instance)
        assert instance.index_path.read_bytes() == once

    def test_a_refusal_leaves_the_standing_index_untouched(self, instance):
        seed(instance)
        write_map(instance)
        standing = instance.index_path.read_bytes()
        instance.taxonomy_path.write_text("{not json")
        with pytest.raises(ValueError, match="invalid JSON"):
            write_map(instance)
        assert instance.index_path.read_bytes() == standing


class TestMissingInputs:
    def test_a_missing_taxonomy_compiles_an_honestly_empty_map(self, instance):
        compiled = write_map(instance)
        assert compiled.payload == {"topics": {}, "entities": {}, "graph": []}
        assert instance.map_path.exists()

    def test_a_missing_entity_members_file_leaves_entities_memberless(self, instance):
        write_taxonomy(instance, entities={"v60": {"kind": "tool", "raw": ["v60"]}})
        entity = entities_of(instance)["v60"]
        assert entity["items"] == []
        assert entity["count"] == 0

    def test_a_bare_root_with_no_state_tree_compiles(self, tmp_path):
        compiled = write_map(Instance(root=tmp_path))
        assert compiled.payload == {"topics": {}, "entities": {}, "graph": []}
        assert (tmp_path / "state" / "map.json").exists()

    def test_a_taxonomy_without_topics_or_entities_keys_reads_as_empty(self, instance):
        instance.taxonomy_path.write_text("{}")
        assert compile_map(instance).payload == {"topics": {}, "entities": {}, "graph": []}


class TestMalformedInputs:
    def test_invalid_taxonomy_json_refuses(self, instance):
        instance.taxonomy_path.write_text("{not json")
        with pytest.raises(ValueError, match=r"taxonomy\.json: invalid JSON"):
            compile_map(instance)

    def test_a_non_object_taxonomy_refuses(self, instance):
        instance.taxonomy_path.write_text('["not", "an", "object"]')
        with pytest.raises(ValueError, match="expected a JSON object, got list"):
            compile_map(instance)

    def test_wrong_typed_topics_refuses(self, instance):
        instance.taxonomy_path.write_text('{"topics": []}')
        with pytest.raises(ValueError, match="topics must be an object"):
            compile_map(instance)

    def test_wrong_typed_entities_refuses(self, instance):
        instance.taxonomy_path.write_text('{"entities": []}')
        with pytest.raises(ValueError, match="entities must be an object"):
            compile_map(instance)

    def test_a_wrong_typed_topic_refuses(self, instance):
        write_taxonomy(instance, topics={"brewing": "not an object"})
        with pytest.raises(ValueError, match="topic 'brewing' must be an object"):
            compile_map(instance)

    def test_wrong_typed_items_refuses(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": "d", "items": "a"}})
        with pytest.raises(ValueError, match="items must be a list of strings"):
            compile_map(instance)

    def test_a_non_string_member_refuses(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": "d", "items": [1]}})
        with pytest.raises(ValueError, match="items must be a list of strings"):
            compile_map(instance)

    def test_a_wrong_typed_description_refuses(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": 3, "items": []}})
        with pytest.raises(ValueError, match="description must be a string"):
            compile_map(instance)

    def test_a_wrong_typed_entity_refuses(self, instance):
        write_taxonomy(instance, entities={"v60": ["not", "an", "object"]})
        with pytest.raises(ValueError, match="entity 'v60' must be an object"):
            compile_map(instance)

    def test_wrong_typed_aliases_refuse(self, instance):
        write_taxonomy(instance, entities={"v60": {"kind": "tool", "raw": "v60"}})
        with pytest.raises(ValueError, match="raw must be a list of strings"):
            compile_map(instance)

    def test_invalid_entity_members_json_refuses(self, instance):
        (instance.state_dir / "entity-members.json").write_text("{not json")
        with pytest.raises(ValueError, match=r"entity-members\.json: invalid JSON"):
            compile_map(instance)

    def test_a_non_list_entity_members_value_refuses(self, instance):
        write_members(instance, {"v60": "not a list"})
        with pytest.raises(ValueError, match="must map to a list of item-id strings"):
            compile_map(instance)

    def test_a_refusal_writes_nothing(self, instance):
        seed(instance)
        write_map(instance)
        standing = instance.map_path.read_bytes()
        instance.taxonomy_path.write_text("{not json")
        with pytest.raises(ValueError, match="invalid JSON"):
            write_map(instance)
        assert instance.map_path.read_bytes() == standing


class TestWrite:
    def test_the_file_holds_the_serialized_payload(self, instance):
        seed(instance)
        compiled = write_map(instance)
        assert instance.map_path.read_text(encoding="utf-8") == serialize_map(compiled.payload)

    def test_the_canonical_byte_form_is_pinned(self, instance):
        """The exact bytes matter: a freshness check diffs a recompile against them."""
        write_taxonomy(instance, topics={"brewing": {"description": "d", "items": [A]}})
        write_item(instance, A, date="2026-01-05")
        write_map(instance)
        assert instance.map_path.read_text(encoding="utf-8") == (
            "{\n"
            '  "topics": {\n'
            '    "brewing": {\n'
            '      "description": "d",\n'
            f'      "items": [\n        "{A}"\n      ],\n'
            '      "count": 1,\n'
            '      "has_page": false,\n'
            '      "newest": "2026-01-05"\n'
            "    }\n"
            "  },\n"
            '  "entities": {},\n'
            '  "graph": []\n'
            "}\n"
        )

    def test_non_ascii_judgment_text_is_stored_readable(self, instance):
        write_taxonomy(instance, topics={"brewing": {"description": "café burr", "items": []}})
        write_map(instance)
        text = instance.map_path.read_text(encoding="utf-8")
        assert "café burr" in text
        assert "\\u" not in text

    def test_the_write_leaves_no_temp_file(self, instance):
        seed(instance)
        write_map(instance)
        assert list(instance.state_dir.glob("map.json.*")) == []

    def test_an_existing_map_is_replaced(self, instance):
        seed(instance)
        instance.map_path.write_text("stale")
        write_map(instance)
        assert instance.map_path.read_text(encoding="utf-8") != "stale"

    def test_the_summary_counts_match_the_payload(self, instance):
        seed(instance)
        compiled = write_map(instance)
        assert compiled.topics == 2
        assert compiled.entities == 1
        assert compiled.edges == 5


class TestCli:
    def test_main_compiles_and_prints_the_summary(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        seed(instance)
        main([])
        out = capsys.readouterr().out
        assert out == (
            "map compiled: 2 topics, 1 entities, 5 edges -> state/map.json + wiki/index.md\n"
        )
        assert instance.map_path.exists()
        assert instance.index_path.exists()

    def test_an_empty_instance_still_gets_a_map(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        main([])
        assert "0 topics, 0 entities, 0 edges" in capsys.readouterr().out
        assert (tmp_path / "state" / "map.json").exists()

    def test_a_malformed_taxonomy_is_a_loud_exit(self, instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        instance.taxonomy_path.write_text("{not json")
        with pytest.raises(SystemExit) as excinfo:
            main([])
        assert "dex-map" in str(excinfo.value)
        assert not instance.map_path.exists()
