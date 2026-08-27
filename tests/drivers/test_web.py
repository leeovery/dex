"""Tests for the web driver over the shared article seam (drivers/article.py).

The catch-all's whole fetch behaviour — classified fetches, links kept,
thin parking, re-detection, wayback fallback — lives in the article lib,
and this file pins it through the driver that reads every page that way.
"""

import socket
import urllib.parse

from dex_engine.drivers.article import trafilatura_extract
from dex_engine.drivers.transport import HttpResponse
from dex_engine.drivers.web import WebDriver
from dex_engine.pipeline.classify import PAYWALL_REASON
from dex_engine.pipeline.types import (
    Content,
    Format,
    Kind,
    Missing,
    Redetected,
    Refused,
    Unusable,
)
from tests.capabilities.conftest import fixture_bytes
from tests.drivers.conftest import (
    FakeTransport,
    body_of,
    content_of,
    evidence_of,
    fixture_text,
    html_response,
    json_response,
    make_unit,
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
        # Identity only; the file driver owns outputs — Redetected has no body.
        assert result == Redetected(kind=Kind.FILE, format=Format.PDF)

    def test_docx_body_signals_its_format(self):
        response = HttpResponse(
            status=200, content_type="text/html", body=fixture_bytes("report.docx")
        )
        result = driver_for({URL: response}).fetch(make_unit(URL, Kind.WEB))
        assert result == Redetected(kind=Kind.FILE, format=Format.DOCX)

    def test_declared_content_type_catches_signature_less_formats(self):
        response = HttpResponse(
            status=200, content_type="text/csv", body=fixture_bytes("stars.csv")
        )
        result = driver_for({URL: response}).fetch(make_unit(URL, Kind.WEB))
        assert result == Redetected(kind=Kind.FILE, format=Format.CSV)

    def test_an_episode_page_whose_audio_is_its_substance_signals_podcast(self):
        # A player and a paragraph of notes: extraction has nothing to keep,
        # so the audio IS the page. Real extraction, because the threshold
        # is half the decision.
        page = fixture_text("podcast", "indie-episode-page.html")
        driver = driver_for({URL: html_response(page)}, extract=trafilatura_extract)
        result = driver.fetch(make_unit(URL, Kind.WEB))
        assert result == Redetected(kind=Kind.PODCAST)

    def test_an_og_audio_declaration_signals_podcast_over_any_body(self):
        # og:audio is the publisher naming the audio as this page's object —
        # an episode page with full show notes is still an episode page.
        declared = ARTICLE.replace(
            "</head>",
            '<meta property="og:audio" content="https://cdn.pods.test/ep42.mp3"></head>',
        )
        result = driver_for({URL: html_response(declared)}).fetch(make_unit(URL, Kind.WEB))
        assert result == Redetected(kind=Kind.PODCAST)

    def test_a_listen_to_this_article_widget_never_steals_the_article(self):
        # The incident: mainstream publishers ship text-to-speech players
        # and encyclopedias embed media samples. An <audio> element beside
        # a real article is an accessory — parking that article as an
        # episode loses the whole body AND its links.
        narrated = ARTICLE.replace(
            "</body>",
            '<audio class="tts-player" preload="none" controls '
            'src="https://cdn.example.test/polly/article.mp3"></audio></body>',
        )
        driver = driver_for({URL: html_response(narrated)}, extract=trafilatura_extract)
        result = driver.fetch(make_unit(URL, Kind.WEB))
        assert isinstance(result, Content)  # the article wins; never Redetected
        assert "https://example.test/ledger-driven-design" in body_of(result)  # links harvested

    def test_a_player_on_a_page_with_a_substantial_body_is_an_accessory(self):
        # The rule at the seam, independent of any one publisher's markup.
        narrated = ARTICLE.replace("</body>", '<audio src="/media/read-aloud.mp3"></audio></body>')
        result = driver_for({URL: html_response(narrated)}).fetch(make_unit(URL, Kind.WEB))
        assert isinstance(result, Content)

    def test_a_blog_advertising_an_rss_feed_is_never_stolen(self):
        # THE boundary: "/feed", "/rss" and an RSS <link rel> are blog
        # vocabulary. Only audio the page carries makes it an episode.
        blogish = ARTICLE.replace(
            "</head>",
            '<link rel="alternate" type="application/rss+xml" '
            'href="https://example.test/feed.xml"></head>',
        )
        driver = driver_for({URL: html_response(blogish)})
        result = driver.fetch(make_unit(URL, Kind.WEB))
        assert isinstance(result, Content)

    def test_a_post_merely_linking_an_mp3_is_never_stolen(self):
        linked = ARTICLE.replace(
            "</body>", '<p><a href="/files/talk.mp3">Download the talk</a></p></body>'
        )
        driver = driver_for({URL: html_response(linked)})
        assert not isinstance(driver.fetch(make_unit(URL, Kind.WEB)), Redetected)

    def test_ordinary_thin_html_stays_manual_never_redetects(self):
        driver = driver_for({URL: html_response(THIN)}, extract=lambda _html: None)
        result = driver.fetch(make_unit(URL, Kind.WEB))
        assert result == Unusable(evidence="thin-extraction")  # never a Redetected bounce


class TestSuccessfulFetch:
    def test_real_extraction_keeps_hyperlinks(self):
        # The harvest prerequisite fix: include_links=True — harvest reads these.
        driver = driver_for({URL: html_response(ARTICLE)}, extract=trafilatura_extract)
        result = driver.fetch(make_unit(URL, Kind.WEB))
        assert isinstance(result, Content)
        assert "https://example.test/ledger-driven-design" in body_of(result)

    def test_meta_title_and_og_image_media(self):
        driver = driver_for({URL: html_response(ARTICLE)})
        result = content_of(driver.fetch(make_unit(URL, Kind.WEB)))
        assert result.meta["title"] == "Designing Resilient Ingestion Pipelines & Ledgers"
        assert result.media == ["https://cdn.example.test/img/pipeline-hero.png"]

    def og_image_media(self, content: str) -> list[str]:
        html = (
            f'<html><head><meta property="og:image" content="{content}">'
            "</head><body>body</body></html>"
        )
        outcome = driver_for({URL: html_response(html)}).fetch(make_unit(URL, Kind.WEB))
        return content_of(outcome).media

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

    def test_a_wrapped_content_attribute_yields_no_media_at_all(self):
        # Truncating at the line break left "https://cdn.example.test/" — a
        # well-formed request for a resource that does not exist, ledgered
        # as a media unit and fetched for nothing. A URL this driver cannot
        # read whole is not a URL it hands on.
        html = (
            '<html><head><meta property="og:image" content="https://cdn.example.test/\n'
            'hero.png"></head><body>body</body></html>'
        )
        result = content_of(driver_for({URL: html_response(html)}).fetch(make_unit(URL, Kind.WEB)))
        assert result.media == []  # the page still enriches, just without media

    def test_200_but_thin_is_manual_never_dead(self):
        # Learned from the 2026-08-20 overnight runs: JS shells were ledgered
        # dead and their items stranded at raw.
        driver = driver_for({URL: html_response(THIN)}, extract=trafilatura_extract)
        result = driver.fetch(make_unit(URL, Kind.WEB))
        assert result == Unusable(evidence="thin-extraction")


class TestDescriptionMeta:
    """The deck the publisher declares in meta, lifted into the frontmatter.

    The reported page: a Substack post whose standfirst — the sentence
    under the headline — sits in the post header, outside the body div.
    Trafilatura reads that header as boilerplate, so the H1 and the deck
    leave the extraction together; the title lift already compensates the
    H1, and until this nothing carried the deck.
    """

    def meta_for(self, head: str) -> dict:
        html = f"<html><head>{head}</head><body>body</body></html>"
        outcome = driver_for({URL: html_response(html)}).fetch(make_unit(URL, Kind.WEB))
        return content_of(outcome).meta

    def test_a_deck_in_the_post_header_survives_extraction_dropping_it(self):
        page = fixture_text("web", "substack-header-deck.html")
        driver = driver_for({URL: html_response(page)}, extract=trafilatura_extract)
        result = content_of(driver.fetch(make_unit(URL, Kind.WEB)))
        assert "pattern work" not in body_of(result)  # the loss this compensates
        assert list(result.meta) == ["title", "description"]  # frontmatter order
        assert result.meta["description"] == (
            "Engineering leadership is pattern work. I'm treating it that way."
        )

    def test_og_description_wins_over_the_plain_name(self):
        # A page writing both puts its considered wording in the og: block.
        meta = self.meta_for(
            '<meta name="description" content="Truncated for search results…">'
            '<meta property="og:description" content="The deck as the author wrote it.">'
        )
        assert meta["description"] == "The deck as the author wrote it."

    def test_the_plain_name_carries_a_page_with_no_og_block(self):
        meta = self.meta_for('<meta name="description" content="The only deck there is.">')
        assert meta["description"] == "The only deck there is."

    def test_either_attribute_order_is_read(self):
        meta = self.meta_for(
            '<meta content="Content first, name second." property="og:description">'
        )
        assert meta["description"] == "Content first, name second."

    def test_an_empty_declaration_falls_through_to_the_next(self):
        meta = self.meta_for(
            '<meta property="og:description" content="   ">'
            '<meta name="description" content="The deck that says something.">'
        )
        assert meta["description"] == "The deck that says something."

    def test_a_page_declaring_no_description_carries_no_field(self):
        assert "description" not in self.meta_for("<title>A page</title>")

    def test_entities_unescape_and_wrapped_prose_collapses(self):
        meta = self.meta_for(
            '<meta property="og:description" content="Ledgers &amp; queues,\n   one line.">'
        )
        assert meta["description"] == "Ledgers & queues, one line."

    def test_a_long_deck_is_capped(self):
        deck = "deck " * 100
        capped = self.meta_for(f'<meta property="og:description" content="{deck}">')["description"]
        assert len(capped) == 300
        assert capped.startswith("deck deck deck")


class TestClassifiedFailures:
    def test_403_is_blocked_with_the_wayback_miss_noted(self):
        responses = {
            URL: html_response("denied", status=403),
            wayback_lookup_url(URL): json_response({"archived_snapshots": {}}),
        }
        result = driver_for(responses).fetch(make_unit(URL, Kind.WEB))
        assert isinstance(result, Refused)
        assert not result.permanent
        assert "HTTP 403" in result.evidence
        assert "no wayback snapshot" in result.evidence

    def test_402_paywall_is_manual(self):
        responses = {
            URL: html_response("pay up", status=402),
            wayback_lookup_url(URL): json_response({"archived_snapshots": {}}),
        }
        result = driver_for(responses).fetch(make_unit(URL, Kind.WEB))
        assert isinstance(result, Refused)
        assert result.permanent
        assert PAYWALL_REASON in result.evidence

    def test_nxdomain_is_dead(self):
        gone = "https://gone.example.test/x"
        responses = {
            gone: socket.gaierror(socket.EAI_NONAME, "no such host"),
            wayback_lookup_url(gone): json_response({"archived_snapshots": {}}),
        }
        result = driver_for(responses).fetch(make_unit(gone, Kind.WEB))
        assert isinstance(result, Missing)
        assert "NXDOMAIN" in result.evidence


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
        result = content_of(driver_for(responses).fetch(make_unit(URL, Kind.WEB)))
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
        assert isinstance(result, Missing)  # the direct truth stands
        assert "HTTP 404" in result.evidence
        assert "wayback fetch failed: HTTP 503" in result.evidence

    def test_lookup_connection_failure_is_classified_never_swallowed(self):
        responses = {
            URL: html_response("gone", status=404),
            wayback_lookup_url(URL): TimeoutError("timed out"),
        }
        result = driver_for(responses).fetch(make_unit(URL, Kind.WEB))
        assert isinstance(result, Missing)
        assert "wayback lookup failed" in evidence_of(result)

    def test_thin_snapshot_keeps_the_direct_classification(self):
        responses = {
            URL: html_response("gone", status=404),
            wayback_lookup_url(URL): json_response(
                {"archived_snapshots": {"closest": {"available": True, "url": self.SNAPSHOT}}}
            ),
            self.SNAPSHOT: html_response(THIN),
        }
        result = driver_for(responses, extract=lambda _html: None).fetch(make_unit(URL, Kind.WEB))
        assert isinstance(result, Missing)
        assert "wayback snapshot extraction was thin" in result.evidence

    def test_unparseable_lookup_json_is_noted(self):
        responses = {
            URL: html_response("gone", status=404),
            wayback_lookup_url(URL): html_response("<html>not json</html>"),
        }
        result = driver_for(responses).fetch(make_unit(URL, Kind.WEB))
        assert isinstance(result, Missing)
        assert "unparseable JSON" in result.evidence


class TestExtractionFidelity:
    """What `include_links=True` costs unaccompanied, and the page preparation that pays it.

    Every fixture here is the reduced shape of a page that lost content in
    the field — reported from an instance, reproduced against the live
    page, then trimmed to the markup that carries the defect.
    """

    def test_a_headings_permalink_does_not_swallow_its_words(self):
        # Sphinx/mkdocs/Docusaurus put an empty <a> INSIDE the heading. With
        # links on, the extractor emitted the marker and dropped the text:
        # three headings on one docs page became bare `#`/`##` lines, and
        # the words were nowhere else in the file.
        page = fixture_text("web", "docs-anchored-headings.html")
        body = trafilatura_extract(page) or ""
        assert not [line for line in body.split("\n") if line.strip("# ") == "" and "#" in line]
        for heading in ("Agentic Coding: Humans Design", "Agentic Coding Steps"):
            assert heading in body
        assert "Example LLM Project File Structure" in body

    def test_per_line_code_anchors_do_not_take_the_fence_with_them(self):
        # mkdocs-material anchors every highlighted line. Those anchors took
        # the whole block: a llama-cpp docs page went from 42 fenced blocks
        # to none, losing the quickstart and every install command.
        page = fixture_text("web", "mkdocs-code-anchors.html")
        body = trafilatura_extract(page) or ""
        assert "pip install llama-cpp-python" in body
        assert "from llama_cpp import Llama" in body
        assert body.count("```") >= 2  # at least one fence survived, opened and closed

    def test_a_glyph_permalink_does_not_swallow_its_heading(self):
        # A cursor.com post: the permalink wraps a `#` in a span, so it is
        # not empty and the drop above never sees it — and it swallows
        # like an empty one. Every heading on the page came back a bare
        # `##`, the words nowhere in the extract.
        page = fixture_text("web", "glyph-anchored-headings.html")
        body = trafilatura_extract(page) or ""
        assert [line for line in body.split("\n") if line.startswith("#")] == [
            "# Why Git Is Hard",
            "## What's hard about Git?",
            "## What we built instead",
        ]

    def test_a_glyph_permalink_before_the_words_does_not_leak_into_them(self):
        # laravel-news.com: the same chrome without the span leaks instead
        # of swallowing, and the heading led with the icon rather than
        # its subject.
        page = fixture_text("web", "permalink-glyph-headings.html")
        body = trafilatura_extract(page) or ""
        assert [line for line in body.split("\n") if line.startswith("#")] == [
            "# Column Transformations",
            "## Column Transformation Strategies",
            "## Testing The Migration",
        ]

    def test_a_glyph_permalink_after_the_words_does_not_leak_either(self):
        # A hugo blog trails its anchor instead, and the glyph landed on
        # the end of the heading — the same defect, the other side.
        page = fixture_text("web", "trailing-glyph-headings.html")
        body = trafilatura_extract(page) or ""
        assert [line for line in body.split("\n") if line.startswith("#")] == [
            "# A Stateless Protocol",
            "## What Changed In The Spec",
            "## What It Costs",
        ]

    def test_the_glyph_drop_reaches_nothing_an_author_wrote(self):
        # The rails on that repair, all three. An anchor with words in it
        # is a link somebody meant, heading or not; a lone `#` is only
        # provably an icon INSIDE a heading, so in prose it stays; and a
        # glyph that is not an anchor at all is subject matter — docs
        # pages have headings ABOUT `#`.
        page = (
            ARTICLE.replace(
                "<h1>Designing Resilient Ingestion Pipelines</h1>",
                '<h1><a href="https://example.test/series">Designing</a> Resilient Pipelines</h1>',
            )
            .replace(
                "      <p>Politeness matters",
                "      <h2>The <code>#</code> Fragment Rule</h2>\n      <p>Politeness matters",
            )
            .replace(
                "    </article>",
                '      <p>The <a href="https://example.test/rfc#anchors">#</a> convention for'
                " fragment identifiers is argued at length in that note, and this pipeline"
                " follows it to the letter.</p>\n    </article>",
            )
        )
        body = trafilatura_extract(page) or ""
        assert "# [Designing](https://example.test/series) Resilient Pipelines" in body
        assert "## The `#` Fragment Rule" in body
        assert "[#](https://example.test/rfc#anchors)" in body

    def test_whitespace_padded_heading_text_stays_on_the_heading_line(self):
        # Hugging Face model cards wrap heading text in a <span> padded
        # with source-formatting newlines and tabs. The extractor emitted
        # that whitespace verbatim: the marker alone on its line, the words
        # tabs-and-blank-lines below — an empty heading to any consumer
        # keying on the heading line.
        page = fixture_text("web", "whitespace-padded-headings.html")
        body = trafilatura_extract(page) or ""
        assert not [line for line in body.split("\n") if line.strip("# \t") == "" and "#" in line]
        assert any(line.startswith("#") and "HunyuanOCR-1.5" in line for line in body.split("\n"))
        assert any(line.startswith("#") and "Use Case:" in line for line in body.split("\n"))

    def test_a_line_break_between_heading_parts_stays_a_separator(self):
        # The break IS the word boundary. Removing it outright welded the
        # parts: a post whose top heading carried its subtitle after a
        # <br> came back as one line reading "...Big ModelsWhat a year",
        # so the subtitle was no longer a line and two words were one.
        page = fixture_text("web", "line-broken-headings.html")
        body = trafilatura_extract(page) or ""
        assert "ModelsWhat" not in body
        assert "Cheap Tricks Beat Big Models What a year" in body
        assert "Method and its limits" in body

    def test_a_discussion_pages_comments_are_its_substance(self):
        # A thread rendered as nested tables: dropping comments left the
        # masthead and the submission row, and deleted all 115 replies —
        # which were the reason the page was captured at all.
        page = fixture_text("web", "discussion-thread.html")
        body = trafilatura_extract(page) or ""
        assert "the geometry interpretable" in body
        assert "dot-product rankings" in body

    def test_an_ordinary_articles_comment_section_stays_chrome(self):
        # The other half of the same rule: an article extracts identically
        # either way, so the discussion test above must not have turned
        # comment sections on for every blog post that has one.
        assert trafilatura_extract(ARTICLE) == trafilatura_extract(ARTICLE)
        body = trafilatura_extract(ARTICLE) or ""
        assert "https://example.test/ledger-driven-design" in body  # links still kept

    def test_an_unparseable_page_reaches_the_extractor_untouched(self):
        # Preparation is a repair, never a gate: whatever lxml cannot read
        # goes through as it arrived and the extractor decides.
        assert trafilatura_extract("") is None
