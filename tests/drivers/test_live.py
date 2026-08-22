"""Live API drift checks — opt-in via ``pytest -m live``.

These hit real endpoints with the real transport. CI and the default run
never execute them (``-m "not live"`` in pyproject); they exist to catch
the world moving under the drivers: fxtwitter's response shape, arxiv's
Atom feed, the wayback availability API.
"""

import json

import pytest

from dex_engine.drivers.github import GitHubDriver
from dex_engine.drivers.paper import PaperDriver
from dex_engine.drivers.podcast import PodcastDriver
from dex_engine.drivers.transport import urllib_transport
from dex_engine.drivers.x import XDriver
from dex_engine.pipeline.types import Kind, Need, Status
from tests.drivers.conftest import body_of, make_unit

pytestmark = pytest.mark.live


class TestFxtwitterShape:
    def test_a_stable_public_post_still_parses(self):
        # x.com/jack/status/20 — "just setting up my twttr", stable since 2006.
        driver = XDriver(transport=urllib_transport)
        result = driver.fetch(make_unit("https://x.com/jack/status/20", Kind.X))
        assert result.status is Status.DONE
        assert "twttr" in body_of(result)
        assert result.meta["author"] is not None


class TestArxivShape:
    def test_the_export_api_still_speaks_atom(self):
        driver = PaperDriver(transport=urllib_transport)
        result = driver.fetch(make_unit("https://arxiv.org/abs/1706.03762", Kind.PAPER))
        assert result.status is Status.DONE
        assert "Attention" in str(result.meta["title"])
        assert "## Abstract" in body_of(result)


class TestItunesLookupShape:
    def test_an_apple_episode_link_resolves_through_the_show_lookup(self):
        # Lex Fridman #474. The lookup API resolves SHOW ids only — handed
        # the ?i= episode id it answers resultCount 0 — so the episode is
        # matched by trackId inside the show's returned episode window.
        url = (
            "https://podcasts.apple.com/us/podcast/lex-fridman-podcast/"
            "id1434243584?i=1000716969529"
        )
        result = PodcastDriver(transport=urllib_transport).fetch(make_unit(url, Kind.PODCAST))
        assert result.status is Status.WAITING
        assert result.needs is Need.TRANSCRIBE
        assert str(result.meta["enclosure"]).startswith("http")


class TestGitHubContentsShape:
    BLOB = "https://github.com/octocat/Hello-World/blob/master/"
    QUALIFIED_BLOB = "https://github.com/octocat/Hello-World/blob/refs/heads/master/"
    SLASHED_BLOB = "https://github.com/rust-lang/rust/blob/automation/bors/auto/README.md"

    def test_a_public_blob_still_arrives_base64_through_gh(self):
        # octocat/Hello-World's README, stable since 2011.
        result = GitHubDriver().fetch(make_unit(self.BLOB + "README", Kind.GITHUB))
        assert result.status is Status.DONE
        assert "Hello World!" in body_of(result)

    def test_a_path_that_does_not_exist_is_still_a_gh_404(self):
        result = GitHubDriver().fetch(make_unit(self.BLOB + "no-such-file.txt", Kind.GITHUB))
        assert result.status is Status.DEAD

    def test_the_contents_api_still_accepts_a_fully_qualified_ref(self):
        # The `blob/refs/heads/<branch>/` permalink form is passed through as
        # `?ref=refs/heads/master`; the driver's free split depends on the
        # API continuing to take a qualified ref, not just a bare name.
        result = GitHubDriver().fetch(make_unit(self.QUALIFIED_BLOB + "README", Kind.GITHUB))
        assert result.status is Status.DONE
        assert "Hello World!" in body_of(result)

    def test_an_lfs_tracked_blob_still_arrives_as_its_pointer(self):
        # The contents API serves Git-LFS pointer text, never the object it
        # names. The blob route depends on that staying true: the pointer is
        # clean UTF-8 with no signature, so only the filename keeps its 130
        # bytes of `oid sha256:…` from being fenced as the document.
        url = "https://github.com/sarabander/sicp-pdf/blob/master/sicp.pdf"
        result = GitHubDriver().fetch(make_unit(url, Kind.GITHUB))
        assert result.status is Status.MANUAL
        assert "is a pdf document" in str(result.reason)
        assert result.body is None

    def test_a_slashed_branch_resolves_through_matching_refs(self):
        # rust-lang/rust's bors branches are the routine slashed-name shape.
        # The ref/path boundary is unrecoverable from the URL, so this pins
        # `git/matching-refs` answering `refs/heads/automation/bors/auto`.
        result = GitHubDriver().fetch(make_unit(self.SLASHED_BLOB, Kind.GITHUB))
        assert result.status is Status.DONE
        assert result.meta["file"] == "README.md"
        assert "Rust" in body_of(result)


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


class TestWaybackShape:
    def test_the_availability_api_shape_holds(self):
        lookup = "https://archive.org/wayback/available?url=example.com"
        response = urllib_transport(lookup)
        assert response.ok
        payload = json.loads(response.text())
        assert "archived_snapshots" in payload
