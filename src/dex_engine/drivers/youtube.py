"""The youtube driver: yt-dlp metadata + cleaned VTT captions (§3/§6).

The driver NEVER downloads audio — audio acquisition belongs to the
transcribe drain (§6). No usable captions means
``Result(waiting, needs=transcribe)``: the work is parked for the
capability, not given up on.

Probe failures are mapped honestly: an HTTP code buried in yt-dlp's message
routes through the central classifier; private/sign-in walls are ``manual``;
only a confirmed-unavailable video is ``dead``.
"""

import re
from collections.abc import Callable
from urllib.parse import urlsplit

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
_KEEP_PARAMS = frozenset({"v"})

# A cleaned captions track shorter than this is no transcript at all.
_MIN_TRANSCRIPT_CHARS = 200

_HTTP_CODE_RE = re.compile(r"HTTP Error (\d{3})")
_LOGIN_MARKERS = ("private", "sign in", "members-only", "members only", "log in")
_GEO_MARKERS = ("in your country", "in your region", "geo restricted", "geo-restricted")
# `dead` needs an explicit confirmed-gone marker — anything else (transient
# network trouble, yt-dlp extractor breakage) is the world misbehaving and
# defaults to `blocked`: the motivating-incident class, again (§5).
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
    import yt_dlp  # noqa: PLC0415 — lazy: heavy dep, loaded only when probing (§14)

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
        """youtu.be short links become watch URLs; only the ``v`` param survives."""
        parts = urlsplit(url)
        if host_of(url) == "youtu.be":
            video_id = parts.path.strip("/")
            url = f"https://youtube.com/watch?v={video_id}"
        return base_canonical(url, keep_params=_KEEP_PARAMS)

    def fetch(self, unit: WorkUnit) -> Result:
        """Probe the video; captions become the transcript, or the unit parks."""
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


def classify_probe_failure(message: str) -> Classification:
    """Map a yt-dlp failure message: code > login > geo > confirmed gone > blocked.

    Public because the transcribe drain's audio acquisition (§6) fails in
    exactly the same vocabulary — one classifier, not two.

    ``dead`` requires an explicit confirmed-gone marker. The default is
    ``blocked`` — a transient network error or an extractor-breakage message
    mislabeled ``dead`` would be terminal and never retried, which is
    exactly the incident class this design exists to kill (§5).
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
        "duration_min": round(duration / 60),
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
