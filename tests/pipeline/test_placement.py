"""Tests for pipeline/placement.py: `enrich place` — the taxonomy writer."""

import json

import pytest

from dex_engine.instance_map import compile_map, serialize_map
from dex_engine.pipeline.placement import PlacementPayloadError, place
from dex_engine.pipeline.types import Instance

A = "2026-08-19-example-55ad7b"
B = "2026-08-20-other-66be8c"


def run(instance, payload) -> str:
    path = instance.cache_dir / "placement.json"
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return place(path, instance=instance)


def taxonomy(instance) -> dict:
    return json.loads(instance.taxonomy_path.read_text(encoding="utf-8"))


def members(instance) -> dict:
    return json.loads(instance.entity_members_path.read_text(encoding="utf-8"))


def topic(name: str, description: str = "d") -> dict:
    return {"name": name, "description": description}


def entity(name: str, kind: str = "tool", raw: object = None) -> dict:
    record: dict = {"name": name, "kind": kind}
    if raw is not None:
        record["raw"] = raw
    return record


def seed(instance) -> None:
    """One topic and one entity with a member each, via the verb itself."""
    run(
        instance,
        {
            "topics": [topic("brewing", "Brew technique.")],
            "entities": [entity("hoffmann", "person", ["james-hoffmann"])],
            "place": [{"id": A, "topics": ["brewing"], "entities": ["hoffmann"]}],
        },
    )


class TestBootstrap:
    def test_the_first_placement_creates_both_files(self, instance):
        assert not instance.taxonomy_path.exists()
        summary = run(
            instance,
            {
                "topics": [topic("uncategorized-shares", "Ledger for low-signal items.")],
                "place": [{"id": A, "topics": ["uncategorized-shares"]}],
            },
        )
        assert summary == (
            "wrote state/taxonomy.json + state/entity-members.json "
            "(1 topics defined, 0 entities defined, 1 placed, 0 unplaced, 0 dropped) "
            "· map and index recompiled"
        )
        assert taxonomy(instance) == {
            "topics": {
                "uncategorized-shares": {
                    "description": "Ledger for low-signal items.",
                    "items": [A],
                }
            },
            "entities": {},
        }
        assert members(instance) == {}

    def test_a_bare_root_with_no_state_tree_bootstraps(self, tmp_path):
        payload = tmp_path / "placement.json"
        payload.write_text(json.dumps({"topics": [topic("brewing")]}))
        place(payload, instance=Instance(root=tmp_path))
        assert (tmp_path / "state" / "taxonomy.json").exists()
        assert (tmp_path / "state" / "entity-members.json").exists()
        assert (tmp_path / "state" / "map.json").exists()


class TestUpserts:
    def test_a_new_topic_starts_with_no_members(self, instance):
        run(instance, {"topics": [topic("brewing", "Brew technique.")]})
        assert taxonomy(instance)["topics"]["brewing"] == {
            "description": "Brew technique.",
            "items": [],
        }

    def test_redescribing_a_topic_keeps_its_members(self, instance):
        seed(instance)
        run(instance, {"topics": [topic("brewing", "All of brewing.")]})
        assert taxonomy(instance)["topics"]["brewing"] == {
            "description": "All of brewing.",
            "items": [A],
        }

    def test_a_new_entity_folds_its_canonical_name_into_raw(self, instance):
        run(instance, {"entities": [entity("hoffmann", "person", ["james-hoffmann"])]})
        assert taxonomy(instance)["entities"]["hoffmann"] == {
            "kind": "person",
            "raw": ["hoffmann", "james-hoffmann"],
        }

    def test_raw_may_be_omitted(self, instance):
        run(instance, {"entities": [entity("v60")]})
        assert taxonomy(instance)["entities"]["v60"] == {"kind": "tool", "raw": ["v60"]}

    def test_redefining_an_entity_replaces_the_definition_and_keeps_members(self, instance):
        seed(instance)
        run(instance, {"entities": [entity("hoffmann", "concept", ["the-hoff"])]})
        assert taxonomy(instance)["entities"]["hoffmann"] == {
            "kind": "concept",
            "raw": ["hoffmann", "the-hoff"],
        }
        assert members(instance) == {"hoffmann": [A]}

    @pytest.mark.parametrize("key", ["topics", "entities"])
    def test_defining_one_name_twice_in_a_payload_is_refused(self, instance, key):
        records = (
            [topic("dup"), topic("dup")] if key == "topics" else [entity("dup"), entity("dup")]
        )
        with pytest.raises(PlacementPayloadError, match="'dup' is defined twice"):
            run(instance, {key: records})

    def test_a_topic_definition_naming_items_is_refused(self, instance):
        # Membership is place/unplace's to move; a definition stating it is
        # a caller's guess at engine-held state.
        with pytest.raises(PlacementPayloadError, match="items is the engine's to derive"):
            run(instance, {"topics": [{**topic("brewing"), "items": [A]}]})


class TestPlace:
    def test_place_appends_to_an_existing_topic(self, instance):
        seed(instance)
        run(instance, {"place": [{"id": B, "topics": ["brewing"]}]})
        assert taxonomy(instance)["topics"]["brewing"]["items"] == [A, B]

    def test_place_into_a_topic_the_same_payload_defines(self, instance):
        run(
            instance,
            {"topics": [topic("grinders")], "place": [{"id": A, "topics": ["grinders"]}]},
        )
        assert taxonomy(instance)["topics"]["grinders"]["items"] == [A]

    def test_place_creates_the_entity_member_list(self, instance):
        run(
            instance,
            {"entities": [entity("v60")], "place": [{"id": A, "entities": ["v60"]}]},
        )
        assert members(instance) == {"v60": [A]}

    def test_one_record_may_name_several_topics_and_entities(self, instance):
        seed(instance)
        run(
            instance,
            {
                "topics": [topic("grinders")],
                "entities": [entity("v60")],
                "place": [{"id": B, "topics": ["brewing", "grinders"], "entities": ["v60"]}],
            },
        )
        assert taxonomy(instance)["topics"]["grinders"]["items"] == [B]
        assert taxonomy(instance)["topics"]["brewing"]["items"] == [A, B]
        assert members(instance)["v60"] == [B]

    def test_replacing_an_already_placed_id_is_idempotent(self, instance):
        seed(instance)
        before = instance.taxonomy_path.read_bytes()
        summary = run(
            instance, {"place": [{"id": A, "topics": ["brewing"], "entities": ["hoffmann"]}]}
        )
        assert "0 placed" in summary
        assert instance.taxonomy_path.read_bytes() == before

    def test_place_into_an_unknown_topic_is_refused(self, instance):
        with pytest.raises(
            PlacementPayloadError,
            match=r"place\[0\]: topic 'brewing' does not exist and this payload does not define it",
        ):
            run(instance, {"place": [{"id": A, "topics": ["brewing"]}]})

    def test_place_into_an_unknown_entity_is_refused(self, instance):
        seed(instance)
        with pytest.raises(PlacementPayloadError, match=r"place\[0\]: entity 'v60' does not exist"):
            run(instance, {"place": [{"id": A, "entities": ["v60"]}]})

    def test_a_record_naming_no_topic_and_no_entity_is_refused(self, instance):
        with pytest.raises(PlacementPayloadError, match=r"place\[0\] names no topic and no entity"):
            run(instance, {"place": [{"id": A}]})


class TestUnplace:
    def test_unplace_removes_the_membership(self, instance):
        seed(instance)
        run(instance, {"unplace": [{"id": A, "topics": ["brewing"]}]})
        assert taxonomy(instance)["topics"]["brewing"]["items"] == []

    def test_unplacing_the_last_entity_member_drops_the_key(self, instance):
        seed(instance)
        run(instance, {"unplace": [{"id": A, "entities": ["hoffmann"]}]})
        assert members(instance) == {}

    def test_unplacing_what_is_not_placed_is_refused(self, instance):
        seed(instance)
        with pytest.raises(
            PlacementPayloadError, match=rf"unplace\[0\]: {B} is not placed in topic 'brewing'"
        ):
            run(instance, {"unplace": [{"id": B, "topics": ["brewing"]}]})

    def test_unplacing_from_an_unknown_topic_is_refused(self, instance):
        with pytest.raises(PlacementPayloadError, match="is not placed in topic 'ghost'"):
            run(instance, {"unplace": [{"id": A, "topics": ["ghost"]}]})

    def test_unplacing_a_missing_entity_member_is_refused(self, instance):
        seed(instance)
        with pytest.raises(
            PlacementPayloadError, match=rf"unplace\[0\]: {B} is not a member of entity 'hoffmann'"
        ):
            run(instance, {"unplace": [{"id": B, "entities": ["hoffmann"]}]})

    def test_a_member_list_the_taxonomy_does_not_name_still_empties(self, instance):
        # Drifted state an old hand-write left behind: the list exists, the
        # entity does not. What is placed can be unplaced, so the leftover
        # empties out through the verb — and its key goes with the last id.
        seed(instance)
        raw = json.loads(instance.entity_members_path.read_text())
        raw["ghost"] = [A]
        instance.entity_members_path.write_text(json.dumps(raw))
        run(instance, {"unplace": [{"id": A, "entities": ["ghost"]}]})
        assert members(instance) == {"hoffmann": [A]}

    def test_a_sweep_move_is_place_plus_unplace_in_one_payload(self, instance):
        seed(instance)
        run(
            instance,
            {
                "topics": [topic("pour-over")],
                "place": [{"id": A, "topics": ["pour-over"]}],
                "unplace": [{"id": A, "topics": ["brewing"]}],
            },
        )
        assert taxonomy(instance)["topics"]["pour-over"]["items"] == [A]
        assert taxonomy(instance)["topics"]["brewing"]["items"] == []


class TestDrop:
    def test_drop_removes_the_topic_and_its_memberships(self, instance):
        seed(instance)
        run(instance, {"drop": {"topics": ["brewing"]}})
        assert taxonomy(instance)["topics"] == {}

    def test_drop_removes_the_entity_and_its_member_list(self, instance):
        seed(instance)
        run(instance, {"drop": {"entities": ["hoffmann"]}})
        assert taxonomy(instance)["entities"] == {}
        assert members(instance) == {}

    def test_dropping_uncategorized_shares_is_refused(self, instance):
        run(instance, {"topics": [topic("uncategorized-shares", "Ledger.")]})
        with pytest.raises(
            PlacementPayloadError, match="uncategorized-shares is the coverage ledger"
        ):
            run(instance, {"drop": {"topics": ["uncategorized-shares"]}})

    def test_dropping_an_unknown_topic_is_refused(self, instance):
        with pytest.raises(PlacementPayloadError, match="drop: topic 'ghost' does not exist"):
            run(instance, {"drop": {"topics": ["ghost"]}})

    def test_dropping_an_unknown_entity_is_refused(self, instance):
        with pytest.raises(PlacementPayloadError, match="drop: entity 'ghost' does not exist"):
            run(instance, {"drop": {"entities": ["ghost"]}})

    def test_a_drop_naming_nothing_is_refused(self, instance):
        seed(instance)
        with pytest.raises(PlacementPayloadError, match="drop names no topic and no entity"):
            run(instance, {"drop": {"topics": []}})

    def test_dropping_an_entity_with_no_member_list_is_clean(self, instance):
        # A defined entity nothing was ever placed into holds no
        # entity-members key; the drop must not trip over its absence.
        run(instance, {"entities": [entity("v60")]})
        run(instance, {"drop": {"entities": ["v60"]}})
        assert taxonomy(instance)["entities"] == {}

    def test_drop_applies_last(self, instance):
        # Sequential order is the contract: the payload may place into a
        # topic and still fold it away in the same file.
        seed(instance)
        run(
            instance,
            {"place": [{"id": B, "topics": ["brewing"]}], "drop": {"topics": ["brewing"]}},
        )
        assert taxonomy(instance)["topics"] == {}


class TestPayloadValidation:
    def test_invalid_json_is_refused(self, instance):
        with pytest.raises(PlacementPayloadError, match="invalid JSON"):
            run(instance, "not json at all")

    @pytest.mark.parametrize("text", ["[]", '"a string"', "3"])
    def test_a_payload_that_is_not_an_object_is_refused(self, instance, text):
        with pytest.raises(PlacementPayloadError, match="expected a JSON object"):
            run(instance, text)

    def test_an_unknown_top_level_key_is_refused(self, instance):
        with pytest.raises(PlacementPayloadError, match=r"unknown key\(s\) \['move'\]"):
            run(instance, {"move": [{"id": A}]})

    @pytest.mark.parametrize("payload", [{}, {"place": []}, {"topics": [], "unplace": []}])
    def test_a_payload_asking_nothing_is_refused(self, instance, payload):
        with pytest.raises(PlacementPayloadError, match="states no operations"):
            run(instance, payload)

    @pytest.mark.parametrize("value", ["brewing", 3, {"a": 1}])
    def test_an_operation_list_that_is_not_a_list_is_refused(self, instance, value):
        with pytest.raises(PlacementPayloadError, match="topics must be a list"):
            run(instance, {"topics": value})

    def test_a_record_that_is_not_an_object_is_refused(self, instance):
        with pytest.raises(PlacementPayloadError, match=r"place\[1\] must be an object"):
            run(instance, {"place": [{"id": A, "topics": ["x"]}, "not a record"]})

    def test_a_missing_record_key_is_refused_by_name(self, instance):
        with pytest.raises(
            PlacementPayloadError, match=r"topics\[0\]: missing required key\(s\) \['description'\]"
        ):
            run(instance, {"topics": [{"name": "brewing"}]})

    def test_an_unknown_record_key_is_refused_by_name(self, instance):
        with pytest.raises(PlacementPayloadError, match=r"place\[0\]: unknown key\(s\) \['note'\]"):
            run(instance, {"place": [{"id": A, "topics": ["x"], "note": "stray"}]})

    @pytest.mark.parametrize(
        "name",
        ["Agent Architecture", "agent_architecture", "-agent", "agent-", "agent--arch", "café"],
    )
    def test_a_name_that_is_not_kebab_case_is_refused(self, instance, name):
        with pytest.raises(PlacementPayloadError, match="is not kebab-case"):
            run(instance, {"topics": [topic(name)]})

    @pytest.mark.parametrize("value", [3, None, "", "  ", "two\nlines"])
    def test_a_mistyped_description_is_refused(self, instance, value):
        with pytest.raises(
            PlacementPayloadError, match=r"topics\[0\]\.description must be a non-empty"
        ):
            run(instance, {"topics": [{"name": "brewing", "description": value}]})

    @pytest.mark.parametrize("value", ["../../elsewhere", "a b", "", 3])
    def test_a_bad_item_id_is_refused(self, instance, value):
        with pytest.raises(PlacementPayloadError, match=r"place\[0\]\.id"):
            run(
                instance,
                {"topics": [topic("brewing")], "place": [{"id": value, "topics": ["brewing"]}]},
            )

    def test_a_mistyped_raw_is_refused(self, instance):
        with pytest.raises(PlacementPayloadError, match=r"entities\[0\]\.raw must be a list"):
            run(instance, {"entities": [entity("v60", raw="hario")]})

    def test_a_mistyped_alias_is_refused_by_position(self, instance):
        with pytest.raises(
            PlacementPayloadError, match=r"entities\[0\]\.raw\[1\] must be a non-empty"
        ):
            run(instance, {"entities": [entity("v60", raw=["hario", 3])]})

    def test_a_drop_that_is_not_an_object_is_refused(self, instance):
        with pytest.raises(PlacementPayloadError, match="drop must be an object"):
            run(instance, {"drop": ["brewing"]})

    def test_surrounding_whitespace_is_not_part_of_a_value(self, instance):
        run(instance, {"topics": [{"name": "  brewing  ", "description": "  d  "}]})
        assert taxonomy(instance)["topics"] == {"brewing": {"description": "d", "items": []}}

    def test_a_kebab_refusal_names_the_field(self, instance):
        with pytest.raises(
            PlacementPayloadError, match=r"topics\[0\]\.name: 'Bad Name' is not kebab-case"
        ):
            run(instance, {"topics": [topic("Bad Name")]})

    def test_an_entity_name_is_validated_like_a_topic_name(self, instance):
        with pytest.raises(
            PlacementPayloadError, match=r"entities\[0\]\.name: 'Bad Name' is not kebab-case"
        ):
            run(instance, {"entities": [entity("Bad Name")]})

    def test_a_mistyped_name_is_refused_by_field(self, instance):
        with pytest.raises(PlacementPayloadError, match=r"topics\[0\]\.name must be a non-empty"):
            run(instance, {"topics": [{"name": 3, "description": "d"}]})

    def test_a_mistyped_kind_is_refused_by_field(self, instance):
        with pytest.raises(PlacementPayloadError, match=r"entities\[0\]\.kind must be a non-empty"):
            run(instance, {"entities": [{"name": "v60", "kind": 3}]})

    @pytest.mark.parametrize("key", ["topics", "entities"])
    def test_a_record_name_list_that_is_not_a_list_is_refused(self, instance, key):
        with pytest.raises(
            PlacementPayloadError, match=rf"place\[0\]\.{key} must be a list of names"
        ):
            run(instance, {"place": [{"id": A, key: "brewing"}]})

    def test_a_bad_name_in_a_list_is_refused_by_position(self, instance):
        with pytest.raises(
            PlacementPayloadError, match=r"place\[0\]\.topics\[1\]: 'Bad Name' is not kebab-case"
        ):
            run(
                instance,
                {"topics": [topic("ok")], "place": [{"id": A, "topics": ["ok", "Bad Name"]}]},
            )

    def test_an_unplace_record_is_named_like_a_place_record(self, instance):
        with pytest.raises(
            PlacementPayloadError, match=r"unplace\[0\] names no topic and no entity"
        ):
            run(instance, {"unplace": [{"id": A}]})

    def test_a_missing_entity_key_is_refused_by_field(self, instance):
        with pytest.raises(
            PlacementPayloadError, match=r"entities\[0\]: missing required key\(s\) \['kind'\]"
        ):
            run(instance, {"entities": [{"name": "v60"}]})

    def test_an_unknown_drop_key_is_refused_by_name(self, instance):
        with pytest.raises(PlacementPayloadError, match=r"drop: unknown key\(s\) \['items'\]"):
            run(instance, {"drop": {"topics": ["x"], "items": []}})

    @pytest.mark.parametrize("key", ["topics", "entities"])
    def test_a_drop_list_that_is_not_a_list_is_refused(self, instance, key):
        with pytest.raises(PlacementPayloadError, match=rf"drop\.{key} must be a list of names"):
            run(instance, {"drop": {key: "brewing"}})


class TestRefusalWritesNothing:
    def test_a_late_bad_operation_voids_the_whole_payload(self, instance):
        seed(instance)
        taxonomy_before = instance.taxonomy_path.read_bytes()
        members_before = instance.entity_members_path.read_bytes()
        map_before = instance.map_path.read_bytes()
        with pytest.raises(PlacementPayloadError, match="does not exist"):
            run(
                instance,
                {
                    "topics": [topic("grinders")],
                    "place": [{"id": B, "topics": ["grinders"]}],
                    "drop": {"topics": ["ghost"]},
                },
            )
        assert instance.taxonomy_path.read_bytes() == taxonomy_before
        assert instance.entity_members_path.read_bytes() == members_before
        assert instance.map_path.read_bytes() == map_before

    def test_a_refused_bootstrap_writes_no_files_at_all(self, instance):
        with pytest.raises(PlacementPayloadError):
            run(instance, {"place": [{"id": A, "topics": ["ghost"]}]})
        assert not instance.taxonomy_path.exists()
        assert not instance.entity_members_path.exists()
        assert not instance.map_path.exists()

    def test_a_malformed_taxonomy_refuses_before_anything_is_judged(self, instance):
        instance.taxonomy_path.write_text("{not json")
        with pytest.raises(ValueError, match=r"taxonomy\.json: invalid JSON"):
            run(instance, {"topics": [topic("brewing")]})
        assert instance.taxonomy_path.read_text() == "{not json"

    def test_a_malformed_entity_members_file_refuses_too(self, instance):
        seed(instance)
        before = instance.taxonomy_path.read_bytes()
        instance.entity_members_path.write_text('{"v60": "not a list"}')
        with pytest.raises(ValueError, match="must map to a list of item-id strings"):
            run(instance, {"topics": [topic("grinders")]})
        assert instance.taxonomy_path.read_bytes() == before


class TestDeterminism:
    def test_equal_judgment_in_any_order_is_byte_identical(self, tmp_path):
        payloads = [
            {
                "topics": [topic("aaa"), topic("bbb")],
                "entities": [entity("v60")],
                "place": [
                    {"id": A, "topics": ["aaa", "bbb"], "entities": ["v60"]},
                    {"id": B, "topics": ["bbb"]},
                ],
            },
            {
                "topics": [topic("bbb"), topic("aaa")],
                "entities": [entity("v60")],
                "place": [
                    {"id": B, "topics": ["bbb"]},
                    {"id": A, "topics": ["bbb", "aaa"], "entities": ["v60"]},
                ],
            },
        ]
        written: list[tuple[bytes, bytes]] = []
        for i, payload in enumerate(payloads):
            root = tmp_path / str(i)
            (root / "cache").mkdir(parents=True)
            instance = Instance(root=root)
            run(instance, payload)
            written.append(
                (instance.taxonomy_path.read_bytes(), instance.entity_members_path.read_bytes())
            )
        assert written[0] == written[1]

    def test_the_canonical_byte_form_is_pinned(self, instance):
        """The exact bytes matter: the files diff cleanly and merge predictably."""
        run(
            instance,
            {
                "topics": [topic("brewing", "Brew technique.")],
                "entities": [entity("v60", "tool", ["hario-v60"])],
                "place": [{"id": A, "topics": ["brewing"], "entities": ["v60"]}],
            },
        )
        assert instance.taxonomy_path.read_text(encoding="utf-8") == (
            "{\n"
            '  "topics": {\n'
            '    "brewing": {\n'
            '      "description": "Brew technique.",\n'
            f'      "items": [\n        "{A}"\n      ]\n'
            "    }\n"
            "  },\n"
            '  "entities": {\n'
            '    "v60": {\n'
            '      "kind": "tool",\n'
            '      "raw": [\n        "hario-v60",\n        "v60"\n      ]\n'
            "    }\n"
            "  }\n"
            "}\n"
        )
        assert instance.entity_members_path.read_text(encoding="utf-8") == (
            f'{{\n  "v60": [\n    "{A}"\n  ]\n}}\n'
        )

    def test_a_hand_written_file_is_canonicalized_by_the_next_write(self, instance):
        # Unsorted names, duplicate ids, compact style: the rewrite settles
        # all of it to the one byte form.
        instance.taxonomy_path.write_text(
            json.dumps(
                {
                    "topics": {
                        "zzz": {"description": "z", "items": [B, A, B]},
                        "aaa": {"description": "a", "items": []},
                    },
                    "entities": {},
                }
            )
        )
        run(instance, {"topics": [topic("mmm")]})
        text = instance.taxonomy_path.read_text(encoding="utf-8")
        assert list(taxonomy(instance)["topics"]) == ["aaa", "mmm", "zzz"]
        assert taxonomy(instance)["topics"]["zzz"]["items"] == [A, B]
        assert text.startswith("{\n")

    def test_non_ascii_judgment_text_is_stored_readable(self, instance):
        run(instance, {"topics": [topic("brewing", "café burr — grinders")]})
        text = instance.taxonomy_path.read_text(encoding="utf-8")
        assert "café burr — grinders" in text
        assert "\\u" not in text


class TestSummary:
    def test_the_counts_tally_every_operation(self, instance):
        # Multiple changes per class, so the count is a running tally and
        # not a flag: the summary is the session's only signal for what a
        # bulk payload actually moved.
        seed(instance)
        summary = run(
            instance,
            {
                "topics": [topic("aaa"), topic("bbb")],
                "entities": [entity("v60"), entity("kalita")],
                "place": [
                    {"id": B, "topics": ["aaa", "bbb"], "entities": ["v60", "kalita"]},
                    {"id": A, "topics": ["aaa"]},
                ],
                "unplace": [
                    {"id": A, "topics": ["brewing"], "entities": ["hoffmann"]},
                    {"id": B, "topics": ["aaa"]},
                ],
                "drop": {"topics": ["brewing", "aaa"], "entities": ["hoffmann"]},
            },
        )
        assert "(2 topics defined, 2 entities defined, 5 placed, 3 unplaced, 3 dropped)" in summary


class TestMapTrigger:
    def test_a_successful_place_leaves_a_fresh_map(self, instance):
        seed(instance)
        assert instance.map_path.read_text(encoding="utf-8") == (
            serialize_map(compile_map(instance).payload)
        )
        assert "brewing" in json.loads(instance.map_path.read_text())["topics"]

    def test_a_stale_map_is_replaced(self, instance):
        instance.state_dir.mkdir(exist_ok=True)
        instance.map_path.write_text("stale")
        seed(instance)
        assert instance.map_path.read_text(encoding="utf-8") != "stale"

    def test_a_failed_recompile_reports_but_the_placement_stands(self, instance):
        # A real write failure: the map path is occupied by a directory, so
        # the atomic replace refuses. The state writes must survive and the
        # error must carry the summary — the work is not un-reported.
        instance.map_path.mkdir()
        with pytest.raises(ValueError, match="did not recompile") as excinfo:
            run(instance, {"topics": [topic("brewing")]})
        assert "wrote state/taxonomy.json + state/entity-members.json" in str(excinfo.value)
        assert "`bin/dex map` recompiles" in str(excinfo.value)
        assert "brewing" in taxonomy(instance)["topics"]
