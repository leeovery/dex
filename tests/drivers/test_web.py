"""Tests for drivers/web.py: classified fetches, links kept, wayback fallback."""

import socket
import urllib.parse

from dex_engine.drivers.transport import HttpResponse
from dex_engine.drivers.web import WebDriver, trafilatura_extract
from dex_engine.pipeline.classify import PAYWALL_REASON
from dex_engine.pipeline.types import Format, Kind, Status
from tests.capabilities.conftest import fixture_bytes
from tests.drivers.conftest import (
    FakeTransport,
    body_of,
    fixture_text,
    html_response,
    json_response,
    make_unit,
    reason_of,
)

URL = "https://example.test/post"
ARTICLE = fixture_text("web", "article.html")
THIN = fixture_text("web", "thin.html")


def wayback_lookup_url(url: str) -> str:
    return "https://archive.org/wayback/available?url=" + urllib.parse.quote(url, safe="")


def substantial_extract(_html: str) -> str:
    return "extracted body " * 40


def driver_for(responses: dict, extract=substantial_extract) -> WebDriver:
    return WebDriver(transport=FakeTransport(responses), extract=extract)


class TestIdentity:
    def test_kind_and_sleep(self):
        driver = driver_for({})
        assert driver.kind is Kind.WEB
        assert driver.sleep == 1.0

    def test_matches_everything_as_the_catch_all(self):
        driver = driver_for({})
        assert driver.matches("https://anything.example/x")
        assert driver.matches("https://x.com/user/status/1")  # ordering, not matching, excludes

    def test_canonical_uses_the_shared_base_rules(self):
        driver = driver_for({})
        assert (
            driver.canonical("http://www.example.test/a/?utm_source=x&b=c")
            == "https://example.test/a?b=c"
        )


class TestRedetection:
    def test_pdf_body_signals_file_redetection(self):
        # The lying server: content type claims html, the bytes are a PDF —
        # magic wins, and this must never park as thin-extraction manual.
        response = HttpResponse(
            status=200, content_type="text/html", body=fixture_bytes("paper.pdf")
        )
        driver = driver_for({URL: response})
        result = driver.fetch(make_unit(URL, Kind.WEB))
        assert result.status is Status.QUEUED
        assert result.redetect is not None
        assert result.redetect.kind is Kind.FILE
        assert result.redetect.format is Format.PDF
        assert result.body is None  # identity only; the file driver owns outputs

    def test_docx_body_signals_its_format(self):
        response = HttpResponse(
            status=200, content_type="text/html", body=fixture_bytes("report.docx")
        )
        result = driver_for({URL: response}).fetch(make_unit(URL, Kind.WEB))
        assert result.redetect is not None
        assert result.redetect.format is Format.DOCX

    def test_declared_content_type_catches_signature_less_formats(self):
        response = HttpResponse(
            status=200, content_type="text/csv", body=fixture_bytes("stars.csv")
        )
        result = driver_for({URL: response}).fetch(make_unit(URL, Kind.WEB))
        assert result.redetect is not None
        assert result.redetect.format is Format.CSV

    def test_ordinary_thin_html_stays_manual_never_redetects(self):
        driver = driver_for({URL: html_response(THIN)}, extract=lambda _html: None)
        result = driver.fetch(make_unit(URL, Kind.WEB))
        assert result.redetect is None
        assert result.status is Status.MANUAL
        assert reason_of(result) == "thin-extraction"


class TestSuccessfulFetch:
    def test_real_extraction_keeps_hyperlinks(self):
        # The harvest prerequisite fix: include_links=True — harvest reads these.
        driver = driver_for({URL: html_response(ARTICLE)}, extract=trafilatura_extract)
        result = driver.fetch(make_unit(URL, Kind.WEB))
        assert result.status is Status.DONE
        assert "https://example.test/ledger-driven-design" in body_of(result)

    def test_meta_title_and_og_image_media(self):
        driver = driver_for({URL: html_response(ARTICLE)})
        result = driver.fetch(make_unit(URL, Kind.WEB))
        assert result.meta["title"] == "Designing Resilient Ingestion Pipelines & Ledgers"
        assert result.media == ["https://cdn.example.test/img/pipeline-hero.png"]

    def og_image_media(self, content: str) -> list[str]:
        html = (
            f'<html><head><meta property="og:image" content="{content}">'
            "</head><body>body</body></html>"
        )
        result = driver_for({URL: html_response(html)}).fetch(make_unit(URL, Kind.WEB))
        return result.media

    def test_relative_og_image_resolves_against_the_page(self):
        # The media stage fetches verbatim: a relative value that reached it
        # was a URL nothing could fetch.
        assert self.og_image_media("/img/hero.png") == ["https://example.test/img/hero.png"]

    def test_protocol_relative_og_image_takes_the_page_scheme(self):
        assert self.og_image_media("//cdn.example.test/hero.png") == [
            "https://cdn.example.test/hero.png"
        ]

    def test_og_image_entities_are_unescaped(self):
        assert self.og_image_media("https://cdn.example.test/h.png?a=1&amp;b=2") == [
            "https://cdn.example.test/h.png?a=1&b=2"
        ]

    def test_non_http_og_image_is_not_media(self):
        assert self.og_image_media("data:image/png;base64,iVBOR") == []

    def test_wrapped_content_attribute_never_yields_a_multi_line_url(self):
        html = (
            '<html><head><meta property="og:image" content="https://cdn.example.test/\n'
            'hero.png"></head><body>body</body></html>'
        )
        media = driver_for({URL: html_response(html)}).fetch(make_unit(URL, Kind.WEB)).media
        assert media == ["https://cdn.example.test/"]
        assert all("\n" not in url for url in media)

    def test_200_but_thin_is_manual_never_dead(self):
        # Learned from the 2026-08-20 overnight runs: JS shells were ledgered
        # dead and their items stranded at raw.
        driver = driver_for({URL: html_response(THIN)}, extract=trafilatura_extract)
        result = driver.fetch(make_unit(URL, Kind.WEB))
        assert result.status is Status.MANUAL
        assert result.reason == "thin-extraction"


class TestClassifiedFailures:
    def test_403_is_blocked_with_the_wayback_miss_noted(self):
        responses = {
            URL: html_response("denied", status=403),
            wayback_lookup_url(URL): json_response({"archived_snapshots": {}}),
        }
        result = driver_for(responses).fetch(make_unit(URL, Kind.WEB))
        assert result.status is Status.BLOCKED
        assert "HTTP 403" in reason_of(result)
        assert "no wayback snapshot" in reason_of(result)

    def test_402_paywall_is_manual(self):
        responses = {
            URL: html_response("pay up", status=402),
            wayback_lookup_url(URL): json_response({"archived_snapshots": {}}),
        }
        result = driver_for(responses).fetch(make_unit(URL, Kind.WEB))
        assert result.status is Status.MANUAL
        assert PAYWALL_REASON in reason_of(result)

    def test_nxdomain_is_dead(self):
        gone = "https://gone.example.test/x"
        responses = {
            gone: socket.gaierror(socket.EAI_NONAME, "no such host"),
            wayback_lookup_url(gone): json_response({"archived_snapshots": {}}),
        }
        result = driver_for(responses).fetch(make_unit(gone, Kind.WEB))
        assert result.status is Status.DEAD
        assert "NXDOMAIN" in reason_of(result)


class TestWaybackFallback:
    SNAPSHOT = "https://web.archive.org/web/2026/https://example.test/post"

    def test_404_rescued_by_snapshot_is_done(self):
        responses = {
            URL: html_response("gone", status=404),
            wayback_lookup_url(URL): json_response(
                {"archived_snapshots": {"closest": {"available": True, "url": self.SNAPSHOT}}}
            ),
            self.SNAPSHOT: html_response(ARTICLE),
        }
        result = driver_for(responses).fetch(make_unit(URL, Kind.WEB))
        assert result.status is Status.DONE
        assert result.meta["via"] == "wayback"
        assert result.meta["snapshot"] == self.SNAPSHOT
        assert result.media == []  # snapshot og:images point at web.archive.org

    def test_snapshot_fetch_failure_is_classified_never_swallowed(self):
        responses = {
            URL: html_response("gone", status=404),
            wayback_lookup_url(URL): json_response(
                {"archived_snapshots": {"closest": {"available": True, "url": self.SNAPSHOT}}}
            ),
            self.SNAPSHOT: html_response("oops", status=503),
        }
        result = driver_for(responses).fetch(make_unit(URL, Kind.WEB))
        assert result.status is Status.DEAD  # the direct truth stands
        assert "HTTP 404" in reason_of(result)
        assert "wayback fetch failed: HTTP 503" in reason_of(result)

    def test_lookup_connection_failure_is_classified_never_swallowed(self):
        responses = {
            URL: html_response("gone", status=404),
            wayback_lookup_url(URL): TimeoutError("timed out"),
        }
        result = driver_for(responses).fetch(make_unit(URL, Kind.WEB))
        assert result.status is Status.DEAD
        assert "wayback lookup failed" in reason_of(result)

    def test_thin_snapshot_keeps_the_direct_classification(self):
        responses = {
            URL: html_response("gone", status=404),
            wayback_lookup_url(URL): json_response(
                {"archived_snapshots": {"closest": {"available": True, "url": self.SNAPSHOT}}}
            ),
            self.SNAPSHOT: html_response(THIN),
        }
        result = driver_for(responses, extract=lambda _html: None).fetch(make_unit(URL, Kind.WEB))
        assert result.status is Status.DEAD
        assert "wayback snapshot extraction was thin" in reason_of(result)

    def test_unparseable_lookup_json_is_noted(self):
        responses = {
            URL: html_response("gone", status=404),
            wayback_lookup_url(URL): html_response("<html>not json</html>"),
        }
        result = driver_for(responses).fetch(make_unit(URL, Kind.WEB))
        assert result.status is Status.DEAD
        assert "unparseable JSON" in reason_of(result)
