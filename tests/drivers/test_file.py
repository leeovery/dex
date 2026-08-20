"""Tests for the file driver: format routing, honest parking, asset passthrough."""

import pytest

from dex_engine.capabilities import Capabilities
from dex_engine.drivers.file import FileDriver
from dex_engine.drivers.transport import HttpResponse
from dex_engine.pipeline.classify import ProviderInputError
from dex_engine.pipeline.types import Config, Format, Kind, Need, Status
from tests.capabilities.conftest import FakeExtractor, fixture_bytes
from tests.drivers.conftest import FakeTransport, make_unit, reason_of

PDF_URL = "https://example.test/whitepaper"


def caps(*extractors) -> Capabilities:
    return Capabilities(transcribers=(), extractors=tuple(extractors))


class TestLocalFiles:
    def test_matches_and_canonical_for_file_keys(self):
        d = FileDriver(capabilities=caps())
        assert d.matches("file:media/abc123/report.docx")
        assert not d.matches("https://example.test/report.docx")
        assert d.canonical("file:media/abc123/report.docx") == "file:media/abc123/report.docx"

    def test_real_docx_extracts_through_real_anydoc(self, tmp_path):
        # Real anydoc over a committed fixture — local, no network.
        media = tmp_path / "media" / "abc123"
        media.mkdir(parents=True)
        (media / "report.docx").write_bytes(fixture_bytes("report.docx"))
        d = FileDriver(capabilities=Capabilities.build(Config()), root=tmp_path)
        result = d.fetch(make_unit("file:media/abc123/report.docx", Kind.FILE))
        assert result.status is Status.DONE
        assert result.body is not None
        assert "Intro text before the figure." in result.body
        assert result.meta == {"title": "report.docx", "format": "docx", "via": "anydoc"}
        assert len(result.assets) == 1
        assert result.assets[0].suggested_ext == "png"

    def test_byte_sniff_is_authoritative_over_the_recorded_format(self, tmp_path):
        # A unit whose ledger format is stale still routes by what the bytes
        # ARE (byte-signature sniff, authoritative).
        (tmp_path / "doc.bin").write_bytes(fixture_bytes("paper.pdf"))
        extractor = FakeExtractor()
        d = FileDriver(capabilities=caps(extractor), root=tmp_path)
        result = d.fetch(make_unit("file:doc.bin", Kind.FILE, fmt=Format.DOCX))
        assert result.status is Status.DONE
        assert extractor.calls[0][1] is Format.PDF

    def test_missing_file_is_manual_with_the_pull_hint(self, tmp_path):
        d = FileDriver(capabilities=caps(FakeExtractor()), root=tmp_path)
        result = d.fetch(make_unit("file:media/gone/doc.pdf", Kind.FILE))
        assert result.status is Status.MANUAL
        assert "not in the working tree" in reason_of(result)

    def test_unsmudged_lfs_pointer_is_manual(self, tmp_path):
        (tmp_path / "deck.pptx").write_bytes(
            b"version https://git-lfs.github.com/spec/v1\noid sha256:ab\nsize 9\n"
        )
        d = FileDriver(capabilities=caps(FakeExtractor()), root=tmp_path)
        result = d.fetch(make_unit("file:deck.pptx", Kind.FILE))
        assert result.status is Status.MANUAL
        assert "git lfs pull" in reason_of(result)

    def test_unrecognized_bytes_are_manual(self, tmp_path):
        (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0 jpeg bytes")
        d = FileDriver(capabilities=caps(FakeExtractor()), root=tmp_path)
        result = d.fetch(make_unit("file:photo.jpg", Kind.FILE))
        assert result.status is Status.MANUAL
        assert "unrecognized file format" in reason_of(result)

    def test_rootless_local_work_is_an_engine_bug(self):
        d = FileDriver(capabilities=caps(FakeExtractor()))
        with pytest.raises(RuntimeError, match="instance root"):
            d.fetch(make_unit("file:media/abc/doc.pdf", Kind.FILE))


class TestUrlServedBinaries:
    def test_downloads_sniffs_and_extracts(self):
        transport = FakeTransport(
            {
                PDF_URL: HttpResponse(
                    status=200, content_type="application/pdf", body=fixture_bytes("paper.pdf")
                )
            }
        )
        extractor = FakeExtractor()
        d = FileDriver(capabilities=caps(extractor), transport=transport)
        result = d.fetch(make_unit(PDF_URL, Kind.FILE, fmt=Format.PDF))
        assert result.status is Status.DONE
        assert extractor.calls[0][1] is Format.PDF
        assert result.meta["title"] == "whitepaper"

    def test_http_failures_classify(self):
        transport = FakeTransport(
            {PDF_URL: HttpResponse(status=403, content_type="text/html", body=b"")}
        )
        d = FileDriver(capabilities=caps(FakeExtractor()), transport=transport)
        result = d.fetch(make_unit(PDF_URL, Kind.FILE, fmt=Format.PDF))
        assert result.status is Status.BLOCKED  # the regression pin's class: never dead
        assert reason_of(result) == "HTTP 403"

    def test_connection_failures_classify(self):
        transport = FakeTransport({PDF_URL: OSError("connection refused")})
        d = FileDriver(capabilities=caps(FakeExtractor()), transport=transport)
        result = d.fetch(make_unit(PDF_URL, Kind.FILE, fmt=Format.PDF))
        assert result.status is Status.BLOCKED

    def test_server_lied_bytes_still_route_by_signature(self):
        # HEAD said PDF; the body is a docx — the sniff corrects the route.
        transport = FakeTransport(
            {
                PDF_URL: HttpResponse(
                    status=200, content_type="application/pdf", body=fixture_bytes("report.docx")
                )
            }
        )
        extractor = FakeExtractor()
        d = FileDriver(capabilities=caps(extractor), transport=transport)
        result = d.fetch(make_unit(PDF_URL, Kind.FILE, fmt=Format.PDF))
        assert result.status is Status.DONE
        assert extractor.calls[0][1] is Format.DOCX


class TestRedetection:
    HTML = b"<!DOCTYPE html>\n<html><body>a page pretending to be a paper</body></html>"

    def test_claimed_pdf_serving_html_redetects_to_web(self):
        transport = FakeTransport(
            {PDF_URL: HttpResponse(status=200, content_type="text/html", body=self.HTML)}
        )
        d = FileDriver(capabilities=caps(FakeExtractor()), transport=transport)
        result = d.fetch(make_unit(PDF_URL, Kind.FILE, fmt=Format.PDF))
        assert result.status is Status.QUEUED
        assert result.redetect is not None
        assert result.redetect.kind is Kind.WEB
        assert result.redetect.format is None

    def test_html_lead_bytes_redetect_even_with_a_lying_content_type(self):
        transport = FakeTransport(
            {
                PDF_URL: HttpResponse(
                    status=200, content_type="application/pdf", body=self.HTML
                )
            }
        )
        d = FileDriver(capabilities=caps(FakeExtractor()), transport=transport)
        result = d.fetch(make_unit(PDF_URL, Kind.FILE, fmt=Format.PDF))
        assert result.redetect is not None
        assert result.redetect.kind is Kind.WEB

    def test_real_document_magic_beats_an_html_content_type(self):
        # Magic bytes are authoritative in BOTH directions: a real PDF served
        # as text/html extracts here, never bounces back to web.
        transport = FakeTransport(
            {
                PDF_URL: HttpResponse(
                    status=200, content_type="text/html", body=fixture_bytes("paper.pdf")
                )
            }
        )
        extractor = FakeExtractor()
        d = FileDriver(capabilities=caps(extractor), transport=transport)
        result = d.fetch(make_unit(PDF_URL, Kind.FILE, fmt=Format.PDF))
        assert result.redetect is None
        assert result.status is Status.DONE

    def test_local_files_never_redetect(self, tmp_path):
        # A captured HTML file is not a page to fetch — local work parks
        # honestly instead.
        media = tmp_path / "media" / "abc123"
        media.mkdir(parents=True)
        (media / "saved.bin").write_bytes(self.HTML)
        d = FileDriver(capabilities=caps(FakeExtractor()), root=tmp_path)
        result = d.fetch(make_unit("file:media/abc123/saved.bin", Kind.FILE))
        assert result.redetect is None
        assert result.status is Status.MANUAL


class TestExtractRouting:
    def test_no_provider_for_the_format_parks_waiting_with_the_reason(self, tmp_path):
        (tmp_path / "doc.pdf").write_bytes(fixture_bytes("paper.pdf"))
        csv_only = FakeExtractor("csv-builtin", formats=frozenset({Format.CSV}))
        d = FileDriver(capabilities=caps(csv_only), root=tmp_path)
        result = d.fetch(make_unit("file:doc.pdf", Kind.FILE))
        assert result.status is Status.WAITING
        assert result.needs is Need.EXTRACT
        assert "cognitive floor" in reason_of(result)

    def test_unavailable_provider_parks_waiting_with_its_reason(self, tmp_path):
        (tmp_path / "doc.pdf").write_bytes(fixture_bytes("paper.pdf"))
        broken = FakeExtractor("anydoc", ok=False, reason="wheel broken on this platform")
        d = FileDriver(capabilities=caps(broken), root=tmp_path)
        result = d.fetch(make_unit("file:doc.pdf", Kind.FILE))
        assert result.status is Status.WAITING
        assert result.needs is Need.EXTRACT
        assert "wheel broken" in reason_of(result)

    def test_scanned_document_takes_the_ocr_path(self, tmp_path):
        (tmp_path / "scan.pdf").write_bytes(fixture_bytes("scanned.pdf"))
        d = FileDriver(capabilities=Capabilities.build(Config()), root=tmp_path)
        result = d.fetch(make_unit("file:scan.pdf", Kind.FILE))
        assert result.status is Status.WAITING
        assert result.needs is Need.OCR
        assert "OCR" in reason_of(result)

    def test_provider_input_errors_propagate_for_the_run_loop(self, tmp_path):
        # The driver never swallows bad-input raises: the run loop owns the
        # ProviderInputError → manual mapping.
        (tmp_path / "doc.pdf").write_bytes(fixture_bytes("paper.pdf"))
        angry = FakeExtractor(raise_=ProviderInputError("encrypted document"))
        d = FileDriver(capabilities=caps(angry), root=tmp_path)
        with pytest.raises(ProviderInputError, match="encrypted"):
            d.fetch(make_unit("file:doc.pdf", Kind.FILE))

    def test_config_order_decides_which_extractor_wins(self, tmp_path):
        (tmp_path / "stars.csv").write_bytes(fixture_bytes("stars.csv"))
        second = FakeExtractor("anydoc")
        first = FakeExtractor("csv-builtin", formats=frozenset({Format.CSV}))
        d = FileDriver(capabilities=caps(first, second), root=tmp_path)
        result = d.fetch(make_unit("file:stars.csv", Kind.FILE))
        assert result.status is Status.DONE
        assert first.calls
        assert not second.calls
