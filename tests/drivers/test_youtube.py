"""Tests for drivers/youtube.py: captions, waiting parks, probe classification."""

import datetime
import itertools
import json

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from dex_engine.drivers.transport import HttpResponse
from dex_engine.drivers.youtube import YouTubeDriver, clean_vtt
from dex_engine.drivers.ytdlp import ProbeError, YoutubeAudio
from dex_engine.pipeline.classify import PAYWALL_REASON
from dex_engine.pipeline.enrichment import youtube_body
from dex_engine.pipeline.transcribe import Acquired, acquire_youtube_audio
from dex_engine.pipeline.types import (
    Content,
    Kind,
    LedgerEntry,
    Missing,
    Need,
    NeedsCapability,
    Refused,
    Status,
    Unusable,
)
from dex_engine.pipeline.urls import base_canonical, work_hash
from tests.drivers.conftest import (
    FakeTransport,
    body_of,
    content_of,
    fixture_text,
    make_unit,
    needs_of,
)

URL = "https://youtube.com/watch?v=dQw4w9WgXcA"
INFO_WITH = json.loads(fixture_text("youtube", "info-with-captions.json"))
INFO_WITHOUT = json.loads(fixture_text("youtube", "info-without-captions.json"))
VTT = fixture_text("youtube", "captions.vtt")
TRACK_URL = "https://captions.example.test/track.vtt"


def vtt_response(text: str) -> HttpResponse:
    return HttpResponse(status=200, content_type="text/vtt", body=text.encode("utf-8"))


def driver_for(info: dict | Exception, responses: dict | None = None) -> YouTubeDriver:
    def probe(_url: str) -> dict:
        if isinstance(info, Exception):
            raise info
        return info

    return YouTubeDriver(probe=probe, transport=FakeTransport(responses or {}))


class TestIdentity:
    def test_kind_and_sleep(self):
        driver = driver_for({})
        assert driver.kind is Kind.YOUTUBE
        assert driver.sleep == 2.0

    @pytest.mark.parametrize(
        "url",
        [
            "https://youtube.com/watch?v=a",
            "https://www.youtube.com/watch?v=a",
            "https://m.youtube.com/watch?v=a",
            "https://youtu.be/a",
        ],
    )
    def test_matches_youtube_hosts_including_mobile(self, url):
        assert driver_for({}).matches(url)

    def test_does_not_match_other_hosts(self):
        assert not driver_for({}).matches("https://vimeo.com/123")

    def test_canonical_rewrites_short_links_and_keeps_only_v(self):
        driver = driver_for({})
        assert driver.canonical("https://youtu.be/dQw4?si=share123") == (
            "https://youtube.com/watch?v=dQw4"
        )
        assert driver.canonical("https://m.youtube.com/watch?v=dQw4&t=42&feature=x") == (
            "https://youtube.com/watch?v=dQw4"
        )

    def test_canonical_is_idempotent_across_the_rewrite(self):
        driver = driver_for({})
        once = driver.canonical("https://youtu.be/dQw4?si=x")
        assert driver.canonical(once) == once


# Real YouTube video ids are 11 chars of this alphabet; 8+ keeps generated
# ids clear of the live/shorts/embed path prefixes, and "videoseries" is
# excluded because /embed/videoseries is the playlist-embed shape, not a
# video that could carry the name.
_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
    min_size=8,
    max_size=11,
).filter(lambda video_id: video_id != "videoseries")


def _video_shapes(video_id: str) -> list[str]:
    return [
        f"https://youtube.com/watch?v={video_id}",
        f"https://www.youtube.com/watch?v={video_id}&t=42",
        f"https://m.youtube.com/watch?v={video_id}",
        f"https://youtu.be/{video_id}?si=share123",
        f"https://youtu.be/live/{video_id}",
        f"https://youtu.be/shorts/{video_id}",
        f"https://youtu.be/embed/{video_id}",
        f"https://youtube.com/live/{video_id}",
        f"https://youtube.com/shorts/{video_id}",
        f"https://youtube.com/embed/{video_id}",
        f"https://youtube.com/v/{video_id}",
    ]


class TestIdentityProperties:
    @given(_ids)
    def test_every_shape_of_a_video_is_one_fetchable_work_unit(self, video_id):
        driver = driver_for({})
        canonicals = {driver.canonical(shape) for shape in _video_shapes(video_id)}
        assert canonicals == {f"https://youtube.com/watch?v={video_id}"}

    @given(_ids, _ids)
    def test_different_videos_stay_different_work_units(self, id_a, id_b):
        assume(id_a != id_b)
        driver = driver_for({})
        hashes_a = {work_hash(driver.canonical(s)) for s in _video_shapes(id_a)}
        hashes_b = {work_hash(driver.canonical(s)) for s in _video_shapes(id_b)}
        assert hashes_a.isdisjoint(hashes_b)

    @given(_ids, _ids)
    def test_different_playlists_stay_different_work_units(self, id_a, id_b):
        assume(id_a != id_b)
        driver = driver_for({})
        canonical_a = driver.canonical(f"https://youtube.com/playlist?list=PL{id_a}")
        canonical_b = driver.canonical(f"https://www.youtube.com/playlist?list=PL{id_b}")
        assert canonical_a == f"https://youtube.com/playlist?list=PL{id_a}"
        assert work_hash(canonical_a) != work_hash(canonical_b)

    @given(_ids)
    def test_a_playlist_and_a_video_never_collide(self, some_id):
        driver = driver_for({})
        video = driver.canonical(f"https://youtube.com/watch?v={some_id}")
        playlist = driver.canonical(f"https://youtube.com/playlist?list={some_id}")
        assert work_hash(video) != work_hash(playlist)


class TestPlaylists:
    def test_playlist_identity_is_the_list_id_share_noise_dropped(self):
        driver = driver_for({})
        assert driver.canonical("https://www.youtube.com/playlist?list=PLabc&si=share123") == (
            "https://youtube.com/playlist?list=PLabc"
        )

    def test_playlist_parks_manual_with_reason_and_never_probes(self):
        driver = driver_for(AssertionError("the probe must not run for a playlist"))
        result = driver.fetch(make_unit("https://youtube.com/playlist?list=PLabc", Kind.YOUTUBE))
        assert isinstance(result, Unusable)
        assert result.rescuable  # the members are there for judgment to pick
        assert "playlist" in result.evidence

    def test_playlist_embed_identity_is_the_list_id(self):
        driver = driver_for({})
        assert driver.canonical(
            "https://www.youtube.com/embed/videoseries?list=PLabc&si=share123"
        ) == driver.canonical("https://youtube.com/playlist?list=PLabc")

    def test_two_playlist_embeds_stay_distinct_park_manual_and_never_probe(self):
        driver = driver_for(AssertionError("the probe must not run for a playlist embed"))
        canonicals = {
            driver.canonical(f"https://youtube.com/embed/videoseries?list={list_id}")
            for list_id in ("PLfirst", "PLsecond")
        }
        assert canonicals == {
            "https://youtube.com/playlist?list=PLfirst",
            "https://youtube.com/playlist?list=PLsecond",
        }
        for canonical in canonicals:
            result = driver.fetch(make_unit(canonical, Kind.YOUTUBE))
            assert isinstance(result, Unusable)
            assert "playlist" in result.evidence

    def test_an_embedded_single_video_is_still_its_video(self):
        driver = driver_for({})
        assert driver.canonical("https://youtube.com/embed/dQw4w9WgXcA") == (
            "https://youtube.com/watch?v=dQw4w9WgXcA"
        )

    def test_the_youtu_be_videoseries_shape_is_neither_video_nor_playlist(self):
        # No such shape exists in the wild; prove it by not matching:
        # "videoseries" never leaks as a video id, and the playlist form
        # is youtube.com-only, so the URL keeps its base identity.
        driver = driver_for({})
        assert driver.canonical("https://youtu.be/embed/videoseries?list=PLabc") == (
            "https://youtu.be/embed/videoseries?list=PLabc"
        )


CHANNEL_SHAPES = [
    "https://youtube.com/@systemsfieldnotes",
    "https://www.youtube.com/@systemsfieldnotes/videos",
    "https://youtube.com/@systemsfieldnotes/shorts",
    "https://m.youtube.com/@systemsfieldnotes/streams",
    "https://youtube.com/@systemsfieldnotes/playlists",
    "https://youtube.com/user/systemsfieldnotes",
    "https://youtube.com/user/systemsfieldnotes/videos",
    "https://youtube.com/c/SystemsFieldNotes",
    "https://youtube.com/channel/UCabc123def456ghi789jkl",
    "https://youtube.com/channel/UCabc123def456ghi789jkl/videos",
    # The legacy vanity form: a bare channel name at the root, and its tabs.
    "https://www.youtube.com/veritasium",
    "https://www.youtube.com/veritasium/videos",
    "https://youtube.com/SystemsFieldNotes/playlists",
    # @handles arrive percent-encoded from shorteners and clipboards.
    "https://www.youtube.com/%40jamesbriggs",
    "https://www.youtube.com/%40jamesbriggs/videos",
]
SEARCH_URL = "https://www.youtube.com/results?search_query=ledgers+as+work+queues"
HASHTAG_URL = "https://www.youtube.com/hashtag/ledgers"

# Functional paths the guard must never claim: every one of them is a
# youtube.com surface, not a channel, and stealing one would park a real
# video or page `manual` instead of fetching it.
FUNCTIONAL_URLS = [
    "https://youtube.com/watch?v=dQw4w9WgXcA",
    "https://youtube.com/playlist?list=PLabc123",
    "https://www.youtube.com/feed/subscriptions",
    "https://youtube.com/shorts/dQw4w9WgXcA",
    "https://youtube.com/live/dQw4w9WgXcA",
    "https://youtube.com/embed/dQw4w9WgXcA",
    "https://youtube.com/v/dQw4w9WgXcA",
    "https://www.youtube.com/about",
    "https://www.youtube.com/t/terms",
    "https://www.youtube.com/premium",
    "https://www.youtube.com/howyoutubeworks/product-features",
]


class TestChannelsAndSearch:
    """Collections park before the probe: yt-dlp enumerates whole channels."""

    @pytest.mark.parametrize("url", CHANNEL_SHAPES)
    def test_channel_shapes_park_manual_and_never_probe(self, url):
        # The probe raises: a channel URL driven as a video enumerated 63
        # videos in 87 seconds, and the transcribe drain would then pull
        # all 63 audio files over one filename.
        driver = driver_for(AssertionError("the probe must not run for a channel"))
        result = driver.fetch(make_unit(url, Kind.YOUTUBE))
        assert isinstance(result, Unusable)
        assert "a channel is a collection" in result.evidence

    def test_search_results_park_manual_and_never_probe(self):
        driver = driver_for(AssertionError("the probe must not run for a search page"))
        result = driver.fetch(make_unit(SEARCH_URL, Kind.YOUTUBE))
        assert isinstance(result, Unusable)
        assert "a result page is a collection" in result.evidence

    def test_a_hashtag_feed_parks_manual_and_never_probes(self):
        driver = driver_for(AssertionError("the probe must not run for a hashtag feed"))
        result = driver.fetch(make_unit(HASHTAG_URL, Kind.YOUTUBE))
        assert isinstance(result, Unusable)
        assert "a hashtag feed is a collection" in result.evidence

    @pytest.mark.parametrize("url", [*CHANNEL_SHAPES, SEARCH_URL, HASHTAG_URL])
    def test_collection_urls_keep_the_generic_canonical(self, url):
        assert driver_for({}).canonical(url) == base_canonical(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://youtube.com/@systemsfieldnotes/live",
            "https://youtube.com/channel/UCabc123def456ghi789jkl/live",
            "https://www.youtube.com/veritasium/live",
            "https://www.youtube.com/%40jamesbriggs/live",
        ],
    )
    def test_a_channels_live_path_is_one_video_and_still_probes(self, url):
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(VTT)})
        assert isinstance(driver.fetch(make_unit(url, Kind.YOUTUBE)), Content)

    @pytest.mark.parametrize("url", FUNCTIONAL_URLS)
    def test_a_functional_path_is_never_read_as_a_collection(self, url):
        # The counter-pressure to the bare-name rule: youtube.com's own
        # paths are one segment too, and parking /watch or /feed as a
        # channel would be the same bug pointed the other way.
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(VTT)})
        outcome = driver.fetch(make_unit(url, Kind.YOUTUBE))
        assert not (isinstance(outcome, Unusable) and "collection" in outcome.evidence)

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/about",
            "https://www.youtube.com/t/terms",
            "https://www.youtube.com/feed/subscriptions",
            "https://www.youtube.com/premium",
        ],
    )
    def test_a_non_video_functional_page_still_reaches_the_probe(self, url):
        driver = driver_for(AssertionError("the probe must run for a functional path"))
        with pytest.raises(AssertionError, match="the probe must run"):
            driver.fetch(make_unit(url, Kind.YOUTUBE))

    @pytest.mark.parametrize(
        "url",
        [
            "https://youtube.com/watch?v=dQw4w9WgXcA",
            "https://youtube.com/shorts/dQw4w9WgXcA",
            "https://youtube.com/live/dQw4w9WgXcA",
            "https://youtube.com/v/dQw4w9WgXcA",
        ],
    )
    def test_single_video_shapes_still_fetch(self, url):
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(VTT)})
        assert isinstance(driver.fetch(make_unit(url, Kind.YOUTUBE)), Content)

    def test_a_video_whose_id_looks_like_a_channel_prefix_still_probes(self):
        # /c/<name> is a channel but /v/<id> is a video: the pair shapes
        # must not bleed into each other.
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(VTT)})
        url = "https://youtube.com/v/dQw4w9WgXcA"
        assert isinstance(driver.fetch(make_unit(url, Kind.YOUTUBE)), Content)


class TestCaptions:
    def test_captions_become_description_and_transcript_sections(self):
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(VTT)})
        result = driver.fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Content)
        body = body_of(result)
        assert body.startswith("## Description")
        assert "## Transcript" in body
        assert "not receipts, they are the queue" in body

    def test_a_captioned_video_with_no_description_still_labels_its_transcript(self):
        # The transcript is its own labelled section, description or not:
        # the drain splits a stored body on that heading, so a bare
        # transcript comes back as "description" and duplicates itself.
        info = {k: v for k, v in INFO_WITH.items() if k != "description"}
        driver = driver_for(info, {TRACK_URL: vtt_response(VTT)})
        body = body_of(driver.fetch(make_unit(URL, Kind.YOUTUBE)))
        assert body.startswith("## Transcript\n\n")
        assert "## Description" not in body

    def test_vtt_is_cleaned_tags_cues_and_duplicates_stripped(self):
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(VTT)})
        body = body_of(driver.fetch(make_unit(URL, Kind.YOUTUBE)))
        assert "-->" not in body
        assert "<c." not in body
        assert "WEBVTT" not in body
        assert body.count("So the first thing to understand") == 1

    def test_real_subtitles_win_over_automatic_captions(self):
        transport = FakeTransport({TRACK_URL: vtt_response(VTT)})
        YouTubeDriver(probe=lambda _url: INFO_WITH, transport=transport).fetch(
            make_unit(URL, Kind.YOUTUBE)
        )
        assert transport.calls == [("GET", TRACK_URL)]  # never auto.vtt

    def test_meta_carries_title_channel_duration_upload_date(self):
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(VTT)})
        meta = content_of(driver.fetch(make_unit(URL, Kind.YOUTUBE))).meta
        assert meta["title"] == "Ledgers as Work Queues: a Field Report"
        assert meta["channel"] == "Systems Field Notes"
        assert meta["duration_min"] == 13
        assert meta["upload_date"] == "20260810"

    def test_unknown_duration_is_omitted_never_zero(self):
        # Parity with the transcribe drain's route: an unknown duration is
        # None (omitted from frontmatter), not a fabricated 0.
        info = {k: v for k, v in INFO_WITH.items() if k != "duration"}
        driver = driver_for(info, {TRACK_URL: vtt_response(VTT)})
        meta = content_of(driver.fetch(make_unit(URL, Kind.YOUTUBE))).meta
        assert meta["duration_min"] is None

    def test_a_captions_transcript_stamps_its_provenance(self):
        # Frontmatter, not the heading, says a body holds a transcript.
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(VTT)})
        meta = content_of(driver.fetch(make_unit(URL, Kind.YOUTUBE))).meta
        assert meta["via"] == "captions"


def _park_file(path, outcome: Content | NeedsCapability, url: str = URL) -> None:
    """Write the enrichment file the run layer writes for ``outcome``.

    The frontmatter is the driver's OWN meta — the point of the round trip
    below is that whatever the driver did or did not stamp is what the
    drain reads back.
    """
    fields = "".join(
        f"{key}: {value}\n" for key, value in outcome.meta.items() if value not in (None, "")
    )
    path.write_text(
        f"---\nurl: {url}\nfetched: '2026-08-10'\n{fields}---\n\n{outcome.body}\n",
        encoding="utf-8",
    )


class FakeDownload:
    """A yt-dlp seam that writes fake audio — the drain's acquisition point."""

    def __call__(self, _url: str, cache_dir, stem: str) -> YoutubeAudio:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{stem}.m4a"
        path.write_bytes(b"fake-audio")
        return YoutubeAudio(
            path=path,
            title="Ledgers as Work Queues: a Field Report",
            channel="Systems Field Notes",
            description="the re-probe's description",
            duration_min=13,
            upload_date="20260810",
        )


class TestCaptionsRoundTripThroughTheDrain:
    """A captioned body re-entering the transcribe path (`enrich mark … waiting`)."""

    def _drain_over(self, tmp_path, outcome: object) -> str:
        assert isinstance(outcome, Content | NeedsCapability)
        park = tmp_path / "youtube-abc123.md"
        _park_file(park, outcome)
        entry = LedgerEntry(
            hash=work_hash(URL),
            url=URL,
            item="2026-08-19-example-55ad7b",
            kind=Kind.YOUTUBE,
            status=Status.WAITING,
            needs=Need.TRANSCRIBE,
            engine="0.2.0",
            date=datetime.date(2026, 8, 10),
        )
        acquired = acquire_youtube_audio(entry, park, tmp_path / "cache", FakeDownload())
        assert isinstance(acquired, Acquired)
        # Exactly what run.py composes from the acquisition.
        return youtube_body(acquired.prefix, "The whisper transcript.")

    def test_a_whisper_drain_never_stacks_a_second_transcript(self, tmp_path):
        # Reachable by `enrich mark <url> waiting --needs transcribe`, or a
        # rerun where the captions vanished. Unstamped, the whole captions
        # body came back as "description" and the drain wrote old caption
        # text and new whisper text into one file under two headings.
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(VTT)})
        body = self._drain_over(tmp_path, driver.fetch(make_unit(URL, Kind.YOUTUBE)))
        assert body.count("## Transcript") == 1
        assert body.count("## Description") == 1
        assert "The whisper transcript." in body
        assert "not receipts, they are the queue" not in body  # superseded, not stacked

    def test_the_description_the_captions_route_wrote_survives(self, tmp_path):
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(VTT)})
        body = self._drain_over(tmp_path, driver.fetch(make_unit(URL, Kind.YOUTUBE)))
        assert "why blocked is not dead" in body  # the park's own, not the re-probe's
        assert "the re-probe's description" not in body

    def test_a_description_less_captions_body_holds_no_stale_transcript(self, tmp_path):
        # Nothing but `## Transcript` on disk — the newline-anchored split
        # would miss a body that STARTS with the heading and hand the
        # captions back as notes. The drain's own no-notes fallback (the
        # re-probe's description) then stands alone above one transcript.
        info = {k: v for k, v in INFO_WITH.items() if k != "description"}
        driver = driver_for(info, {TRACK_URL: vtt_response(VTT)})
        result = driver.fetch(make_unit(URL, Kind.YOUTUBE))
        assert body_of(result).startswith("## Transcript\n\n")
        body = self._drain_over(tmp_path, result)
        assert body.count("## Transcript") == 1
        assert "not receipts, they are the queue" not in body
        assert body.endswith("## Transcript\n\nThe whisper transcript.")

    def test_a_park_is_still_read_as_notes_end_to_end(self, tmp_path):
        # The other half of the contract: a park stamps nothing, so its
        # body — `## Transcript` lines in the author's description and all —
        # is never truncated at a heading it does not own.
        info = dict(INFO_WITHOUT)
        info["description"] = "Chapters below.\n\n## Transcript\n\nsee the pinned comment"
        body = self._drain_over(tmp_path, driver_for(info).fetch(make_unit(URL, Kind.YOUTUBE)))
        assert "see the pinned comment" in body
        assert body.endswith("## Transcript\n\nThe whisper transcript.")


class TestWaitingParks:
    def test_no_captions_parks_waiting_transcribe_and_never_downloads_audio(self):
        # The empty FakeTransport is the pin: ANY download attempt raises —
        # audio acquisition belongs to the drain, not the driver.
        driver = driver_for(INFO_WITHOUT)
        result = needs_of(driver.fetch(make_unit(URL, Kind.YOUTUBE)))
        assert result.need is Need.TRANSCRIBE
        assert result.reason == "no captions available"
        assert result.meta["title"] == "Unindexed Conference Talk"

    def test_the_park_carries_the_description_it_already_fetched(self):
        # A video that goes private during a transcription backlog must not
        # take its description with it: the run layer writes this now.
        driver = driver_for(INFO_WITHOUT)
        body = body_of(driver.fetch(make_unit(URL, Kind.YOUTUBE)))
        assert body.startswith("## Description")
        assert "Recorded on a phone" in body
        assert "## Transcript" not in body  # the drain appends that later

    def test_a_description_less_video_still_parks_cleanly(self):
        info = {k: v for k, v in INFO_WITHOUT.items() if k != "description"}
        result = needs_of(driver_for(info).fetch(make_unit(URL, Kind.YOUTUBE)))
        assert result.need is Need.TRANSCRIBE
        assert result.body is None  # nothing to write, nothing invented

    def test_thin_captions_track_parks_waiting_transcribe(self):
        thin = "WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\nhi\n"
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(thin)})
        result = needs_of(driver.fetch(make_unit(URL, Kind.YOUTUBE)))
        assert result.need is Need.TRANSCRIBE
        assert "## Description" in body_of(result)  # the description still lands


class TestProbeClassification:
    def test_http_code_in_the_message_routes_through_the_classifier(self):
        failure = ProbeError("ERROR: unable to download: HTTP Error 429: Too Many Requests")
        result = driver_for(failure).fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Refused)
        assert not result.permanent

    def test_private_video_is_manual_login_walled(self):
        failure = ProbeError("ERROR: Private video. Sign in if you've been granted access")
        result = driver_for(failure).fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Refused)
        assert result.permanent  # a sign-in wall never heals on retry
        assert PAYWALL_REASON in result.evidence

    def test_confirmed_unavailable_is_dead(self):
        failure = ProbeError("ERROR: Video unavailable. This video has been removed")
        result = driver_for(failure).fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Missing)

    def test_closed_account_is_dead(self):
        failure = ProbeError(
            "ERROR: This video is no longer available because the YouTube "
            "account associated with this video has been closed"
        )
        result = driver_for(failure).fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Missing)

    def test_transient_network_failure_is_blocked_never_dead(self):
        # THE regression pin for the probe path: the motivating-incident
        # class was "cannot distinguish transient trouble from gone".
        failure = ProbeError(
            "ERROR: Unable to download webpage: <urlopen error [Errno 60] "
            "Operation timed out> (caused by URLError(TimeoutError(60, "
            "'Operation timed out')))"
        )
        result = driver_for(failure).fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Refused)
        assert not isinstance(result, Missing)

    def test_extractor_breakage_is_blocked_never_dead(self):
        # yt-dlp breaking against a YouTube change heals via a yt-dlp
        # release — the video is not gone.
        failure = ProbeError(
            "ERROR: [youtube] dQw4w9WgXcA: Unable to extract player response; "
            "please report this issue on https://github.com/yt-dlp/yt-dlp/issues"
        )
        result = driver_for(failure).fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Refused)
        assert "github.com" not in result.evidence  # the message URL was scrubbed

    @pytest.mark.parametrize("message", ["", "   \t "])
    def test_a_messageless_probe_failure_still_states_blocked_shaped_evidence(self, message):
        # An empty (or whitespace-only) yt-dlp message scrubs to "": without
        # the classifier's stated fallback the Refused it becomes would fail
        # evidence validation inside the driver, and the broad catch would
        # ledger an engine error — a filed issue and a release-gated retry —
        # where the baseline gave blocked with the attempts lifecycle.
        result = driver_for(ProbeError(message)).fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Refused)
        assert not result.permanent
        assert result.evidence == "probe failed with no message from yt-dlp"

    def test_geo_block_is_manual_with_reason(self):
        failure = ProbeError(
            "ERROR: Video unavailable. The uploader has not made this video "
            "available in your country"
        )
        result = driver_for(failure).fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Refused)
        assert result.permanent
        assert "geo-blocked" in result.evidence


class TestCaptionTrackFailures:
    def test_track_404_is_blocked_never_video_death(self):
        # Track URLs are signed and short-lived; a 404 there says nothing
        # about the video — a re-probe next run mints fresh URLs.
        gone = HttpResponse(status=404, content_type="text/plain", body=b"")
        driver = driver_for(INFO_WITH, {TRACK_URL: gone})
        result = driver.fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Refused)
        assert not result.permanent
        assert "caption track fetch failed: HTTP 404" in result.evidence

    def test_track_connection_failure_is_blocked(self):
        driver = driver_for(INFO_WITH, {TRACK_URL: TimeoutError("timed out")})
        result = driver.fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Refused)
        assert "caption track fetch failed" in result.evidence

    def test_track_failure_keeps_the_wire_fact_never_classifier_framing(self):
        # A 402 on the VIDEO would classify manual/paywalled; on a signed
        # track URL it is CDN weather like any other code. The reason keeps
        # the plain wire fact — "payment/login required" here would send a
        # session rescuing a paywall that does not exist.
        walled = HttpResponse(status=402, content_type="text/plain", body=b"")
        driver = driver_for(INFO_WITH, {TRACK_URL: walled})
        result = driver.fetch(make_unit(URL, Kind.YOUTUBE))
        assert isinstance(result, Refused)
        assert not result.permanent  # a 402 on a signed track URL is CDN weather
        assert "caption track fetch failed: HTTP 402" in result.evidence
        assert PAYWALL_REASON not in result.evidence


class TestCleanVtt:
    def test_headers_cues_numbers_and_align_lines_drop(self):
        cleaned = clean_vtt(VTT)
        for noise in ("WEBVTT", "Kind:", "Language:", "-->", "align:"):
            assert noise not in cleaned
        assert not any(line.isdigit() for line in cleaned.split("\n"))

    def test_rolling_duplicate_lines_collapse(self):
        lines = clean_vtt(VTT).split("\n")
        assert all(a != b for a, b in itertools.pairwise(lines))
