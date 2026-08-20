"""Tests for drivers/paper.py: arxiv API + full text, web delegation."""

from dex_engine.drivers.paper import ARXIV_ID, PaperDriver
from dex_engine.drivers.transport import HttpResponse
from dex_engine.drivers.web import WebDriver
from dex_engine.pipeline.types import Kind, Status
from tests.drivers.conftest import FakeTransport, body_of, fixture_text, make_unit, reason_of

ABS_URL = "https://arxiv.org/abs/2408.12345"
API_URL = "https://export.arxiv.org/api/query?id_list=2408.12345"
HTML_URL = "https://arxiv.org/html/2408.12345"
AR5IV_URL = "https://ar5iv.labs.arxiv.org/html/2408.12345"
FEED = fixture_text("paper", "arxiv-feed.xml")
EMPTY_FEED = fixture_text("paper", "arxiv-empty-feed.xml")


def xml_response(text: str, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, content_type="application/atom+xml", body=text.encode())


def html_page(text: str, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, content_type="text/html", body=text.encode())


def long_extract(_html: str) -> str:
    return "Section 1. Introduction. " * 200


def thin_extract(_html: str) -> str:
    return "nav chrome"


def driver_for(responses: dict, extract=long_extract) -> PaperDriver:
    return PaperDriver(transport=FakeTransport(responses), extract=extract)


class TestIdentity:
    def test_kind_and_sleep(self):
        driver = driver_for({})
        assert driver.kind is Kind.PAPER
        assert driver.sleep == 3.0

    def test_matches_arxiv_openreview_and_hf_papers(self):
        driver = driver_for({})
        assert driver.matches("https://arxiv.org/abs/2408.12345")
        assert driver.matches("https://openreview.net/forum?id=abc")
        assert driver.matches("https://huggingface.co/papers/2408.12345")
        assert not driver.matches("https://huggingface.co/models/acme/thing")
        assert not driver.matches("https://example.test/paper")

    def test_arxiv_id_handles_abs_pdf_html_and_versions(self):
        for url in (
            "https://arxiv.org/abs/2408.12345",
            "https://arxiv.org/pdf/2408.12345.pdf",
            "https://arxiv.org/pdf/2408.12345v2",
            "https://arxiv.org/html/2408.12345v1",
        ):
            match = ARXIV_ID.search(url)
            assert match is not None
            assert match.group(1) == "2408.12345"


class TestArxiv:
    def test_abstract_and_full_text_sections(self):
        responses = {API_URL: xml_response(FEED), HTML_URL: html_page("<html>paper</html>")}
        result = driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER))
        assert result.status is Status.DONE
        body = body_of(result)
        assert body.startswith("## Abstract")
        assert "the work queue rather than a receipt log" in body
        assert "## Full text" in body
        assert result.meta.get("note") is None

    def test_meta_title_collapsed_published_date_and_id(self):
        responses = {API_URL: xml_response(FEED), HTML_URL: html_page("x")}
        meta = driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER)).meta
        assert meta["title"] == (
            "Ledger-Driven Ingestion: Honest Failure Classification for Long-Lived Corpora"
        )
        assert meta["published"] == "2026-07-15"
        assert meta["arxiv_id"] == "2408.12345"

    def test_ar5iv_fallback_when_arxiv_html_is_missing(self):
        responses = {
            API_URL: xml_response(FEED),
            HTML_URL: html_page("nope", status=404),
            AR5IV_URL: html_page("<html>rendered</html>"),
        }
        result = driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER))
        assert result.status is Status.DONE
        assert "## Full text" in body_of(result)

    def test_thin_full_text_degrades_to_abstract_only_with_note(self):
        responses = {
            API_URL: xml_response(FEED),
            HTML_URL: html_page("<html>chrome</html>"),
            AR5IV_URL: html_page("<html>chrome</html>"),
        }
        result = driver_for(responses, extract=thin_extract).fetch(make_unit(ABS_URL, Kind.PAPER))
        assert result.status is Status.DONE
        assert result.meta["note"] == "abstract only"
        assert "## Full text" not in body_of(result)

    def test_unknown_id_is_dead(self):
        responses = {API_URL: xml_response(EMPTY_FEED)}
        result = driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER))
        assert result.status is Status.DEAD
        assert "no such id" in reason_of(result)

    def test_api_failures_are_classified(self):
        responses = {API_URL: xml_response("busy", status=503)}
        result = driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER))
        assert result.status is Status.BLOCKED

    def test_unparseable_feed_is_blocked(self):
        responses = {API_URL: xml_response("<feed><unclosed")}
        result = driver_for(responses).fetch(make_unit(ABS_URL, Kind.PAPER))
        assert result.status is Status.BLOCKED
        assert "unparseable XML" in reason_of(result)


class TestDelegation:
    def test_non_arxiv_papers_read_as_articles_via_the_web_driver(self):
        calls: list[str] = []

        class RecordingWeb(WebDriver):
            def fetch(self, unit):
                calls.append(unit.url)
                return super().fetch(unit)

        url = "https://openreview.net/forum?id=abc"
        web = RecordingWeb(
            transport=FakeTransport({url: html_page("<html>rev</html>")}), extract=long_extract
        )
        driver = PaperDriver(transport=FakeTransport({}), extract=long_extract, web=web)
        result = driver.fetch(make_unit(url, Kind.PAPER))
        assert calls == [url]
        assert result.status is Status.DONE
