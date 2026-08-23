"""Tests for pipeline/digest.py: `enrich item digest` — the digest writer."""

import datetime
import json

import pytest

from dex_engine import corpus
from dex_engine.pipeline.digest import DigestPayloadError, item_digest
from tests.test_lint import lint, write_index, write_taxonomy

ITEM = "2026-08-19-example-55ad7b"

PAYLOAD = {
    "id": ITEM,
    "signal": "high",
    "topics": ["agentic-engineering", "claude"],
    "facts": [
        "Ships a subagent API that runs tools in parallel.",
        "Benchmarks show 30% fewer tokens.",
    ],
}

SHARED_ON = datetime.date(2026, 8, 19)


def write_item(instance, item_id=ITEM, *, date=SHARED_ON, media=None) -> None:
    path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    corpus.write_item(
        path,
        corpus.CorpusItem(
            id=item_id,
            source="inbox",
            channel="inbox",
            shared_by="owner",
            date=date,
            kinds=["web"],
            media=media or [],
            body="why I saved it\n",
        ),
    )


def digest(instance, payload=None, **overrides) -> str:
    path = instance.cache_dir / "digest.json"
    body = {**PAYLOAD, **overrides} if payload is None else payload
    path.write_text(json.dumps(body) if not isinstance(body, str) else body)
    return item_digest(path, instance=instance)


def digest_text(instance, item_id: str = ITEM) -> str:
    return (instance.digests_dir / f"{item_id}.md").read_text()


class TestWrite:
    def test_writes_the_canonical_digest(self, instance):
        write_item(instance)
        assert digest(instance) == f"wrote state/digests/{ITEM}.md"
        assert digest_text(instance) == (
            f"---\nid: {ITEM}\ndate: 2026-08-19\nsignal: high\n"
            "topics: [agentic-engineering, claude]\n---\n"
            "- Ships a subagent API that runs tools in parallel.\n"
            "- Benchmarks show 30% fewer tokens.\n"
        )

    def test_the_date_is_the_items_share_date_not_the_ids_prefix(self, instance):
        # The id encodes the date it was minted from; the corpus item holds
        # the date itself, and that is the one the digest states.
        write_item(instance, date=datetime.date(2026, 8, 17))
        digest(instance)
        assert "\ndate: 2026-08-17\n" in digest_text(instance)

    def test_entities_are_emitted_when_the_payload_names_them(self, instance):
        write_item(instance)
        digest(instance, entities=["claude-code", "anthropic"])
        assert "\nentities: [claude-code, anthropic]\n" in digest_text(instance)

    def test_entities_are_omitted_when_the_payload_names_none(self, instance):
        write_item(instance)
        digest(instance)
        assert "entities" not in digest_text(instance)

    def test_media_comes_from_the_corpus_item(self, instance):
        # Block form: a media path ends in a filed asset's own filename,
        # which may hold a comma a flow list could not carry.
        write_item(instance, media=["media/55ad7b/photo, framed.jpg"])
        digest(instance)
        assert "\nmedia:\n  - media/55ad7b/photo, framed.jpg\n---\n" in digest_text(instance)

    def test_an_item_with_no_media_gets_no_media_key(self, instance):
        write_item(instance)
        digest(instance)
        assert "media" not in digest_text(instance)

    def test_surrounding_whitespace_is_not_part_of_a_value(self, instance):
        write_item(instance)
        digest(instance, topics=["  brewing  "], facts=["  a fact.  "])
        assert "\ntopics: [brewing]\n" in digest_text(instance)
        assert digest_text(instance).endswith("\n- a fact.\n")

    def test_the_write_creates_the_digests_directory(self, instance):
        write_item(instance)
        assert not instance.digests_dir.exists()
        digest(instance)
        assert (instance.digests_dir / f"{ITEM}.md").is_file()


class TestRewrite:
    """A session revising its judgment writes the payload again."""

    def test_a_rewrite_says_so_and_replaces_every_field(self, instance):
        write_item(instance)
        digest(instance, entities=["claude-code"])
        # Nothing carries over: each field is the payload's judgment or the
        # corpus item's fact, so the second write is a full re-derivation.
        assert digest(instance, signal="low", topics=["brewing"], facts=["one fact."]) == (
            f"rewrote state/digests/{ITEM}.md"
        )
        assert digest_text(instance) == (
            f"---\nid: {ITEM}\ndate: 2026-08-19\nsignal: low\ntopics: [brewing]\n---\n- one fact.\n"
        )

    def test_a_refused_rewrite_leaves_the_standing_digest_alone(self, instance):
        write_item(instance)
        digest(instance)
        before = digest_text(instance)
        with pytest.raises(DigestPayloadError):
            digest(instance, signal="urgent")
        assert digest_text(instance) == before


class TestPayloadValidation:
    def test_a_bogus_signal_is_refused(self, instance):
        write_item(instance)
        with pytest.raises(DigestPayloadError, match="signal must be one of high, medium, low"):
            digest(instance, signal="urgent")

    def test_empty_topics_are_refused(self, instance):
        write_item(instance)
        with pytest.raises(DigestPayloadError, match="topics is empty"):
            digest(instance, topics=[])

    def test_empty_facts_are_refused(self, instance):
        write_item(instance)
        with pytest.raises(DigestPayloadError, match="facts is empty"):
            digest(instance, facts=[])

    @pytest.mark.parametrize("key", ["id", "signal", "topics", "facts"])
    def test_a_missing_key_is_refused_by_name(self, instance, key):
        write_item(instance)
        payload = {k: v for k, v in PAYLOAD.items() if k != key}
        with pytest.raises(DigestPayloadError, match=rf"missing required key\(s\) \['{key}'\]"):
            digest(instance, payload)

    @pytest.mark.parametrize("key", ["date", "media"])
    def test_an_engine_owned_key_is_refused_rather_than_trusted(self, instance, key):
        # The item's own date and media are facts the corpus item states;
        # a payload naming either is guessing at one of them.
        write_item(instance)
        with pytest.raises(DigestPayloadError, match="is the engine's to write"):
            digest(instance, **{key: "2020-01-01"})

    def test_an_unknown_key_is_refused(self, instance):
        write_item(instance)
        with pytest.raises(DigestPayloadError, match=r"unknown key\(s\) \['note'\]"):
            digest(instance, note="a stray key")

    @pytest.mark.parametrize("value", [3, None, "", "   ", ["a"], "two\nlines"])
    def test_a_mistyped_scalar_is_refused(self, instance, value):
        write_item(instance)
        with pytest.raises(DigestPayloadError, match="signal must be a non-empty single-line"):
            digest(instance, signal=value)

    @pytest.mark.parametrize("value", ["brewing", 3, {"a": 1}])
    def test_topics_that_are_not_a_list_are_refused(self, instance, value):
        write_item(instance)
        with pytest.raises(DigestPayloadError, match="topics must be a list of names"):
            digest(instance, topics=value)

    @pytest.mark.parametrize("value", [3, None, "", "  ", "two\nlines"])
    def test_a_mistyped_topic_is_refused_by_position(self, instance, value):
        write_item(instance)
        with pytest.raises(DigestPayloadError, match=r"topics\[1\] must be a non-empty"):
            digest(instance, topics=["brewing", value])

    @pytest.mark.parametrize("name", ["a,b", "a[b", "a]b"])
    def test_a_topic_that_would_break_the_flow_list_is_refused(self, instance, name):
        # The list style is the engine's decision, so a name that cannot
        # survive it is refused rather than quoted around.
        write_item(instance)
        with pytest.raises(DigestPayloadError, match=r"topics\[0\] may not contain"):
            digest(instance, topics=[name])

    @pytest.mark.parametrize("value", ["a fact.", 3, {"a": 1}])
    def test_facts_that_are_not_a_list_are_refused(self, instance, value):
        write_item(instance)
        with pytest.raises(DigestPayloadError, match="facts must be a list of strings"):
            digest(instance, facts=value)

    @pytest.mark.parametrize("value", [3, None, "", "  ", "two\nlines"])
    def test_a_mistyped_fact_is_refused_by_position(self, instance, value):
        write_item(instance)
        with pytest.raises(DigestPayloadError, match=r"facts\[1\] must be a non-empty"):
            digest(instance, facts=["a fact.", value])

    def test_invalid_json_is_refused(self, instance):
        write_item(instance)
        with pytest.raises(DigestPayloadError, match="invalid JSON"):
            digest(instance, "not json at all")

    @pytest.mark.parametrize("text", ["[]", '"a string"', "3"])
    def test_a_payload_that_is_not_an_object_is_refused(self, instance, text):
        write_item(instance)
        with pytest.raises(DigestPayloadError, match="expected a JSON object"):
            digest(instance, text)


class TestTheItemMustExist:
    def test_an_id_naming_no_corpus_item_is_refused(self, instance):
        with pytest.raises(DigestPayloadError, match="is not a corpus item in this instance"):
            digest(instance)
        assert not instance.digests_dir.exists()

    def test_an_id_shaped_like_a_path_never_reaches_the_filesystem(self, instance):
        with pytest.raises(DigestPayloadError, match="is not a corpus item id"):
            digest(instance, id="../../elsewhere")

    def test_an_unparseable_corpus_item_is_refused(self, instance):
        path = instance.corpus_dir / ITEM[:4] / f"{ITEM}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("no frontmatter here\n")
        with pytest.raises(DigestPayloadError, match="does not parse"):
            digest(instance)

    def test_a_corpus_item_declaring_another_id_is_refused(self, instance):
        # Filed under one id and claiming another, the digest would be
        # unresolvable either way — lint's own id check, made impossible.
        write_item(instance)
        path = instance.corpus_dir / ITEM[:4] / f"{ITEM}.md"
        path.write_text(path.read_text().replace(f"id: {ITEM}", "id: 2026-08-19-other-999999"))
        with pytest.raises(DigestPayloadError, match="declares id '2026-08-19-other-999999'"):
            digest(instance)


class TestLintAgrees:
    """A digest that went through the verb cannot fail the health check."""

    def test_a_verb_written_digest_passes_the_real_lint(self, instance):
        write_taxonomy(instance)
        write_index(instance, "")
        write_item(instance, media=["media/55ad7b/photo.jpg"])
        digest(instance, entities=["claude-code"])
        outcome = lint(instance)
        assert "MALFORMED DIGESTS (the wiki layer reads these) — none" in outcome.report
        assert outcome.exit_code == 0

    def test_a_single_fact_digest_passes_the_real_lint(self, instance):
        write_taxonomy(instance)
        write_index(instance, "")
        write_item(instance)
        digest(instance, facts=["The one fact a two-line tweet yields."])
        outcome = lint(instance)
        assert "MALFORMED DIGESTS (the wiki layer reads these) — none" in outcome.report
        assert outcome.exit_code == 0
