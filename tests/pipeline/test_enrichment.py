"""Tests for the enrichment-file format: the readers' contracts."""

from pathlib import Path

import pytest

from dex_engine.pipeline.enrichment import (
    described_file,
    description_header,
    read_enrichment,
    read_enrichment_fields,
)


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
