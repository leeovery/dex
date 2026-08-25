"""Tests for corpus.py: the ONE corpus-item frontmatter read/write point."""

import dataclasses
import datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dex_engine.corpus import (
    CorpusItem,
    CorpusSchemaError,
    parse,
    read_item,
    serialize,
    write_item,
)
from dex_engine.pipeline.types import Instance

# A real-shaped item, canonical layout (the wild shape studied from a live
# instance's corpus).
WILD = """---
id: 2025-06-23-lemmy-apps-claude-trace-5c560e
source: gspace
channel: ai-coding
shared_by: Fabric user
date: 2025-06-23
urls:
  - https://github.com/badlogic/lemmy/tree/main/apps/claude-trace
kinds: [github]
status: enriched
enrichment: [github-9c70bf.md]
---

**Fabric user** (2025-06-23 16:13):
Claude Trace

https://github.com/badlogic/lemmy/tree/main/apps/claude-trace
"""


BASE_ITEM = CorpusItem(
    id="2026-08-19-example-55ad7b",
    source="inbox",
    channel="inbox",
    shared_by="Alex",
    date=datetime.date(2026, 8, 19),
    kinds=["web"],
    status="raw",
    body="\nA note.\n",
)


def item(**overrides: object) -> CorpusItem:
    return dataclasses.replace(BASE_ITEM, **overrides)


class TestParse:
    def test_wild_item_parses(self):
        parsed = parse(WILD)
        assert parsed.id == "2025-06-23-lemmy-apps-claude-trace-5c560e"
        assert parsed.source == "gspace"
        assert parsed.channel == "ai-coding"
        assert parsed.shared_by == "Fabric user"
        assert parsed.date == datetime.date(2025, 6, 23)
        assert parsed.urls == ["https://github.com/badlogic/lemmy/tree/main/apps/claude-trace"]
        assert parsed.kinds == ["github"]
        assert parsed.status == "enriched"
        assert parsed.enrichment == ["github-9c70bf.md"]
        assert parsed.reactions is None
        assert parsed.attachments == []
        assert parsed.media == []

    def test_body_is_byte_exact(self):
        assert parse(WILD).body == (
            "\n**Fabric user** (2025-06-23 16:13):\nClaude Trace\n\n"
            "https://github.com/badlogic/lemmy/tree/main/apps/claude-trace\n"
        )

    def test_canonical_text_round_trips_byte_exact(self):
        assert serialize(parse(WILD)) == WILD

    def test_pre_rename_kinds_still_parse(self):
        # Frontmatter kinds are provisional forever; migration 1 must be able
        # to read pre-rename vocabulary in order to rewrite it.
        text = WILD.replace("kinds: [github]", "kinds: [tweet, blog]")
        assert parse(text).kinds == ["tweet", "blog"]

    def test_optional_fields_parse(self):
        text = WILD.replace(
            "kinds: [github]",
            "kinds: [github]\nattachments:\n  - raw/gspace/File-Shot 2025-01-27.png\nreactions: 3",
        )
        parsed = parse(text)
        assert parsed.attachments == ["raw/gspace/File-Shot 2025-01-27.png"]
        assert parsed.reactions == 3

    def test_flow_empty_media_parses(self):
        text = WILD.replace(
            "enrichment: [github-9c70bf.md]", "enrichment: [github-9c70bf.md]\nmedia: []"
        )
        assert parse(text).media == []

    def test_block_form_accepted_for_flow_fields(self):
        text = WILD.replace("kinds: [github]", "kinds:\n  - github\n  - web")
        assert parse(text).kinds == ["github", "web"]

    def test_missing_opening_fence_is_loud(self):
        with pytest.raises(CorpusSchemaError, match="fence"):
            parse("id: x\n")

    def test_unterminated_frontmatter_is_loud(self):
        with pytest.raises(CorpusSchemaError, match="unterminated"):
            parse("---\nid: x\n")

    def test_unknown_key_is_loud(self):
        with pytest.raises(CorpusSchemaError, match="topics"):
            parse(WILD.replace("status: enriched", "topics: [ai]\nstatus: enriched"))

    def test_duplicate_key_is_loud(self):
        with pytest.raises(CorpusSchemaError, match="duplicate"):
            parse(WILD.replace("source: gspace", "source: gspace\nsource: inbox"))

    def test_missing_required_key_is_loud(self):
        with pytest.raises(CorpusSchemaError, match="shared_by"):
            parse(WILD.replace("shared_by: Fabric user\n", ""))

    def test_bad_date_is_loud(self):
        with pytest.raises(CorpusSchemaError, match="ISO"):
            parse(WILD.replace("date: 2025-06-23", "date: 23/06/2025"))

    def test_bad_reactions_is_loud(self):
        with pytest.raises(CorpusSchemaError, match="reactions"):
            parse(WILD.replace("status: enriched", "reactions: lots\nstatus: enriched"))

    def test_scalar_where_list_belongs_is_loud(self):
        with pytest.raises(CorpusSchemaError, match="kinds"):
            parse(WILD.replace("kinds: [github]", "kinds: github"))

    def test_valueless_scalar_is_loud(self):
        with pytest.raises(CorpusSchemaError, match="channel"):
            parse(WILD.replace("channel: ai-coding", "channel:"))

    def test_non_key_line_is_loud(self):
        with pytest.raises(CorpusSchemaError, match="key: value"):
            parse(WILD.replace("source: gspace", "source: gspace\n  - stray"))


class TestValidation:
    def test_bad_status_rejected(self):
        with pytest.raises(CorpusSchemaError, match="status"):
            item(status="digested")

    def test_empty_kinds_rejected(self):
        with pytest.raises(CorpusSchemaError, match="kinds"):
            item(kinds=[])

    def test_multiline_scalar_rejected(self):
        with pytest.raises(CorpusSchemaError, match="single line"):
            item(shared_by="Alex\nOwen")

    def test_unstripped_scalar_rejected(self):
        with pytest.raises(CorpusSchemaError, match="whitespace"):
            item(channel=" inbox")

    def test_flow_list_entry_with_comma_rejected(self):
        with pytest.raises(CorpusSchemaError, match="kinds"):
            item(kinds=["a,b"])

    def test_zero_reactions_rejected(self):
        with pytest.raises(CorpusSchemaError, match="reactions"):
            item(reactions=0)


class TestSerialize:
    def test_canonical_layout(self):
        text = serialize(
            item(
                urls=["https://example.test/a"],
                attachments=["raw/x/file.png"],
                reactions=2,
                media=["media/55ad7b/shot.png"],
                enrichment=["web-73bd78.md"],
                status="enriched",
            )
        )
        assert text == (
            "---\n"
            "id: 2026-08-19-example-55ad7b\n"
            "source: inbox\n"
            "channel: inbox\n"
            "shared_by: Alex\n"
            "date: 2026-08-19\n"
            "urls:\n"
            "  - https://example.test/a\n"
            "kinds: [web]\n"
            "attachments:\n"
            "  - raw/x/file.png\n"
            "reactions: 2\n"
            "status: enriched\n"
            "enrichment: [web-73bd78.md]\n"
            "media:\n"
            "  - media/55ad7b/shot.png\n"
            "---\n"
            "\nA note.\n"
        )

    def test_empty_optional_fields_are_omitted(self):
        text = serialize(item())
        assert "urls:" not in text
        assert "attachments:" not in text
        assert "reactions:" not in text
        assert "media:" not in text
        assert "enrichment: []" in text  # always emitted, like the wild shape


class TestFiles:
    def test_write_then_read(self, instance: Instance):
        path = instance.corpus_dir / "2026" / "2026-08-19-example-55ad7b.md"
        original = item(urls=["https://example.test"])
        write_item(path, original)
        assert read_item(path) == original

    def test_read_error_names_the_file(self, instance: Instance):
        path = instance.corpus_dir / "broken.md"
        path.write_text("no frontmatter here")
        with pytest.raises(CorpusSchemaError, match=r"broken\.md"):
            read_item(path)

    def test_write_item_is_atomic_and_cleans_its_temp_file(self, instance: Instance):
        path = instance.corpus_dir / "2026" / "2026-08-19-example-55ad7b.md"
        write_item(path, item())
        write_item(path, item(status="enriched", enrichment=["web-73bd78.md"]))
        assert [p.name for p in path.parent.iterdir()] == [path.name]
        assert read_item(path).status == "enriched"


# ---------------------------------------------------------------------------
# Hypothesis property: parse∘serialize over generated items preserves
# the body byte-exact and the frontmatter semantically — the guarantee that
# lets migration 1 and the refresh path touch thousands of Claude-authored
# files safely.
# ---------------------------------------------------------------------------

_scalar = (
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n"),
        min_size=1,
        max_size=40,
    )
    .map(str.strip)
    .filter(bool)
)
_flow_entry = (
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n,[]"),
        min_size=1,
        max_size=30,
    )
    .map(str.strip)
    .filter(bool)
)
_body = st.text(max_size=400)


@st.composite
def corpus_items(draw: st.DrawFn) -> CorpusItem:
    return CorpusItem(
        id=draw(_scalar),
        source=draw(_scalar),
        channel=draw(_scalar),
        shared_by=draw(_scalar),
        date=draw(st.dates()),
        urls=draw(st.lists(_scalar, max_size=4)),
        kinds=draw(st.lists(_flow_entry, min_size=1, max_size=3)),
        reactions=draw(st.none() | st.integers(min_value=1, max_value=99)),
        attachments=draw(st.lists(_scalar, max_size=3)),
        status=draw(st.sampled_from(["raw", "enriched"])),
        enrichment=draw(st.lists(_flow_entry, max_size=3)),
        media=draw(st.lists(_scalar, max_size=3)),
        body=draw(_body),
    )


@given(original=corpus_items())
def test_parse_serialize_round_trip(original: CorpusItem):
    parsed = parse(serialize(original))
    assert parsed.body == original.body  # byte-exact
    assert parsed == original  # frontmatter semantically identical


@given(original=corpus_items())
def test_serialization_is_stable(original: CorpusItem):
    once = serialize(original)
    assert serialize(parse(once)) == once
