"""Tests for drivers/youtube.py: captions, waiting parks, probe classification."""

import itertools
import json

import pytest

from dex_engine.drivers.transport import HttpResponse
from dex_engine.drivers.youtube import ProbeError, YouTubeDriver, clean_vtt
from dex_engine.pipeline.classify import PAYWALL_REASON
from dex_engine.pipeline.types import Kind, Need, Status
from tests.drivers.conftest import FakeTransport, body_of, fixture_text, make_unit, reason_of

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


class TestCaptions:
    def test_captions_become_description_and_transcript_sections(self):
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(VTT)})
        result = driver.fetch(make_unit(URL, Kind.YOUTUBE))
        assert result.status is Status.DONE
        body = body_of(result)
        assert body.startswith("## Description")
        assert "## Transcript" in body
        assert "not receipts, they are the queue" in body

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
        meta = driver.fetch(make_unit(URL, Kind.YOUTUBE)).meta
        assert meta["title"] == "Ledgers as Work Queues: a Field Report"
        assert meta["channel"] == "Systems Field Notes"
        assert meta["duration_min"] == 13
        assert meta["upload_date"] == "20260810"


class TestWaitingParks:
    def test_no_captions_parks_waiting_transcribe_and_never_downloads_audio(self):
        # The empty FakeTransport is the pin: ANY download attempt raises —
        # audio acquisition belongs to the drain, not the driver (§6).
        driver = driver_for(INFO_WITHOUT)
        result = driver.fetch(make_unit(URL, Kind.YOUTUBE))
        assert result.status is Status.WAITING
        assert result.needs is Need.TRANSCRIBE
        assert reason_of(result) == "no captions available"
        assert result.meta["title"] == "Unindexed Conference Talk"

    def test_thin_captions_track_parks_waiting_transcribe(self):
        thin = "WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\nhi\n"
        driver = driver_for(INFO_WITH, {TRACK_URL: vtt_response(thin)})
        result = driver.fetch(make_unit(URL, Kind.YOUTUBE))
        assert result.status is Status.WAITING
        assert result.needs is Need.TRANSCRIBE


class TestProbeClassification:
    def test_http_code_in_the_message_routes_through_the_classifier(self):
        failure = ProbeError("ERROR: unable to download: HTTP Error 429: Too Many Requests")
        result = driver_for(failure).fetch(make_unit(URL, Kind.YOUTUBE))
        assert result.status is Status.BLOCKED

    def test_private_video_is_manual_login_walled(self):
        failure = ProbeError("ERROR: Private video. Sign in if you've been granted access")
        result = driver_for(failure).fetch(make_unit(URL, Kind.YOUTUBE))
        assert result.status is Status.MANUAL
        assert PAYWALL_REASON in reason_of(result)

    def test_confirmed_unavailable_is_dead(self):
        failure = ProbeError("ERROR: Video unavailable. This video has been removed")
        result = driver_for(failure).fetch(make_unit(URL, Kind.YOUTUBE))
        assert result.status is Status.DEAD


class TestCaptionTrackFailures:
    def test_track_404_is_blocked_never_video_death(self):
        # Track URLs are signed and short-lived; a 404 there says nothing
        # about the video — a re-probe next run mints fresh URLs.
        gone = HttpResponse(status=404, content_type="text/plain", body=b"")
        driver = driver_for(INFO_WITH, {TRACK_URL: gone})
        result = driver.fetch(make_unit(URL, Kind.YOUTUBE))
        assert result.status is Status.BLOCKED
        assert "caption track fetch failed: HTTP 404" in reason_of(result)

    def test_track_connection_failure_is_blocked(self):
        driver = driver_for(INFO_WITH, {TRACK_URL: TimeoutError("timed out")})
        result = driver.fetch(make_unit(URL, Kind.YOUTUBE))
        assert result.status is Status.BLOCKED
        assert "caption track fetch failed" in reason_of(result)


class TestCleanVtt:
    def test_headers_cues_numbers_and_align_lines_drop(self):
        cleaned = clean_vtt(VTT)
        for noise in ("WEBVTT", "Kind:", "Language:", "-->", "align:"):
            assert noise not in cleaned
        assert not any(line.isdigit() for line in cleaned.split("\n"))

    def test_rolling_duplicate_lines_collapse(self):
        lines = clean_vtt(VTT).split("\n")
        assert all(a != b for a, b in itertools.pairwise(lines))
