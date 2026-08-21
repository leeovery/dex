"""The youtube driver: yt-dlp metadata + cleaned VTT captions.

A video's identity is its video id: every single-video URL shape —
``watch?v=``, bare and ``/live|/shorts|/embed`` youtu.be short links,
``youtube.com/live|shorts|embed|v/<id>`` — canonicalizes to
``watch?v=<id>``, one fetchable work unit per video. A playlist's identity
is its list id — the playlist page and the ``/embed/videoseries?list=``
embed alike; playlist units park ``manual`` (a playlist is a collection,
and which entries deserve capture is judgment).

The driver NEVER downloads audio — audio acquisition belongs to the
transcribe drain. No usable captions means
``Result(waiting, needs=transcribe)``: the work is parked for the
capability, not given up on.

Probe failures are mapped honestly: an HTTP code buried in yt-dlp's message
routes through the central classifier; private/sign-in walls are ``manual``;
only a confirmed-unavailable video is ``dead``.
"""

import re
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit

from dex_engine.pipeline.classify import (
    PAYWALL_REASON,
    Classification,
    classify_connection,
    classify_http,
    scrub,
)
from dex_engine.pipeline.types import Kind, Need, Result, Status, WorkUnit
from dex_engine.pipeline.urls import base_canonical, host_of

from .transport import Transport, urllib_transport

__all__ = ["ProbeError", "YouTubeDriver", "classify_probe_failure", "clean_vtt", "yt_dlp_probe"]

_HOSTS = frozenset({"youtube.com", "youtu.be"})
# Path prefixes that address a single video, on youtube.com and youtu.be
# alike: /live/<id>, /shorts/<id>, /embed/<id>, the legacy /v/<id>.
_VIDEO_PREFIXES = frozenset({"live", "shorts", "embed", "v"})
# The standard playlist-embed shape: /embed/videoseries?list=<id> — a
# playlist, not a video called "videoseries".
_PLAYLIST_EMBED_SEGMENTS = ["embed", "videoseries"]

# A cleaned captions track shorter than this is no transcript at all.
_MIN_TRANSCRIPT_CHARS = 200

_HTTP_CODE_RE = re.compile(r"HTTP Error (\d{3})")
_LOGIN_MARKERS = ("private", "sign in", "members-only", "members only", "log in")
_GEO_MARKERS = ("in your country", "in your region", "geo restricted", "geo-restricted")
# `dead` needs an explicit confirmed-gone marker — anything else (transient
# network trouble, yt-dlp extractor breakage) is the world misbehaving and
# defaults to `blocked`: the motivating-incident class, again.
_GONE_MARKERS = ("video unavailable", "removed", "terminated")

_VTT_NOISE_PREFIXES = ("WEBVTT", "Kind:", "Language:", "NOTE", "align:")
_VTT_TAG_RE = re.compile(r"<[^>]+>")


class ProbeError(Exception):
    """yt-dlp could not extract video info; the message says why."""


def yt_dlp_probe(url: str) -> dict:
    """Extract video info via yt-dlp, no download.

    Args:
        url: The canonical video URL.

    Returns:
        The yt-dlp info dict.

    Raises:
        ProbeError: yt-dlp reported a download error; the driver maps the
            message through the central classifier.
    """
    import yt_dlp  # noqa: PLC0415 — lazy: heavy dep, loaded only when probing

    options = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ProbeError(str(e)) from e
    return dict(info or {})


class YouTubeDriver:
    """Fetch video metadata and captions; park for transcription otherwise."""

    kind: Kind = Kind.YOUTUBE
    sleep: float = 2.0

    def __init__(
        self,
        *,
        probe: Callable[[str], dict] = yt_dlp_probe,
        transport: Transport = urllib_transport,
    ) -> None:
        """Wire the probe (yt-dlp) and caption-download seams.

        Args:
            probe: url -> yt-dlp info dict, raising :class:`ProbeError`.
            transport: The HTTP seam for the caption track download.
        """
        self._probe = probe
        self._transport = transport

    def matches(self, url: str) -> bool:
        """True for youtube.com / youtu.be (m. and www. prefixes normalized)."""
        return host_of(url) in _HOSTS

    def canonical(self, url: str) -> str:
        """Video shapes collapse to ``watch?v=<id>``; playlist pages keep their list id."""
        video_id = _video_id(url)
        if video_id is not None:
            return "https://youtube.com/watch?" + urlencode({"v": video_id})
        playlist_id = _playlist_id(url)
        if playlist_id is not None:
            return "https://youtube.com/playlist?" + urlencode({"list": playlist_id})
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Result:
        """Probe the video; captions become the transcript, or the unit parks."""
        if _playlist_id(unit.url) is not None:
            return Result(
                status=Status.MANUAL,
                meta={},
                reason="playlist link — capture the videos worth keeping individually",
            )
        try:
            info = self._probe(unit.url)
        except ProbeError as e:
            failure = classify_probe_failure(str(e))
            return Result(status=failure.status, meta={}, reason=failure.reason)
        meta = _video_meta(info)
        track_url = _caption_track_url(info)
        if track_url is None:
            return Result(
                status=Status.WAITING,
                meta=meta,
                needs=Need.TRANSCRIBE,
                reason="no captions available",
            )
        return self._fetch_transcript(track_url, info, meta)

    def _fetch_transcript(
        self, track_url: str, info: dict, meta: dict[str, str | int | None]
    ) -> Result:
        # A failing caption track says nothing about the VIDEO: yt-dlp track
        # URLs are signed and short-lived, so even a 404 here is transient —
        # blocked (a re-probe next run mints fresh URLs), never dead.
        try:
            response = self._transport(track_url)
        except OSError as e:
            detail = classify_connection(e).reason
            return Result(
                status=Status.BLOCKED, meta={}, reason=f"caption track fetch failed: {detail}"
            )
        if not response.ok:
            return Result(
                status=Status.BLOCKED,
                meta={},
                reason=f"caption track fetch failed: HTTP {response.status}",
            )
        transcript = clean_vtt(response.text())
        if len(transcript) < _MIN_TRANSCRIPT_CHARS:
            return Result(
                status=Status.WAITING,
                meta=meta,
                needs=Need.TRANSCRIBE,
                reason="captions track too thin to be a transcript",
            )
        return Result(status=Status.DONE, meta=meta, body=_body(info, transcript))


def _video_id(url: str) -> str | None:
    """The video id a URL addresses, or None for non-video shapes."""
    host = host_of(url)
    parts = urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments == _PLAYLIST_EMBED_SEGMENTS:
        # A playlist embed, never a video — "videoseries" must not leak
        # out as a (bogus, shared) video id.
        return None
    if host == "youtube.com" and segments == ["watch"]:
        return dict(parse_qsl(parts.query)).get("v")
    if host == "youtu.be" and len(segments) == 1:
        return segments[0]
    if host in _HOSTS and len(segments) == 2 and segments[0] in _VIDEO_PREFIXES:  # noqa: PLR2004 — /live/<id>-shaped pair
        return segments[1]
    return None


def _playlist_id(url: str) -> str | None:
    """The list id of a playlist-page URL (or playlist embed), or None."""
    parts = urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    is_playlist_page = host_of(url) == "youtube.com" and segments in (
        ["playlist"],
        _PLAYLIST_EMBED_SEGMENTS,
    )
    if not is_playlist_page:
        return None
    return dict(parse_qsl(parts.query)).get("list")


def classify_probe_failure(message: str) -> Classification:
    """Map a yt-dlp failure message: code > login > geo > confirmed gone > blocked.

    Public because the transcribe drain's audio acquisition fails in
    exactly the same vocabulary — one classifier, not two.

    ``dead`` requires an explicit confirmed-gone marker. The default is
    ``blocked`` — a transient network error or an extractor-breakage message
    mislabeled ``dead`` would be terminal and never retried, which is
    exactly the incident class this design exists to kill.
    """
    code_match = _HTTP_CODE_RE.search(message)
    if code_match:
        return classify_http(int(code_match.group(1)))
    lowered = message.lower()
    if any(marker in lowered for marker in _LOGIN_MARKERS):
        return Classification(status=Status.MANUAL, reason=f"{PAYWALL_REASON} ({scrub(message)})")
    if any(marker in lowered for marker in _GEO_MARKERS):
        # Checked before the gone markers: geo messages often open with
        # "Video unavailable." and the specific diagnosis must win.
        return Classification(status=Status.MANUAL, reason=f"geo-blocked ({scrub(message)})")
    if any(marker in lowered for marker in _GONE_MARKERS) or (
        "account" in lowered and "closed" in lowered
    ):
        return Classification(status=Status.DEAD, reason=scrub(message))
    return Classification(status=Status.BLOCKED, reason=scrub(message))


def _video_meta(info: dict) -> dict[str, str | int | None]:
    duration = info.get("duration") or 0
    return {
        "title": info.get("title") or None,
        "channel": info.get("channel") or info.get("uploader"),
        # None (omitted), never 0, for an unknown duration — the same
        # semantics as the transcribe drain's route: one frontmatter shape
        # per kind regardless of how the transcript was obtained.
        "duration_min": round(duration / 60) if duration else None,
        "upload_date": info.get("upload_date"),
    }


def _caption_track_url(info: dict) -> str | None:
    """The first English VTT track: real subtitles first, then auto captions."""
    subtitles = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    for source in (subtitles, automatic):
        for lang in sorted(source):
            if not lang.startswith("en"):
                continue
            for track in source[lang]:
                if track.get("ext") == "vtt" and track.get("url"):
                    return track["url"]
    return None


def _body(info: dict, transcript: str) -> str:
    """Description + transcript sections, as today; transcript alone otherwise."""
    description = (info.get("description") or "").strip()
    if description:
        return f"## Description\n\n{description}\n\n## Transcript\n\n{transcript}"
    return transcript


def clean_vtt(vtt: str) -> str:
    """Strip VTT cues, tags, headers, and consecutive duplicates to plain text."""
    lines: list[str] = []
    previous = None
    for raw in vtt.splitlines():
        line = _VTT_TAG_RE.sub("", raw).strip()
        if (
            not line
            or "-->" in line
            or line.startswith(_VTT_NOISE_PREFIXES)
            or re.fullmatch(r"\d+", line)
        ):
            continue
        if line != previous:
            lines.append(line)
            previous = line
    return "\n".join(lines)
