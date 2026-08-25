"""The yt-dlp seam: probe, audio download, and the failure vocabulary.

Two consumers reach yt-dlp and neither owns it: the youtube driver probes
for metadata and captions, and the transcribe drain downloads the audio a
park recorded (audio acquisition belongs to the drain, not the drivers).
Both fail in exactly the same vocabulary — one classifier, not two — and
before this module existed the drain had to import the youtube driver to
share it, the one standing pipeline-imports-a-driver violation.

It sits beside :mod:`dex_engine.drivers.transport`,
:mod:`dex_engine.drivers.gh` and :mod:`dex_engine.drivers.audio`, for those
modules' reason: a seam several layers share is not itself a driver.
Like ``gh`` it reaches only for pipeline vocabulary — nothing here knows
about work units or results.

The audio-cache scan lives here too: the yt-dlp download reuses a
completed file under its stem and must clean up its own partials, and the
enclosure route shares the same cache directory under the same contract,
so both read the one scan.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dex_engine.pipeline.classify import PAYWALL_REASON, Classification, classify_http, scrub
from dex_engine.pipeline.types import Status

__all__ = [
    "DownloadAudio",
    "ProbeError",
    "YoutubeAudio",
    "cached_audio",
    "classify_probe_failure",
    "yt_dlp_audio",
    "yt_dlp_probe",
]

_HTTP_CODE_RE = re.compile(r"HTTP Error (\d{3})")
_LOGIN_MARKERS = ("private", "sign in", "members-only", "members only", "log in")
_GEO_MARKERS = ("in your country", "in your region", "geo restricted", "geo-restricted")
# `dead` needs an explicit confirmed-gone marker — anything else (transient
# network trouble, yt-dlp extractor breakage) is the world misbehaving and
# defaults to `blocked`: the motivating-incident class, again.
_GONE_MARKERS = ("video unavailable", "removed", "terminated")


class ProbeError(Exception):
    """yt-dlp could not extract video info; the message says why."""


def yt_dlp_probe(url: str) -> dict:
    """Extract video info via yt-dlp, no download.

    Args:
        url: The canonical video URL.

    Returns:
        The yt-dlp info dict.

    Raises:
        ProbeError: yt-dlp reported a download error; the caller maps the
            message through :func:`classify_probe_failure`.
    """
    import yt_dlp  # noqa: PLC0415 — lazy: heavy dep, loaded only when probing

    options = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise ProbeError(str(e)) from e
    return dict(info or {})


def classify_probe_failure(message: str) -> Classification:
    """Map a yt-dlp failure message: code > login > geo > confirmed gone > blocked.

    The youtube driver's probe and the transcribe drain's audio
    acquisition fail in exactly the same vocabulary — one classifier, not
    two.

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


@dataclass(frozen=True, slots=True, kw_only=True)
class YoutubeAudio:
    """What the yt-dlp seam returns: the audio file plus priming vocabulary."""

    path: Path
    title: str | None
    channel: str | None
    description: str
    duration_min: int | None = None
    upload_date: str | None = None


# url, cache_dir, stem -> downloaded audio; raises ProbeError on yt-dlp
# failure (classified by classify_probe_failure — one classifier).
DownloadAudio = Callable[[str, Path, str], YoutubeAudio]


def yt_dlp_audio(url: str, cache_dir: Path, stem: str) -> YoutubeAudio:
    """The one real :data:`DownloadAudio`: bestaudio into the cache, no ffmpeg.

    An audio file already cached under ``stem`` is reused (retries don't
    re-download); the probe still runs for the priming vocabulary.

    Args:
        url: The canonical video URL.
        cache_dir: ``cache/audio/``.
        stem: The work-unit hash — the cache filename stem.

    Returns:
        The audio and its vocabulary.

    Raises:
        ProbeError: yt-dlp failed; the caller classifies the message.
    """
    import yt_dlp  # noqa: PLC0415 — lazy: heavy dep, loaded only when acquiring

    cache_dir.mkdir(parents=True, exist_ok=True)
    existing = cached_audio(cache_dir, stem)
    options = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": str(cache_dir / f"{stem}.%(ext)s"),
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = dict(ydl.extract_info(url, download=existing is None) or {})
            path = existing or Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        raise ProbeError(str(e)) from e
    if not path.exists():
        fallback = cached_audio(cache_dir, stem)
        if fallback is None:
            raise ProbeError("yt-dlp reported success but produced no audio file")
        path = fallback
    duration = info.get("duration") or 0
    return YoutubeAudio(
        path=path,
        title=info.get("title") or None,
        channel=info.get("channel") or info.get("uploader"),
        description=(info.get("description") or "").strip(),
        duration_min=round(duration / 60) if duration else None,
        upload_date=info.get("upload_date") or None,
    )


# In-flight artifacts: yt-dlp's `.part` bodies (also `.part-Frag…`) and
# `.ytdl` state files, the enclosure download's atomic-write `.tmp` temps,
# and the odd `.download`/`.temp` from other tooling.
_PARTIAL_MARKERS = (".part", ".ytdl", ".download", ".temp", ".tmp")


def _is_partial(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(_PARTIAL_MARKERS) or ".part-" in lowered


def cached_audio(cache_dir: Path, stem: str) -> Path | None:
    """The completed cached download for ``stem`` — partials never count.

    A crash mid-download leaves ``.part``/``.ytdl`` files behind; reusing
    one would transcribe truncated audio and ledger it ``done``. Partials
    are deleted here so the retry re-downloads from scratch (the
    don't-re-download rule covers completed audio only).
    """
    complete: Path | None = None
    for path in sorted(cache_dir.glob(f"{stem}.*")):
        if _is_partial(path.name):
            path.unlink(missing_ok=True)
        elif complete is None:
            complete = path
    return complete
