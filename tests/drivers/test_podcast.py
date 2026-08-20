"""Tests for the podcast driver (§9): resolution paths, honest failures."""

import xml.etree.ElementTree as ET

from dex_engine.drivers.podcast import PodcastDriver, _itunes_episode, _match_by_title
from dex_engine.drivers.transport import HttpResponse
from dex_engine.pipeline.types import Kind, Need, Status
from tests.drivers.conftest import FakeTransport, fixture_text, html_response, make_unit, reason_of

APPLE_URL = "https://podcasts.apple.com/us/podcast/engineering-distilled/id9990001?i=8880042"
SPOTIFY_URL = "https://open.spotify.com/episode/4rOoJ6Egrf8K2IrywzwOMk"
FEED_URL = "https://feeds.pods.test/engineering-distilled.rss"
PAGE_URL = "https://engineering-distilled.test/episodes/ledgers-as-work-queues"
LOOKUP_URL = "https://itunes.apple.com/lookup?id=8880042&entity=podcastEpisode"
ENCLOSURE = "https://cdn.pods.test/ed/ep42.mp3?sig=abc123"


def xml_response(text: str) -> HttpResponse:
    return HttpResponse(status=200, content_type="application/rss+xml", body=text.encode())


def driver(responses: dict) -> PodcastDriver:
    return PodcastDriver(transport=FakeTransport(responses))


def feed() -> HttpResponse:
    return xml_response(fixture_text("podcast", "feed.xml"))


class TestDetection:
    def test_matches_apple_spotify_and_rss_ish(self):
        d = PodcastDriver()
        assert d.matches(APPLE_URL)
        assert d.matches(SPOTIFY_URL)
        assert d.matches(FEED_URL)
        assert d.matches("https://example.test/podcast/feed")
        assert not d.matches("https://open.spotify.com/track/abc")  # music, not an episode
        assert not d.matches("https://example.test/blog/post")

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
        # §9: Apple link → iTunes lookup (keyless) → show RSS → match → enclosure.
        d = driver(
            {
                LOOKUP_URL: html_response(fixture_text("podcast", "itunes-lookup.json")),
                FEED_URL: feed(),
            }
        )
        result = d.fetch(make_unit(APPLE_URL, Kind.PODCAST))
        assert result.status is Status.WAITING
        assert result.needs is Need.TRANSCRIBE
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
        result = d.fetch(make_unit(APPLE_URL, Kind.PODCAST))
        assert result.body is not None
        assert "[Ada Guest](https://example.test/guest)" in result.body  # harvestable (§9)
        assert "- Why JSONL merges" in result.body

    def test_show_link_without_episode_param_is_manual(self):
        d = driver({})
        show_url = "https://podcasts.apple.com/us/podcast/engineering/id9990001"
        unit = make_unit(show_url, Kind.PODCAST)
        result = d.fetch(unit)
        assert result.status is Status.MANUAL
        assert "show, not an episode" in reason_of(result)

    def test_unknown_episode_in_lookup_is_manual(self):
        d = driver({LOOKUP_URL: html_response('{"resultCount": 0, "results": []}')})
        result = d.fetch(make_unit(APPLE_URL, Kind.PODCAST))
        assert result.status is Status.MANUAL
        assert "iTunes lookup" in reason_of(result)

    def test_episode_pulled_from_feed_is_manual(self):
        pulled = fixture_text("podcast", "itunes-lookup.json").replace("ep-42-guid", "gone-guid")
        pulled = pulled.replace("Ledgers as Work Queues", "An Episode The Feed Lost")
        d = driver({LOOKUP_URL: html_response(pulled), FEED_URL: feed()})
        result = d.fetch(make_unit(APPLE_URL, Kind.PODCAST))
        assert result.status is Status.MANUAL
        assert "not in the show's feed" in reason_of(result)

    def test_lookup_http_failure_classifies_never_dies(self):
        d = driver({LOOKUP_URL: HttpResponse(status=503, content_type="text/html", body=b"")})
        result = d.fetch(make_unit(APPLE_URL, Kind.PODCAST))
        assert result.status is Status.BLOCKED
        assert reason_of(result) == "HTTP 503"

    def test_unparseable_itunes_json_is_blocked(self):
        d = driver({LOOKUP_URL: html_response("<html>maintenance</html>")})
        result = d.fetch(make_unit(APPLE_URL, Kind.PODCAST))
        assert result.status is Status.BLOCKED
        assert "unparseable JSON" in reason_of(result)


class TestSpotifyResolution:
    SEARCH_URL = (
        "https://itunes.apple.com/search?media=podcast&entity=podcastEpisode"
        "&term=Ledgers%20as%20Work%20Queues"
    )

    def test_og_title_search_feed_match(self):
        # §9: Spotify link → og-title → iTunes search → RSS → match.
        d = driver(
            {
                SPOTIFY_URL: html_response(fixture_text("podcast", "spotify-episode.html")),
                self.SEARCH_URL: html_response(fixture_text("podcast", "itunes-search.json")),
                FEED_URL: feed(),
            }
        )
        result = d.fetch(make_unit(SPOTIFY_URL, Kind.PODCAST))
        assert result.status is Status.WAITING
        assert result.needs is Need.TRANSCRIBE
        assert result.meta["enclosure"] == ENCLOSURE

    def test_exclusive_fails_honestly_manual(self):
        # §9: Spotify exclusives are not in the podcast index — manual, with
        # the rescue route stated; never dead, never a crash.
        d = driver(
            {
                SPOTIFY_URL: html_response(fixture_text("podcast", "spotify-episode.html")),
                self.SEARCH_URL: html_response('{"resultCount": 0, "results": []}'),
            }
        )
        result = d.fetch(make_unit(SPOTIFY_URL, Kind.PODCAST))
        assert result.status is Status.MANUAL
        assert "Spotify" in reason_of(result)
        assert "show's own site" in reason_of(result)

    def test_titleless_page_is_manual(self):
        d = driver({SPOTIFY_URL: html_response("<html><body>consent wall</body></html>")})
        result = d.fetch(make_unit(SPOTIFY_URL, Kind.PODCAST))
        assert result.status is Status.MANUAL
        assert "no episode title" in reason_of(result)


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
        result = d.fetch(make_unit("https://feeds.pods.test/one-shot.rss", Kind.PODCAST))
        assert result.status is Status.WAITING
        assert result.meta["enclosure"] == ENCLOSURE

    def test_multi_item_feed_is_a_show_not_an_episode(self):
        d = driver({FEED_URL: feed()})
        result = d.fetch(make_unit(FEED_URL, Kind.PODCAST))
        assert result.status is Status.MANUAL
        assert "capture an episode link" in reason_of(result)

    def test_indie_episode_page_resolves_via_its_feed_link(self):
        # §9: indie episode page → <link rel> in head → feed → match.
        page_url = PAGE_URL + "/feed"  # RSS-ish enough to reach this driver
        d = driver(
            {
                page_url: html_response(fixture_text("podcast", "episode-page.html")),
                FEED_URL: feed(),
            }
        )
        result = d.fetch(make_unit(page_url, Kind.PODCAST))
        assert result.status is Status.WAITING
        assert result.meta["enclosure"] == ENCLOSURE

    def test_enclosureless_episode_is_manual(self):
        d = driver(
            {
                "https://feeds.pods.test/engineering-distilled.rss/feed": html_response(
                    '<html><head><meta property="og:title" content="Interview With No Audio"/>'
                    f'<link rel="alternate" type="application/rss+xml" href="{FEED_URL}"/>'
                    "</head></html>"
                ),
                FEED_URL: feed(),
            }
        )
        unit = make_unit("https://feeds.pods.test/engineering-distilled.rss/feed", Kind.PODCAST)
        result = d.fetch(unit)
        assert result.status is Status.MANUAL
        assert "no audio enclosure" in reason_of(result)

    def test_feed_fetch_failure_classifies(self):
        d = driver({FEED_URL: OSError("connection reset")})
        result = d.fetch(make_unit(FEED_URL, Kind.PODCAST))
        assert result.status is Status.BLOCKED
