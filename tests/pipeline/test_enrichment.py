"""Tests for the enrichment-file format: the readers' contracts."""

from pathlib import Path

import pytest

from dex_engine.capabilities import Capabilities
from dex_engine.pipeline.enrichment import (
    CAPTIONS_VIA,
    TRANSCRIPT_SOURCES,
    described_file,
    description_header,
    description_section,
    description_text,
    holds_transcript,
    pre_transcript,
    read_enrichment,
    read_enrichment_fields,
    split_transcript,
    transcript_provenance,
)
from dex_engine.pipeline.types import Config


class TestReadEnrichment:
    def test_round_trips_quoted_values(self, tmp_path):
        record = tmp_path / "podcast-abc123.md"
        record.write_text(
            '---\nurl: https://x.test\nfetched: 2026-08-20\ntitle: "Ep: one"\n'
            'enclosure: "https://cdn.test/a.mp3?sig=1"\n---\n\nnotes body\n'
        )
        fields, body = read_enrichment(record)
        assert fields["title"] == "Ep: one"
        assert fields["enclosure"] == "https://cdn.test/a.mp3?sig=1"
        assert body == "notes body"

    def test_frontmatterless_file_is_all_body(self, tmp_path):
        record = tmp_path / "x.md"
        record.write_text("just text\n")
        assert read_enrichment(record) == ({}, "just text")

    def test_an_unclosed_fence_yields_no_fields(self, tmp_path):
        # Every caller decides on a field it looks up, so transcript prose
        # read as frontmatter answers those lookups with nonsense: a stale
        # output survives a correction, a park reports a description it
        # never wrote. The sibling scan raises on this shape instead.
        record = tmp_path / "podcast-abc123.md"
        record.write_text(
            "---\nurl: https://x.test\nenclosure: https://cdn.test/a.mp3\n"
            "and then the transcript began\n"
        )
        fields, body = read_enrichment(record)
        assert fields == {}
        assert "transcript" in body


class TestReadEnrichmentFields:
    """The frontmatter-only read: same parse, none of the body."""

    def test_agrees_with_the_full_read(self, tmp_path):
        record = tmp_path / "podcast-abc123.md"
        record.write_text(
            '---\nurl: https://x.test\nfetched: 2026-08-20\ntitle: "Ep: one"\n---\n\nnotes body\n'
        )
        assert read_enrichment_fields(record) == read_enrichment(record)[0]

    def test_the_body_is_never_parsed_as_fields(self, tmp_path):
        record = tmp_path / "x-abc123.md"
        record.write_text('---\nurl: https://x.test\n---\n\nchain_incomplete: "true"\n')
        assert read_enrichment_fields(record) == {"url": "https://x.test"}

    def test_frontmatterless_file_has_no_fields(self, tmp_path):
        record = tmp_path / "x.md"
        record.write_text("just text\n")
        assert read_enrichment_fields(record) == {}

    def test_an_unterminated_fence_is_loud(self, tmp_path):
        # The shape an interrupted write leaves. Empty fields would read as
        # "opened fine, no markers" — indistinguishable from a clean file,
        # and lint's marker scan is the only reader an enrichment file has.
        record = tmp_path / "x.md"
        record.write_text("---\nurl: https://x.test\nand then the file just ends\n")
        with pytest.raises(ValueError, match="no closing '---' fence"):
            read_enrichment_fields(record)

    def test_the_file_is_never_slurped(self, tmp_path, monkeypatch):
        # The reason this function exists: enrichment bodies are whole
        # transcripts, and a marker scan reads thousands of them.
        record = tmp_path / "podcast-abc123.md"
        record.write_text("---\nurl: https://x.test\n---\n\n" + "transcript line\n" * 100_000)

        def refuse(*_args, **_kwargs):
            raise AssertionError("read_enrichment_fields must not read the whole file")

        monkeypatch.setattr(Path, "read_text", refuse)
        assert read_enrichment_fields(record) == {"url": "https://x.test"}


STAMPED = {"via": "whisper-local"}


class TestTranscriptSections:
    """One test says whether a body holds a transcript; the split and the keep share it."""

    @pytest.mark.parametrize(
        ("fields", "body", "holds"),
        [
            (STAMPED, "notes\n\n## Transcript\n\nwords", True),
            (STAMPED, "## Transcript\n\nwords", True),  # a no-notes episode opens with it
            (STAMPED, "## Transcript", True),
            (STAMPED, "notes, never transcribed", False),
            # A park is notes end to end, whatever headings its author wrote.
            ({}, "notes\n\n## Transcript\n\nthe author's own section", False),
            (STAMPED, "notes\n\n## Transcripts elsewhere\n\nprose", False),
        ],
    )
    def test_holds_transcript(self, fields, body, holds):
        assert holds_transcript(fields, body) is holds

    def test_the_notes_are_everything_before_the_transcript(self):
        assert pre_transcript(STAMPED, "notes\n\n## Transcript\n\nwords") == "notes"

    def test_the_split_takes_the_last_section_the_drain_appended(self):
        body = "notes\n\n## Transcript\n\nthe author's own\n\n## Transcript\n\nwords"
        assert pre_transcript(STAMPED, body) == "notes\n\n## Transcript\n\nthe author's own"

    def test_a_body_holding_no_transcript_is_all_notes(self):
        body = "notes\n\n## Transcript\n\nthe author's own section"
        assert pre_transcript({}, body) == body

    def test_a_body_opening_with_its_transcript_has_no_notes(self):
        assert pre_transcript(STAMPED, "## Transcript\n\nwords") == ""

    @pytest.mark.parametrize("source", sorted(TRANSCRIPT_SOURCES))
    def test_every_transcript_source_stamps_a_transcript(self, source):
        assert holds_transcript({"via": source}, "notes\n\n## Transcript\n\nwords")

    @pytest.mark.parametrize("via", ["fxtwitter", "wayback", "anydoc", "csv-builtin"])
    def test_fetch_provenance_with_a_transcript_heading_holds_none(self, via):
        # Other kinds' files carry `via` as the route they were fetched by,
        # and a fetched post may quote the heading in its own text.
        assert not holds_transcript({"via": via}, "the post\n\n## Transcript\n\nwords")

    def test_the_sources_are_every_transcriber_and_the_captions_route(self):
        # The one set, pinned to its writers both ways: a transcriber added
        # without joining it would land transcripts no rerun recognises.
        capabilities = Capabilities.build(Config())
        transcribers = {t.name for t in capabilities.transcribers}
        assert transcribers | {CAPTIONS_VIA} == TRANSCRIPT_SOURCES
        assert not TRANSCRIPT_SOURCES & {e.name for e in capabilities.extractors}

    def test_the_split_returns_both_halves(self):
        body = "notes\n\n## Transcript\n\nwords"
        assert split_transcript(STAMPED, body) == ("notes", "words")
        assert split_transcript(STAMPED, "## Transcript\n\nwords") == ("", "words")
        assert split_transcript({}, body) is None

    def test_provenance_is_the_transcriptions_own_stamps(self):
        fields = {"url": "u", "title": "t", "via": "whisper-api", "model": "m"}
        assert transcript_provenance(fields) == {"via": "whisper-api", "model": "m"}
        assert transcript_provenance({"via": CAPTIONS_VIA}) == {"via": CAPTIONS_VIA}

    def test_description_text_unframes_the_section(self):
        assert description_text(description_section("the description")) == "the description"
        assert description_text("show notes") == "show notes"


class TestDescribedFile:
    """A description's first line names the file it covers."""

    def test_the_header_names_the_file_it_opens(self, tmp_path):
        description = tmp_path / "media-0.md"
        description.write_text(f"{description_header('media-0.png')}\n\nA chart.\n")
        assert described_file(description) == "media-0.png"

    def test_a_one_line_description_with_no_newline_still_names_its_file(self, tmp_path):
        description = tmp_path / "media-0.md"
        description.write_text("Describes `media-0.png`", encoding="utf-8")
        assert described_file(description) == "media-0.png"

    def test_prose_after_the_name_is_ignored(self, tmp_path):
        # A description written before the verb carries its own words on.
        description = tmp_path / "media-0.md"
        description.write_text("Describes `media-0.png` — a 1.2MB chart\n\nText.\n")
        assert described_file(description) == "media-0.png"

    def test_only_the_first_line_is_read(self, tmp_path):
        description = tmp_path / "media-0.md"
        description.write_text("A chart.\nDescribes `media-0.png`\n")
        assert described_file(description) is None

    def test_an_unreadable_file_names_nothing(self, tmp_path):
        description = tmp_path / "media-0.md"
        description.write_bytes(b"\xff\xfe not text")
        assert described_file(description) is None
        assert described_file(tmp_path / "absent.md") is None
