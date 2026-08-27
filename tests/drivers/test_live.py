"""Live API drift checks — opt-in via ``pytest -m live``.

These hit real endpoints with the real transport. CI and the default run
never execute them (``-m "not live"`` in pyproject); they exist to catch
the world moving under the drivers: fxtwitter's response shape, arxiv's
Atom feed, the wayback availability API.

A scheduled workflow runs them daily. The ones additionally marked
``ci_hostile`` run there best-effort, because their endpoint answers a
datacenter IP differently from a laptop and a red run every morning is a
red run nobody reads; the rest are trusted to mean something.
"""

import json
import time

import pytest

from dex_engine.drivers.gh import Blob, blob_ref, fetch_blob, run_gh
from dex_engine.drivers.github import GitHubDriver
from dex_engine.drivers.instagram import (
    DEFAULT_BASE_URL,
    PROBE_SLEEP,
    _canonical_code,
    _Item,
    _og_tags,
    _parse_post,
    _Post,
    _typed_item,
)
from dex_engine.drivers.paper import PaperDriver
from dex_engine.drivers.podcast import PodcastDriver
from dex_engine.drivers.transport import HttpResponse, bot_transport, urllib_transport
from dex_engine.drivers.web import WebDriver
from dex_engine.drivers.x import XDriver
from dex_engine.pipeline.types import Content, Format, Kind, Missing, Need, Redetected
from tests.drivers.conftest import body_of, content_of, make_unit, needs_of

pytestmark = pytest.mark.live


@pytest.mark.ci_hostile  # fxtwitter proxies Twitter's guest API and has gone
# down for days at a stretch; from CI a failure cannot be told from an outage.
class TestFxtwitterShape:
    def test_a_stable_public_post_still_parses(self):
        # x.com/jack/status/20 — "just setting up my twttr", stable since 2006.
        driver = XDriver(transport=urllib_transport)
        result = content_of(driver.fetch(make_unit("https://x.com/jack/status/20", Kind.X)))
        assert "twttr" in body_of(result)
        assert result.meta["author"] is not None


class TestArxivShape:
    def test_the_export_api_still_speaks_atom(self):
        driver = PaperDriver(transport=urllib_transport)
        result = content_of(driver.fetch(make_unit("https://arxiv.org/abs/1706.03762", Kind.PAPER)))
        assert "Attention" in str(result.meta["title"])
        assert "## Abstract" in body_of(result)


class TestItunesLookupShape:
    def test_an_apple_episode_link_resolves_through_the_show_lookup(self):
        # Lex Fridman #474. The lookup API resolves SHOW ids only — handed
        # the ?i= episode id it answers resultCount 0 — so the episode is
        # matched by trackId inside the show's returned episode window.
        url = (
            "https://podcasts.apple.com/us/podcast/lex-fridman-podcast/id1434243584?i=1000716969529"
        )
        result = needs_of(
            PodcastDriver(transport=urllib_transport).fetch(make_unit(url, Kind.PODCAST))
        )
        assert result.need is Need.TRANSCRIBE
        assert str(result.meta["enclosure"]).startswith("http")


class TestGitHubContentsShape:
    BLOB = "https://github.com/octocat/Hello-World/blob/master/"
    QUALIFIED_BLOB = "https://github.com/octocat/Hello-World/blob/refs/heads/master/"
    SLASHED_BLOB = "https://github.com/rust-lang/rust/blob/automation/bors/auto/README.md"

    def test_a_public_blob_still_arrives_base64_through_gh(self):
        # octocat/Hello-World's README, stable since 2011.
        result = GitHubDriver().fetch(make_unit(self.BLOB + "README", Kind.GITHUB))
        assert "Hello World!" in body_of(result)

    def test_a_path_that_does_not_exist_is_still_a_gh_404(self):
        result = GitHubDriver().fetch(make_unit(self.BLOB + "no-such-file.txt", Kind.GITHUB))
        assert isinstance(result, Missing)

    def test_the_contents_api_still_accepts_a_fully_qualified_ref(self):
        # The `blob/refs/heads/<branch>/` permalink form is passed through as
        # `?ref=refs/heads/master`; the driver's free split depends on the
        # API continuing to take a qualified ref, not just a bare name.
        result = GitHubDriver().fetch(make_unit(self.QUALIFIED_BLOB + "README", Kind.GITHUB))
        assert "Hello World!" in body_of(result)

    def test_an_lfs_tracked_blob_still_arrives_as_its_pointer(self):
        # The contents API serves Git-LFS pointer text, never the object it
        # names. Asserted on the seam's bytes, because no status downstream
        # can show it: a pointer and the real document both re-detect to
        # pdf work, and only the bytes say which one arrived.
        url = "https://github.com/sarabander/sicp-pdf/blob/master/sicp.pdf"
        ref = blob_ref(url)
        assert ref is not None
        blob = fetch_blob(run_gh, ref)
        assert isinstance(blob, Blob)
        assert blob.data.startswith(b"version https://git-lfs.github.com/spec/v1")

    def test_an_lfs_pointer_is_never_fenced_as_the_document(self):
        # The floor the pointer must not fall through: whatever the driver
        # decides, it must not present 130 bytes of `oid sha256:…` as the
        # document they stand for.
        url = "https://github.com/sarabander/sicp-pdf/blob/master/sicp.pdf"
        result = GitHubDriver().fetch(make_unit(url, Kind.GITHUB))
        assert result == Redetected(kind=Kind.FILE, format=Format.PDF)  # identity, no body

    def test_a_slashed_branch_resolves_through_matching_refs(self):
        # rust-lang/rust's bors branches are the routine slashed-name shape.
        # The ref/path boundary is unrecoverable from the URL, so this pins
        # `git/matching-refs` answering `refs/heads/automation/bors/auto`.
        result = content_of(GitHubDriver().fetch(make_unit(self.SLASHED_BLOB, Kind.GITHUB)))
        assert result.meta["file"] == "README.md"
        assert "Rust" in body_of(result)


@pytest.mark.ci_hostile  # youtube.com serves datacenter IPs a consent or
# bot-check interstitial instead of the channel page, so the assertions here
# fail from a runner for reasons that say nothing about the driver.
class TestYouTubeRootNamespace:
    """The bare-name guard rests on who owns youtube.com's root namespace."""

    def test_a_legacy_vanity_name_is_still_a_channel_address(self):
        # /veritasium — a grandfathered vanity URL, no /@ and no /c/ prefix.
        # If the root namespace ever stopped meaning "channel", the driver's
        # functional-path allowlist would be inverted.
        response = urllib_transport("https://www.youtube.com/veritasium")
        assert response.ok
        assert "/channel/UC" in response.text()

    def test_an_unclaimed_bare_name_is_a_404_not_a_product_page(self):
        assert not urllib_transport("https://www.youtube.com/zzqqxxnotachannel1234").ok


class TestPageAudioIsNotAnEpisode:
    def test_an_encyclopedia_article_with_embedded_audio_still_extracts(self):
        # en.wikipedia.org/wiki/Podcast embeds media samples in a 60k-char
        # article — the live shape the over-broad <audio> signal discarded.
        # Real markup, because this is exactly what a fixture cannot pin.
        driver = WebDriver(transport=urllib_transport)
        result = driver.fetch(make_unit("https://en.wikipedia.org/wiki/Podcast", Kind.WEB))
        assert isinstance(result, Content)  # extracted, never a Redetected bounce
        assert len(body_of(result)) > 10_000


@pytest.mark.ci_hostile  # archive.org's availability endpoint rate-limits and
# has multi-day outages; its uptime is not a fact about this repo.
class TestWaybackShape:
    def test_the_availability_api_shape_holds(self):
        lookup = "https://archive.org/wayback/available?url=example.com"
        response = urllib_transport(lookup)
        assert response.ok
        payload = json.loads(response.text())
        assert "archived_snapshots" in payload


# The world-record egg: public since 2019, the most-liked post on the platform,
# and a single image — which makes its index 2 the out-of-range answer the probe
# walk ends on. natgeo's reel is the video index; neither account goes private.
EGG_CODE = "BsOGulcndj-"
EGG_POST = f"https://www.instagram.com/p/{EGG_CODE}/"
REEL_CODE = "DHVrPLrIyQ_"


@pytest.mark.ci_hostile  # instagram.com serves an anonymous fetcher at its own
# discretion, and a datacenter IP is who it is likeliest to answer with a login
# wall where a laptop gets the og: set — a red run from a runner would be about
# where the request came from, not about the page.
class TestInstagramOgShape:
    """The caption track rests on instagram.com answering a link-preview bot with og: metadata."""

    def test_a_stable_public_post_still_serves_its_og_set(self):
        response = bot_transport(EGG_POST)
        assert response.ok
        page = response.text()
        assert {"og:title", "og:description"} <= _og_tags(page).keys()
        post = _parse_post(page, requested=EGG_CODE)
        assert isinstance(post, _Post), f"expected a parsed post, got {post!r}"
        assert post.display_name  # og:title still spells `<name> on Instagram: "<caption>"`
        assert post.caption
        assert post.handle == "world_record_egg"
        assert post.posted == "January 4, 2019"
        assert _canonical_code(page) == EGG_CODE


@pytest.mark.ci_hostile  # the media proxy is an unmaintained community freebie —
# its upstream is archived and ddinstagram.com already died — so it rate-limits,
# and one day it stops answering for good. Either is a fact about that host's
# life expectancy, not about this repo.
class TestInstagramProbeShapes:
    """The probe walk rests on the proxy typing each media index and ending in a zero-byte 200."""

    def probe(self, family: str, code: str, index: int) -> HttpResponse:
        """One probe as the walk makes it: paced, and one byte of body — never the media."""
        time.sleep(PROBE_SLEEP)
        return bot_transport(f"{DEFAULT_BASE_URL}/{family}/{code}/{index}", limit=0)

    def test_a_video_index_still_types_as_video(self):
        response = self.probe("videos", REEL_CODE, 1)
        assert response.ok
        assert _typed_item(response.content_type) is _Item.VIDEO

    def test_an_image_index_still_types_as_image(self):
        # The /images/ spelling is what a fetched post carries as its media, so
        # it has to serve the bytes long after the walk typed the post.
        response = self.probe("images", EGG_CODE, 1)
        assert response.ok
        assert _typed_item(response.content_type) is _Item.IMAGE

    def test_an_index_past_the_end_is_still_an_empty_200(self):
        # The walk's only terminator: out of range is never a 404.
        response = self.probe("images", EGG_CODE, 2)
        assert response.ok
        assert not response.body
