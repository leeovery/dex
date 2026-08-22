"""Tests for the file driver: format routing, honest parking, asset passthrough."""

import json

import pytest

from dex_engine.capabilities import Capabilities
from dex_engine.drivers.file import FileDriver
from dex_engine.drivers.transport import HttpResponse
from dex_engine.pipeline.classify import ProviderInputError, ProviderUnavailableError
from dex_engine.pipeline.types import Config, Format, Kind, Need, Status
from tests.capabilities.conftest import FakeExtractor, fixture_bytes
from tests.drivers.conftest import (
    FakeGh,
    FakeTransport,
    gh_contents,
    gh_fail,
    gh_matching_refs,
    gh_ok,
    make_unit,
    reason_of,
)

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


class TestPathContainment:
    """Repo paths are owner-editable frontmatter: data, never trusted as paths."""

    def _driver(self, root) -> FileDriver:
        return FileDriver(capabilities=caps(FakeExtractor()), root=root)

    def test_absolute_path_is_manual_never_read(self, tmp_path):
        root = tmp_path / "instance"
        root.mkdir()
        outside = tmp_path / "secret.pdf"
        outside.write_bytes(fixture_bytes("paper.pdf"))
        result = self._driver(root).fetch(make_unit(f"file:{outside}", Kind.FILE))
        assert result.status is Status.MANUAL
        assert "outside the instance root" in reason_of(result)

    def test_dotdot_escape_is_manual_never_read(self, tmp_path):
        root = tmp_path / "instance"
        root.mkdir()
        (tmp_path / "secret.pdf").write_bytes(fixture_bytes("paper.pdf"))
        result = self._driver(root).fetch(make_unit("file:../secret.pdf", Kind.FILE))
        assert result.status is Status.MANUAL
        assert "outside the instance root" in reason_of(result)

    def test_symlink_pointing_outside_the_root_is_manual_never_read(self, tmp_path):
        root = tmp_path / "instance"
        media = root / "media"
        media.mkdir(parents=True)
        outside = tmp_path / "secret.pdf"
        outside.write_bytes(fixture_bytes("paper.pdf"))
        (media / "doc.pdf").symlink_to(outside)
        result = self._driver(root).fetch(make_unit("file:media/doc.pdf", Kind.FILE))
        assert result.status is Status.MANUAL
        assert "outside the instance root" in reason_of(result)

    def test_nested_path_staying_inside_the_root_keeps_working(self, tmp_path):
        media = tmp_path / "media" / "abc123"
        media.mkdir(parents=True)
        (media / "doc.pdf").write_bytes(fixture_bytes("paper.pdf"))
        # Dot segments that RESOLVE inside the root are legitimate.
        unit = make_unit("file:media/abc123/../abc123/doc.pdf", Kind.FILE)
        result = self._driver(tmp_path).fetch(unit)
        assert result.status is Status.DONE


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


class TestGithubBlobs:
    """Repo-committed documents: bytes through gh, never the viewer page."""

    URL = "https://github.com/acme/pipeline-kit/blob/main/docs/whitepaper.pdf"
    CONTENTS = ("api", "repos/acme/pipeline-kit/contents/docs/whitepaper.pdf?ref=main")

    def refuse_transport(self, url, *, method="GET"):
        raise AssertionError(f"a blob URL must never be fetched over HTTP ({method} {url})")

    def test_a_blob_pdf_extracts_through_the_gh_seam(self):
        gh = FakeGh({self.CONTENTS: gh_contents(fixture_bytes("paper.pdf"))})
        extractor = FakeExtractor()
        d = FileDriver(capabilities=caps(extractor), transport=self.refuse_transport, gh=gh)
        result = d.fetch(make_unit(self.URL, Kind.FILE, fmt=Format.PDF))
        assert result.status is Status.DONE
        assert extractor.calls[0][1] is Format.PDF
        assert result.meta["title"] == "whitepaper.pdf"

    def test_a_private_repo_403_classifies_blocked_not_dead(self):
        gh = FakeGh({self.CONTENTS: gh_fail("gh: API rate limit exceeded (HTTP 403)")})
        d = FileDriver(capabilities=caps(FakeExtractor()), transport=self.refuse_transport, gh=gh)
        result = d.fetch(make_unit(self.URL, Kind.FILE, fmt=Format.PDF))
        assert result.status is Status.BLOCKED

    def test_an_oversize_blob_is_manual_with_the_clone_route(self):
        gh = FakeGh({self.CONTENTS: gh_ok(json.dumps({"encoding": "none", "content": ""}))})
        d = FileDriver(capabilities=caps(FakeExtractor()), transport=self.refuse_transport, gh=gh)
        result = d.fetch(make_unit(self.URL, Kind.FILE, fmt=Format.PDF))
        assert result.status is Status.MANUAL
        assert "read it from a clone" in reason_of(result)

    def test_a_slashed_branch_resolves_on_the_way_back_in(self):
        # The github driver hands this URL over verbatim after re-detection,
        # so the file driver has to settle the same ref/path boundary. Split
        # at the first segment it would 404 — and a 404 on contents is
        # terminally dead, which is the whole blocker the shared seam fixes.
        repo = "repos/rust-lang/rust"
        url = "https://github.com/rust-lang/rust/blob/automation/bors/auto/paper.pdf"
        gh = FakeGh(
            {
                ("api", f"{repo}/contents/bors/auto/paper.pdf?ref=automation"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{repo}/git/matching-refs/heads/automation"): gh_matching_refs(
                    "refs/heads/automation/bors/auto"
                ),
                (
                    "api",
                    f"{repo}/contents/paper.pdf?ref=automation%2Fbors%2Fauto",
                ): gh_contents(fixture_bytes("paper.pdf")),
            }
        )
        extractor = FakeExtractor()
        d = FileDriver(capabilities=caps(extractor), transport=self.refuse_transport, gh=gh)
        result = d.fetch(make_unit(url, Kind.FILE, fmt=Format.PDF))
        assert result.status is Status.DONE
        # The name comes from the resolved split, not the guessed one.
        assert result.meta["title"] == "paper.pdf"

    def test_a_blob_with_no_signature_is_routed_by_its_name(self):
        # A CSV carries no byte signature at all, so only the filename that
        # came back with the bytes says what it is. Unnamed, these bytes are
        # "unrecognized" and park — the committed table never reaches an
        # extractor.
        url = "https://github.com/acme/pipeline-kit/blob/main/data/runs.csv"
        args = ("api", "repos/acme/pipeline-kit/contents/data/runs.csv?ref=main")
        gh = FakeGh({args: gh_contents(b"run,status\n1,done\n2,dead\n")})
        extractor = FakeExtractor("csv-builtin", formats=frozenset({Format.CSV}))
        d = FileDriver(capabilities=caps(extractor), transport=self.refuse_transport, gh=gh)
        result = d.fetch(make_unit(url, Kind.FILE))  # no declared format to fall back on
        assert result.status is Status.DONE
        assert extractor.calls[0][1] is Format.CSV

    def test_an_lfs_pointer_blob_parks_rather_than_extracting_its_stand_in_text(self):
        # The github driver re-detects a `.pdf` blob by name, pointer bytes
        # or not — it cannot tell. This is where the 130 bytes of
        # `oid sha256:…` stop, before any extractor is handed them.
        pointer = (
            b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:08709a87567d8311d6fd29c4f4a5386801153e71450e628c4a5a5d7e85feda8b\n"
            b"size 7416886\n"
        )
        gh = FakeGh({self.CONTENTS: gh_contents(pointer)})
        extractor = FakeExtractor()
        d = FileDriver(capabilities=caps(extractor), transport=self.refuse_transport, gh=gh)
        result = d.fetch(make_unit(self.URL, Kind.FILE, fmt=Format.PDF))
        assert result.status is Status.MANUAL
        assert "git lfs pull" in reason_of(result)
        assert extractor.calls == []

    def test_html_committed_to_a_repo_never_bounces_back_to_web(self):
        # These bytes came from the contents API, not from a viewer page:
        # an HTML file in a repo is a file, and re-routing it to web would
        # fetch the viewer and re-detect straight back — a loop park.
        html = b"<!DOCTYPE html>\n<html><body>a committed page</body></html>"
        url = "https://github.com/acme/pipeline-kit/blob/main/docs/index.html"
        args = ("api", "repos/acme/pipeline-kit/contents/docs/index.html?ref=main")
        gh = FakeGh({args: gh_contents(html)})
        d = FileDriver(capabilities=caps(FakeExtractor()), transport=self.refuse_transport, gh=gh)
        result = d.fetch(make_unit(url, Kind.FILE))
        assert result.redetect is None
        assert result.status is Status.MANUAL
        assert "unrecognized file format" in reason_of(result)


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

    def test_signature_less_bytes_under_a_lying_html_content_type_extract(self):
        # Bytes decide, never the content type alone: real CSV bytes served
        # as text/html carry no HTML lead — the unit proceeds to extraction
        # under its claimed format instead of bouncing to web.
        transport = FakeTransport(
            {
                PDF_URL: HttpResponse(
                    status=200, content_type="text/html", body=fixture_bytes("stars.csv")
                )
            }
        )
        extractor = FakeExtractor()
        d = FileDriver(capabilities=caps(extractor), transport=transport)
        result = d.fetch(make_unit(PDF_URL, Kind.FILE, fmt=Format.CSV))
        assert result.redetect is None
        assert result.status is Status.DONE
        assert extractor.calls[0][1] is Format.CSV

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

    def test_a_call_time_availability_failure_re_parks_waiting(self, tmp_path):
        # available() said yes and the call said otherwise (a rate-limited
        # hosted extractor, a model download that died): the capability is
        # in truth unavailable, so the job waits — never error + an issue
        # filed about the world's weather, which is the transcribe path's
        # treatment of the same exception.
        (tmp_path / "doc.pdf").write_bytes(fixture_bytes("paper.pdf"))
        flaky = FakeExtractor(raise_=ProviderUnavailableError("extract API returned HTTP 429"))
        d = FileDriver(capabilities=caps(flaky), root=tmp_path)
        result = d.fetch(make_unit("file:doc.pdf", Kind.FILE))
        assert result.status is Status.WAITING
        assert result.needs is Need.EXTRACT
        assert "HTTP 429" in reason_of(result)

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
