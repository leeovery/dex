"""The youtube driver: yt-dlp metadata + cleaned VTT captions.

A video's identity is its video id: every single-video URL shape —
``watch?v=``, bare and ``/live|/shorts|/embed`` youtu.be short links,
``youtube.com/live|shorts|embed|v/<id>`` — canonicalizes to
``watch?v=<id>``, one fetchable work unit per video. A playlist's identity
is its list id — the playlist page and the ``/embed/videoseries?list=``
embed alike; playlist units park ``manual`` (a playlist is a collection,
and which entries deserve capture is judgment).

Channel addresses (``/@handle`` and its tabs, percent-encoded ``/%40handle``
included, ``/user/…``, ``/c/…``, ``/channel/…``, and the legacy bare vanity
name ``/veritasium``), hashtag feeds and search-result pages are collections
too, and park the same way — BEFORE the probe runs, because yt-dlp answers
those URLs by enumerating every video the channel holds. A bare first
segment is a channel unless it is one of youtube.com's own functional
paths, which are named explicitly so ``/watch``, ``/playlist``,
``/results``, ``/feed`` and their kin keep working.

Captions ARE a transcript, and the body that carries one says so in its
frontmatter: the captions route stamps ``via: captions``, the same slot the
transcribe drain stamps its provider into. The heading was never the
answer — the drain reads ``via`` to find where the notes end, so an
unstamped captions body read back as description end to end and a later
whisper drain stacked a second ``## Transcript`` under the first.

The driver NEVER downloads audio — audio acquisition belongs to the
transcribe drain. No usable captions means
``NeedsCapability(need=transcribe)``: the work is parked for the
capability, not given up on — and the park carries the description it
already fetched, so the run layer writes that content now rather than
holding it hostage to a transcription backlog the video may not survive.
The drain appends the transcript to that same file.

Probe failures are mapped honestly: an HTTP code buried in yt-dlp's message
routes through the central classifier; private/sign-in walls are permanent
refusals; only a confirmed-unavailable video is ``Missing``.
"""

import re
from collections.abc import Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from dex_engine.pipeline.enrichment import description_section, youtube_body
from dex_engine.pipeline.types import (
    Content,
    Kind,
    Need,
    NeedsCapability,
    Outcome,
    Refused,
    Unusable,
    WorkUnit,
)
from dex_engine.pipeline.urls import base_canonical, host_of

from .fetch import FetchFailure, fetch_classified
from .transport import Transport, urllib_transport
from .ytdlp import ProbeError, classify_probe_failure, yt_dlp_probe

__all__ = ["YouTubeDriver", "clean_vtt"]

_HOSTS = frozenset({"youtube.com", "youtu.be"})
# Path prefixes that address a single video, on youtube.com and youtu.be
# alike: /live/<id>, /shorts/<id>, /embed/<id>, the legacy /v/<id>.
_VIDEO_PREFIXES = frozenset({"live", "shorts", "embed", "v"})
# The standard playlist-embed shape: /embed/videoseries?list=<id> — a
# playlist, not a video called "videoseries".
_PLAYLIST_EMBED_SEGMENTS = ["embed", "videoseries"]

# Channel address shapes: the @handle form (percent-encoded as %40 by
# plenty of link shorteners and clipboards) and the legacy /user, /c and
# /channel prefixes, each with or without a tab segment (/videos, /shorts,
# /streams, /playlists, …). The one channel-scoped path that addresses a
# single video is /live — the channel's current livestream.
_CHANNEL_PREFIXES = frozenset({"user", "c", "channel"})
_CHANNEL_LIVE_TAB = "live"
_SEARCH_SEGMENT = "results"
_HASHTAG_SEGMENT = "hashtag"
# The oldest channel address is a bare vanity name at the root —
# youtube.com/veritasium, and its tabs — which no URL shape distinguishes
# from a functional path. So youtube.com's own first segments are named
# here and everything else at the root is read as a channel. The list
# errs in one direction on purpose: a functional segment missing from it
# parks `manual` (recoverable, one ledger line), while a channel treated
# as functional reaches the probe and enumerates thousands of videos.
_FUNCTIONAL_SEGMENTS = frozenset(
    {
        # Video, playlist and search surfaces the driver must keep serving.
        "watch",
        "watch_videos",
        "playlist",
        "results",
        "embed",
        "live",
        "shorts",
        "v",
        "feed",
        "channel",
        "user",
        "c",
        "hashtag",
        # Product, account and marketing surfaces — never a channel.
        "about",
        "account",
        "ads",
        "attribution_link",
        "creators",
        "error",
        "gaming",
        "howyoutubeworks",
        "logout",
        "movies",
        "music",
        "new",
        "oops",
        "playables",
        "post",
        "premium",
        "redirect",
        "reporthistory",
        "signin",
        "source",
        "supported_browsers",
        "t",
        "upload",
        "verify_age",
    }
)
# A vanity channel address is the name plus at most one tab segment.
_MAX_CHANNEL_SEGMENTS = 2

# A cleaned captions track shorter than this is no transcript at all.
_MIN_TRANSCRIPT_CHARS = 200

# The transcript-provenance stamp the CAPTIONS route writes into meta, and
# thereby into the enrichment frontmatter. **The frontmatter, not the
# heading, says whether a body holds a transcript** (design §6): the drain
# reads `via` back to find where the notes end, so a captions transcript
# that carried no `via` was read as description end to end and a later
# whisper drain appended a SECOND transcript under the first — old caption
# text and new whisper text in one file. A park still stamps nothing: its
# body IS notes end to end.
_CAPTIONS_VIA = "captions"

_VTT_NOISE_PREFIXES = ("WEBVTT", "Kind:", "Language:", "NOTE", "align:")
_VTT_TAG_RE = re.compile(r"<[^>]+>")


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

    def fetch(self, unit: WorkUnit) -> Outcome:
        """Probe the video; captions become the transcript, or the unit parks."""
        if _playlist_id(unit.url) is not None:
            # A collection, not a unit — but its members are there for
            # judgment to pick, so it parks rather than closing.
            return Unusable(
                evidence="playlist link — capture the videos worth keeping individually"
            )
        collection = _collection_reason(unit.url)
        if collection is not None:
            # Guarded BEFORE the probe: yt-dlp enumerates a whole channel
            # from these URLs (minutes of work, every video's metadata),
            # and the transcribe drain would then pull every one of those
            # audio files over a single filename.
            return Unusable(evidence=collection)
        try:
            info = self._probe(unit.url)
        except ProbeError as e:
            return classify_probe_failure(str(e)).to_outcome()
        meta = _video_meta(info)
        track_url = _caption_track_url(info)
        if track_url is None:
            return NeedsCapability(
                need=Need.TRANSCRIBE,
                meta=meta,
                body=description_section(_description(info)) or None,
                reason="no captions available",
            )
        return self._fetch_transcript(track_url, info, meta)

    def _fetch_transcript(
        self, track_url: str, info: dict, meta: dict[str, str | int | None]
    ) -> Outcome:
        # A failing caption track says nothing about the VIDEO: yt-dlp track
        # URLs are signed and short-lived, so even a 404 here is a transient
        # refusal (a re-probe next run mints fresh URLs), never Missing. The
        # wire fact rides the evidence; the classified framing never does.
        outcome = fetch_classified(self._transport, track_url)
        if isinstance(outcome, FetchFailure):
            return Refused(evidence=f"caption track fetch failed: {outcome.detail}")
        transcript = clean_vtt(outcome.text())
        if len(transcript) < _MIN_TRANSCRIPT_CHARS:
            return NeedsCapability(
                need=Need.TRANSCRIBE,
                meta=meta,
                body=description_section(_description(info)) or None,
                reason="captions track too thin to be a transcript",
            )
        # Stamped only on the route that actually produces a transcript —
        # the two parks above share this meta and must stay unstamped.
        return Content(
            meta={**meta, "via": _CAPTIONS_VIA},
            body=youtube_body(_description(info), transcript),
        )


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


def _collection_reason(url: str) -> str | None:
    """Why a channel/tab/hashtag/search URL parks, or None when it is a video."""
    if host_of(url) != "youtube.com":
        return None
    # Unquoted: %40 is a routine spelling of the @ that opens a handle.
    segments = [unquote(segment) for segment in urlsplit(url).path.split("/") if segment]
    if not segments:
        return None
    first = segments[0].lower()
    if first == _SEARCH_SEGMENT:
        return (
            "search-results link — a result page is a collection: capture the videos "
            "worth keeping individually"
        )
    if first == _HASHTAG_SEGMENT:
        return (
            "hashtag link — a hashtag feed is a collection: capture the videos worth "
            "keeping individually"
        )
    tail = _channel_tab(segments, first)
    if tail is None or tail == [_CHANNEL_LIVE_TAB]:
        return None
    return "channel link — a channel is a collection: capture the videos worth keeping individually"


def _channel_tab(segments: list[str], first: str) -> list[str] | None:
    """The tab segments after a channel address, or None for a non-channel path."""
    if first.startswith("@"):
        return segments[1:]
    if first in _CHANNEL_PREFIXES:
        return segments[2:] if len(segments) >= 2 else None  # noqa: PLR2004 — /channel/<id> pair
    if first in _FUNCTIONAL_SEGMENTS or len(segments) > _MAX_CHANNEL_SEGMENTS:
        return None
    return segments[1:]  # the legacy vanity form: /veritasium, /veritasium/videos


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


def _description(info: dict) -> str:
    return (info.get("description") or "").strip()


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
