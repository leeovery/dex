"""Tests for the podcast driver: resolution paths, honest failures."""

import xml.etree.ElementTree as ET

from dex_engine.drivers.podcast import PodcastDriver, _itunes_episode, _match_by_title
from dex_engine.drivers.transport import HttpResponse
from dex_engine.pipeline.types import Kind, Need, NeedsCapability, Refused, Unusable
from tests.drivers.conftest import (
    FakeTransport,
    evidence_of,
    fixture_text,
    html_response,
    make_unit,
    needs_of,
)

APPLE_URL = "https://podcasts.apple.com/us/podcast/engineering-distilled/id9990001?i=8880042"
SPOTIFY_URL = "https://open.spotify.com/episode/4rOoJ6Egrf8K2IrywzwOMk"
FEED_URL = "https://feeds.pods.test/engineering-distilled.rss"
PAGE_URL = "https://engineering-distilled.test/episodes/ledgers-as-work-queues"
# Keyed by the SHOW id (9990001), never the episode id: handed an episode id
# the lookup API answers resultCount 0 for every episode that exists.
LOOKUP_URL = "https://itunes.apple.com/lookup?id=9990001&entity=podcastEpisode&limit=200"
ENCLOSURE = "https://cdn.pods.test/ed/ep42.mp3?sig=abc123"


def xml_response(text: str) -> HttpResponse:
    return HttpResponse(status=200, content_type="application/rss+xml", body=text.encode())


def driver(responses: dict) -> PodcastDriver:
    return PodcastDriver(transport=FakeTransport(responses))


def feed() -> HttpResponse:
    return xml_response(fixture_text("podcast", "feed.xml"))


def indie_page() -> HttpResponse:
    """A realistic indie episode page: a player, a feed link, real notes."""
    return html_response(fixture_text("podcast", "indie-episode-page.html"))


class TestDetection:
    def test_matches_apple_spotify_and_explicit_rss(self):
        d = PodcastDriver()
        assert d.matches(APPLE_URL)
        assert d.matches(SPOTIFY_URL)
        assert d.matches(FEED_URL)  # explicit .rss
        assert not d.matches("https://open.spotify.com/track/abc")  # music, not an episode
        assert not d.matches("https://example.test/blog/post")

    def test_bare_feed_vocabulary_belongs_to_the_web_driver(self):
        # Blogs own /feed, /rss and feeds.* — over-claiming them stole
        # ordinary blog URLs from the web driver. The
        # long-term answer for a feed URL with no audio is corrected-kind
        # re-entry, out of scope this phase.
        d = PodcastDriver()
        assert not d.matches("https://example.test/podcast/feed")
        assert not d.matches("https://example.test/rss")
        assert not d.matches("https://example.test/blog/feed/")
        assert not d.matches("https://feeds.example.test/updates")
        assert not d.matches("https://feed.example.test/atom")

    def test_canonical_keeps_only_the_apple_episode_param(self):
        d = PodcastDriver()
        assert d.canonical(APPLE_URL + "&l=en-GB") == (
            "https://podcasts.apple.com/us/podcast/engineering-distilled/id9990001?i=8880042"
        )
        assert d.canonical(SPOTIFY_URL + "?si=share-noise") == (
            "https://open.spotify.com/episode/4rOoJ6Egrf8K2IrywzwOMk"
        )


class TestAppleResolution:
    def test_lookup_feed_match_enclosure(self):
        # Apple link → iTunes lookup on the SHOW id (keyless) → match the
        # episode by trackId → show RSS → enclosure.
        d = driver(
            {
                LOOKUP_URL: html_response(fixture_text("podcast", "itunes-lookup.json")),
                FEED_URL: feed(),
            }
        )
        result = needs_of(d.fetch(make_unit(APPLE_URL, Kind.PODCAST)))
        assert result.need is Need.TRANSCRIBE
        assert result.meta["enclosure"] == ENCLOSURE
        assert result.meta["title"] == "Ledgers as Work Queues"
        assert result.meta["show"] == "Engineering Distilled"

    def test_show_notes_come_from_the_feed_links_kept(self):
        d = driver(
            {
                LOOKUP_URL: html_response(fixture_text("podcast", "itunes-lookup.json")),
                FEED_URL: feed(),
            }
        )
        result = needs_of(d.fetch(make_unit(APPLE_URL, Kind.PODCAST)))
        assert result.body is not None
        assert "[Ada Guest](https://example.test/guest)" in result.body  # harvestable
        assert "- Why JSONL merges" in result.body

    def test_show_link_without_episode_param_is_manual(self):
        d = driver({})
        show_url = "https://podcasts.apple.com/us/podcast/engineering/id9990001"
        unit = make_unit(show_url, Kind.PODCAST)
        result = d.fetch(unit)
        assert isinstance(result, Unusable)
        assert "show, not an episode" in evidence_of(result)

    def test_the_lookup_is_keyed_by_the_show_id_not_the_episode_id(self):
        # The pin: the episode-id lookup the driver used to send resolves
        # NOTHING — resultCount 0 for every episode that exists, so the
        # whole Apple route could never park anything but manual.
        transport = FakeTransport(
            {
                LOOKUP_URL: html_response(fixture_text("podcast", "itunes-lookup.json")),
                FEED_URL: feed(),
            }
        )
        result = PodcastDriver(transport=transport).fetch(make_unit(APPLE_URL, Kind.PODCAST))
        assert isinstance(result, NeedsCapability)
        assert transport.calls[0] == ("GET", LOOKUP_URL)
        assert all("id=8880042" not in url for _method, url in transport.calls)

    def test_episode_outside_the_lookup_window_is_manual(self):
        d = driver({LOOKUP_URL: html_response('{"resultCount": 0, "results": []}')})
        result = d.fetch(make_unit(APPLE_URL, Kind.PODCAST))
        assert isinstance(result, Unusable)
        assert "not among the 200 episodes iTunes lists for this show" in evidence_of(result)

    def test_apple_link_without_a_show_id_segment_is_manual(self):
        d = driver({})
        url = "https://podcasts.apple.com/us/podcast/engineering-distilled?i=8880042"
        result = d.fetch(make_unit(url, Kind.PODCAST))
        assert isinstance(result, Unusable)
        assert "no show id" in evidence_of(result)

    def test_episode_pulled_from_feed_is_manual(self):
        pulled = fixture_text("podcast", "itunes-lookup.json").replace("ep-42-guid", "gone-guid")
        pulled = pulled.replace("Ledgers as Work Queues", "An Episode The Feed Lost")
        d = driver({LOOKUP_URL: html_response(pulled), FEED_URL: feed()})
        result = d.fetch(make_unit(APPLE_URL, Kind.PODCAST))
        assert isinstance(result, Unusable)
        assert "not in the show's feed" in evidence_of(result)

    def test_lookup_http_failure_classifies_never_dies(self):
        d = driver({LOOKUP_URL: HttpResponse(status=503, content_type="text/html", body=b"")})
        result = d.fetch(make_unit(APPLE_URL, Kind.PODCAST))
        assert isinstance(result, Refused)
        assert evidence_of(result) == "HTTP 503"

    def test_unparseable_itunes_json_is_blocked(self):
        d = driver({LOOKUP_URL: html_response("<html>maintenance</html>")})
        result = d.fetch(make_unit(APPLE_URL, Kind.PODCAST))
        assert isinstance(result, Refused)
        assert "unparseable JSON" in evidence_of(result)


class TestSpotifyResolution:
    SEARCH_URL = (
        "https://itunes.apple.com/search?media=podcast&entity=podcastEpisode"
        "&term=Ledgers%20as%20Work%20Queues"
    )

    def test_og_title_search_feed_match(self):
        # Spotify link → og-title → iTunes search → RSS → match.
        d = driver(
            {
                SPOTIFY_URL: html_response(fixture_text("podcast", "spotify-episode.html")),
                self.SEARCH_URL: html_response(fixture_text("podcast", "itunes-search.json")),
                FEED_URL: feed(),
            }
        )
        result = needs_of(d.fetch(make_unit(SPOTIFY_URL, Kind.PODCAST)))
        assert result.need is Need.TRANSCRIBE
        assert result.meta["enclosure"] == ENCLOSURE

    def test_exclusive_fails_honestly_manual(self):
        # Spotify exclusives are not in the podcast index — manual, with
        # the rescue route stated; never dead, never a crash.
        d = driver(
            {
                SPOTIFY_URL: html_response(fixture_text("podcast", "spotify-episode.html")),
                self.SEARCH_URL: html_response('{"resultCount": 0, "results": []}'),
            }
        )
        result = d.fetch(make_unit(SPOTIFY_URL, Kind.PODCAST))
        assert isinstance(result, Unusable)
        assert "Spotify" in evidence_of(result)
        assert "show's own site" in evidence_of(result)

    def test_titleless_page_is_manual(self):
        d = driver({SPOTIFY_URL: html_response("<html><body>consent wall</body></html>")})
        result = d.fetch(make_unit(SPOTIFY_URL, Kind.PODCAST))
        assert isinstance(result, Unusable)
        assert "no episode title" in evidence_of(result)


def feed_item(title: str) -> ET.Element:
    return ET.fromstring(f"<item><title>{title}</title></item>")  # noqa: S314 — test-authored XML


class TestContainmentMatching:
    """The longest normalized match wins — never the first containment hit."""

    def test_itunes_search_prefers_the_longest_match(self):
        payload = {
            "results": [
                {"wrapperType": "podcastEpisode", "trackName": "Episode 1", "trackId": 1},
                {"wrapperType": "podcastEpisode", "trackName": "Episode 10", "trackId": 10},
            ]
        }
        episode = _itunes_episode(payload, title="Episode 10 — The Show")
        assert episode is not None
        assert episode.title == "Episode 10"

    def test_feed_title_match_prefers_the_longest_match(self):
        items = [feed_item(title) for title in ("Episode 1", "Episode 10")]
        match = _match_by_title(items, "Episode 10 — The Show")
        assert match is not None
        title = match.find("title")
        assert title is not None
        assert title.text == "Episode 10"

    def test_exact_feed_title_still_wins_outright(self):
        items = [
            feed_item(title)
            for title in ("Episode 10 — The Show — Extended", "Episode 10 — The Show")
        ]
        match = _match_by_title(items, "Episode 10 — The Show")
        assert match is not None
        title = match.find("title")
        assert title is not None
        assert title.text == "Episode 10 — The Show"


class TestRssIshResolution:
    def test_single_item_feed_resolves_directly(self):
        single = fixture_text("podcast", "feed.xml")
        single = single[: single.index("<item>", single.index("</item>"))] + "</channel></rss>"
        d = driver({"https://feeds.pods.test/one-shot.rss": xml_response(single)})
        result = needs_of(d.fetch(make_unit("https://feeds.pods.test/one-shot.rss", Kind.PODCAST)))
        assert result.meta["enclosure"] == ENCLOSURE

    def test_multi_item_feed_is_a_show_not_an_episode(self):
        d = driver({FEED_URL: feed()})
        result = d.fetch(make_unit(FEED_URL, Kind.PODCAST))
        assert isinstance(result, Unusable)
        assert "capture an episode link" in evidence_of(result)

    def test_indie_episode_page_resolves_via_its_feed_link(self):
        # An explicit .rss URL that actually serves the episode PAGE —
        # the driver follows the page's <link rel> feed pointer and matches.
        page_url = PAGE_URL + ".rss"
        d = driver(
            {
                page_url: html_response(fixture_text("podcast", "episode-page.html")),
                FEED_URL: feed(),
            }
        )
        result = needs_of(d.fetch(make_unit(page_url, Kind.PODCAST)))
        assert result.meta["enclosure"] == ENCLOSURE

    def test_a_redetected_indie_page_prefers_the_feeds_episode(self):
        # The page carries its own audio, but the FEED's notes are richer —
        # so a resolvable feed link wins, and the enclosure is the feed's.
        d = driver({PAGE_URL: indie_page(), FEED_URL: feed()})
        result = needs_of(d.fetch(make_unit(PAGE_URL, Kind.PODCAST)))
        assert result.need is Need.TRANSCRIBE
        assert result.meta["enclosure"] == ENCLOSURE
        assert "Ada Guest" in (result.body or "")  # the feed's show notes

    def test_a_page_whose_feed_is_unreachable_falls_back_to_its_own_audio(self):
        # The web driver routed this unit here on the page's audio; the
        # driver must resolve from that same signal rather than bounce it
        # back, which the run layer would park as a re-detection loop.
        d = driver({PAGE_URL: indie_page(), FEED_URL: OSError("connection reset")})
        result = needs_of(d.fetch(make_unit(PAGE_URL, Kind.PODCAST)))
        assert result.meta["enclosure"] == "https://engineering-distilled.test/audio/ep42.mp3"
        assert result.meta["title"] == "Ledgers as Work Queues"

    def test_a_page_with_no_feed_link_still_resolves_from_its_player(self):
        page = (
            indie_page()
            .text()
            .replace(
                '<link rel="alternate" type="application/rss+xml" title="Engineering Distilled"\n'
                '        href="https://feeds.pods.test/engineering-distilled.rss" />',
                "",
            )
        )
        d = driver({PAGE_URL: html_response(page)})
        result = needs_of(d.fetch(make_unit(PAGE_URL, Kind.PODCAST)))
        assert result.meta["enclosure"] == "https://engineering-distilled.test/audio/ep42.mp3"

    def test_an_og_audio_pointer_counts_as_an_enclosure(self):
        page = (
            '<html><head><meta property="og:title" content="Solo Episode"/>'
            '<meta property="og:audio" content="https://cdn.pods.test/solo.mp3"/>'
            "</head><body>notes</body></html>"
        )
        d = driver({PAGE_URL: html_response(page)})
        result = needs_of(d.fetch(make_unit(PAGE_URL, Kind.PODCAST)))
        assert result.meta["enclosure"] == "https://cdn.pods.test/solo.mp3"

    def test_the_no_feed_fallback_keeps_the_pages_notes(self):
        # The fallback built its park with notes="" while the fetched page
        # was in hand: the park file had an empty body, and if the source
        # died the notes were gone. The page's body goes through the same
        # extraction the feed route's notes do.
        page = (
            '<html><head><meta property="og:title" content="Solo Episode"/>'
            '<meta property="og:audio" content="https://cdn.pods.test/solo.mp3"/>'
            "<script>tracker();</script></head>"
            "<body><h1>Solo Episode</h1><p>We discuss ledgers as work queues, with "
            '<a href="https://example.test/deck">the deck</a>.</p>'
            "<script>player.init();</script></body></html>"
        )
        d = driver({PAGE_URL: html_response(page)})
        result = needs_of(d.fetch(make_unit(PAGE_URL, Kind.PODCAST)))
        assert result.meta["enclosure"] == "https://cdn.pods.test/solo.mp3"
        body = result.body or ""
        assert "We discuss ledgers as work queues" in body
        assert "[the deck](https://example.test/deck)" in body  # links kept, like feed notes
        assert "player.init" not in body  # scripts are code, not notes
        assert "tracker()" not in body

    def test_a_page_with_neither_feed_nor_audio_is_manual(self):
        d = driver({PAGE_URL: html_response("<html><body>just a post</body></html>")})
        result = d.fetch(make_unit(PAGE_URL, Kind.PODCAST))
        assert isinstance(result, Unusable)
        assert "no RSS feed link" in evidence_of(result)

    def test_enclosureless_episode_is_manual(self):
        page_url = "https://engineering-distilled.test/episodes/no-audio.rss"
        d = driver(
            {
                page_url: html_response(
                    '<html><head><meta property="og:title" content="Interview With No Audio"/>'
                    f'<link rel="alternate" type="application/rss+xml" href="{FEED_URL}"/>'
                    "</head></html>"
                ),
                FEED_URL: feed(),
            }
        )
        result = d.fetch(make_unit(page_url, Kind.PODCAST))
        assert isinstance(result, Unusable)
        assert "no audio enclosure" in evidence_of(result)

    def test_feed_fetch_failure_classifies(self):
        d = driver({FEED_URL: OSError("connection reset")})
        result = d.fetch(make_unit(FEED_URL, Kind.PODCAST))
        assert isinstance(result, Refused)
