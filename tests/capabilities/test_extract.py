"""Tests for the extract providers: real anydoc over fixtures, csv-builtin, floors."""

import pytest

from dex_engine.capabilities.extract.anydoc import AnydocExtractor
from dex_engine.capabilities.extract.cognitive import CognitiveExtractor
from dex_engine.capabilities.extract.csv_builtin import CsvBuiltinExtractor
from dex_engine.capabilities.ocr.cognitive import CognitiveOcr
from dex_engine.pipeline.classify import ProviderInputError, ScannedDocumentError
from dex_engine.pipeline.types import Format
from tests.capabilities.conftest import fixture_bytes


class TestAnydoc:
    """Real anydoc over tiny committed fixtures — local, no network."""

    def test_supports_every_format(self):
        extractor = AnydocExtractor()
        assert all(extractor.supports(fmt) for fmt in Format)

    def test_available_on_this_machine(self):
        # The wheel is a core dependency; a red bar here means the platform
        # verification regressed.
        assert AnydocExtractor().available().ok is True

    def test_docx_extracts_markdown_and_embedded_asset_bytes(self):
        extraction = AnydocExtractor().extract(fixture_bytes("report.docx"), Format.DOCX)
        assert "Intro text before the figure." in extraction.markdown
        assert len(extraction.assets) == 1
        asset = extraction.assets[0]
        assert asset.suggested_ext == "png"
        assert asset.data.startswith(b"\x89PNG")  # bytes — the only possible form

    def test_pdf_extracts_text_with_no_assets(self):
        # PDF has no document-model form in anydoc: text-only degradation
        # is the contract, not a bug.
        extraction = AnydocExtractor().extract(fixture_bytes("paper.pdf"), Format.PDF)
        assert "Hello PDF fixture" in extraction.markdown
        assert extraction.assets == []

    def test_scanned_pdf_takes_the_ocr_path(self):
        with pytest.raises(ScannedDocumentError, match="OCR"):
            AnydocExtractor().extract(fixture_bytes("scanned.pdf"), Format.PDF)

    def test_csv_with_format_hint_extracts_a_table(self):
        extraction = AnydocExtractor().extract(fixture_bytes("stars.csv"), Format.CSV)
        assert "| name | stars | license |" in extraction.markdown

    def test_garbage_bytes_are_bad_input_never_a_crash(self):
        with pytest.raises(ProviderInputError, match="anydoc"):
            AnydocExtractor().extract(b"\x00\x01 not a document", Format.DOCX)


class TestCsvBuiltin:
    def test_supports_csv_only(self):
        extractor = CsvBuiltinExtractor()
        assert extractor.supports(Format.CSV)
        assert not any(extractor.supports(fmt) for fmt in Format if fmt is not Format.CSV)

    def test_always_available(self):
        assert CsvBuiltinExtractor().available().ok is True

    def test_renders_a_markdown_table(self):
        extraction = CsvBuiltinExtractor().extract(fixture_bytes("stars.csv"), Format.CSV)
        assert extraction.markdown.startswith("| name | stars | license |\n| --- | --- | --- |\n")
        assert "| anydoc | 120 | MIT |" in extraction.markdown
        assert extraction.assets == []

    def test_ragged_rows_are_padded_and_pipes_escaped(self):
        data = b"a,b\n1\nx,y|z,extra\n"
        markdown = CsvBuiltinExtractor().extract(data, Format.CSV).markdown
        lines = markdown.strip().split("\n")
        assert lines[2] == "| 1 |  |  |"
        assert "y\\|z" in lines[3]

    def test_semicolon_delimiter_is_sniffed(self):
        markdown = CsvBuiltinExtractor().extract(b"a;b\n1;2\n", Format.CSV).markdown
        assert markdown.startswith("| a | b |")

    def test_empty_csv_is_bad_input(self):
        with pytest.raises(ProviderInputError, match="no rows"):
            CsvBuiltinExtractor().extract(b"", Format.CSV)

    def test_non_csv_work_is_refused(self):
        with pytest.raises(ProviderInputError, match="csv-builtin"):
            CsvBuiltinExtractor().extract(b"%PDF-1.4", Format.PDF)


class TestCognitiveFloors:
    @pytest.mark.parametrize("floor", [CognitiveExtractor(), CognitiveOcr()])
    def test_always_available_supports_everything(self, floor):
        assert floor.name == "cognitive"
        assert floor.available().ok is True
        assert all(floor.supports(fmt) for fmt in Format)

    @pytest.mark.parametrize("floor", [CognitiveExtractor(), CognitiveOcr()])
    def test_mechanical_drain_is_an_engine_bug(self, floor):
        # enrich run never "drains" the cognitive provider — the session
        # does these jobs with eyes. A mechanical call is a loud bug.
        with pytest.raises(RuntimeError, match="never drained mechanically"):
            floor.extract(b"data", Format.PDF)
