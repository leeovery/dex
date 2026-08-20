"""Transcribe-drain mechanics (§6): audio acquisition, priming, bodies.

**Audio acquisition belongs to the drain, not the drivers**: drivers only
ever emit ``needs: transcribe`` with the pointer — a YouTube URL, or an
enclosure URL recorded in the enrichment file's frontmatter (§9). The drain
obtains the audio itself, into ``cache/audio/<hash>.<ext>``; the run layer
(``pipeline/run.py``) owns the control flow and every ledger write, and
calls the pure-ish helpers here.

Audio lifecycle (§9): stays in cache while pending/failed (retries don't
re-download 150MB), deleted on successful transcription — the audio is a
digestion mechanism, not what was shared; the transcript supersedes it; the
recorded enclosure URL is the re-fetch pointer.
"""

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dex_engine.drivers.transport import Transport
from dex_engine.drivers.youtube import ProbeError, classify_probe_failure

from .classify import Classification, classify_connection, classify_http
from .types import LedgerEntry, Status

__all__ = [
    "TRANSCRIBE_RUN_CAP",
    "Acquired",
    "DownloadAudio",
    "YoutubeAudio",
    "acquire_podcast_audio",
    "acquire_youtube_audio",
    "podcast_body",
    "read_enrichment",
    "youtube_body",
    "yt_dlp_audio",
]

# Per-run transcription cap (§12): a first sync's resurrected backlog must
# never monopolize a machine. `enrich transcribe --limit` overrides.
TRANSCRIBE_RUN_CAP = 10

# Whisper's prompt window is ~224 tokens; priming beyond it is discarded
# anyway, so the vocabulary text is bounded in characters.
_PROMPT_MAX_CHARS = 800

_AUDIO_EXT_DEFAULT = "mp3"
_TRANSCRIPT_HEADING = "## Transcript"


@dataclass(frozen=True, slots=True, kw_only=True)
class YoutubeAudio:
    """What the yt-dlp seam returns: the audio file plus priming vocabulary."""

    path: Path
    title: str | None
    channel: str | None
    description: str
    duration_min: int | None = None


# url, cache_dir, stem -> downloaded audio; raises ProbeError on yt-dlp
# failure (classified by classify_probe_failure — one classifier, §5).
DownloadAudio = Callable[[str, Path, str], YoutubeAudio]


def yt_dlp_audio(url: str, cache_dir: Path, stem: str) -> YoutubeAudio:
    """The one real :data:`DownloadAudio`: bestaudio into the cache, no ffmpeg.

    An audio file already cached under ``stem`` is reused (§9: retries don't
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
    import yt_dlp  # noqa: PLC0415 — lazy: heavy dep, loaded only when acquiring (§14)

    cache_dir.mkdir(parents=True, exist_ok=True)
    existing = next(iter(sorted(cache_dir.glob(f"{stem}.*"))), None)
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
        fallback = next(iter(sorted(cache_dir.glob(f"{stem}.*"))), None)
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
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Acquired:
    """Audio in hand, plus everything the transcript's enrichment needs."""

    audio: Path
    meta: dict[str, str | int | None]
    prompt: str  # initial_prompt priming — the item's known vocabulary (§6)
    prefix: str  # body text the transcript follows (description / show notes)


def acquire_youtube_audio(
    entry: LedgerEntry, cache_dir: Path, download: DownloadAudio
) -> Acquired | Classification:
    """Acquire a YouTube unit's audio via the yt-dlp seam (§6).

    Args:
        entry: The waiting ledger entry.
        cache_dir: ``cache/audio/``.
        download: The yt-dlp seam.

    Returns:
        The acquisition, or the classified failure.
    """
    try:
        audio = download(entry.url, cache_dir, entry.hash)
    except ProbeError as e:
        return classify_probe_failure(str(e))
    meta: dict[str, str | int | None] = {
        "title": audio.title,
        "channel": audio.channel,
        "duration_min": audio.duration_min,
    }
    prompt = _prompt(audio.title, audio.channel, audio.description)
    return Acquired(audio=audio.path, meta=meta, prompt=prompt, prefix=audio.description)


def acquire_podcast_audio(
    entry: LedgerEntry, enrichment_path: Path, cache_dir: Path, transport: Transport
) -> Acquired | Classification:
    """Acquire a podcast unit's audio from its recorded enclosure (§9).

    The enclosure URL and the show notes come from the enrichment file the
    podcast driver's park wrote; a cached download under the entry hash is
    reused. Enclosure 404s classify ``dead`` but are returned as ``manual``
    by the caller's mapping — the EPISODE isn't confirmed gone just because
    a (often signed, expiring) enclosure URL stopped serving.

    Args:
        entry: The waiting ledger entry.
        enrichment_path: ``enrichment/<item>/podcast-<hash6>.md``.
        cache_dir: ``cache/audio/``.
        transport: The HTTP seam for the enclosure GET.

    Returns:
        The acquisition, or the classified failure.
    """
    if not enrichment_path.exists():
        return Classification(
            status=Status.MANUAL,
            reason=(
                "waiting transcribe entry has no enrichment record (no enclosure pointer) "
                "— requeue the unit so the podcast driver re-resolves it"
            ),
        )
    fields, body = read_enrichment(enrichment_path)
    enclosure = fields.get("enclosure")
    if not enclosure:
        return Classification(
            status=Status.MANUAL,
            reason=(
                "enrichment record carries no enclosure URL — requeue the unit so the "
                "podcast driver re-resolves it"
            ),
        )
    notes = body.split(f"\n{_TRANSCRIPT_HEADING}\n")[0].rstrip()
    meta: dict[str, str | int | None] = {
        key: value for key, value in fields.items() if key not in ("url", "fetched")
    }
    prompt = _prompt(fields.get("title"), fields.get("show"), notes)
    audio = _cached_audio(cache_dir, entry.hash)
    if audio is None:
        outcome = _download_enclosure(enclosure, cache_dir, entry.hash, transport)
        if isinstance(outcome, Classification):
            return outcome
        audio = outcome
    return Acquired(audio=audio, meta=meta, prompt=prompt, prefix=notes)


def _cached_audio(cache_dir: Path, stem: str) -> Path | None:
    return next(iter(sorted(cache_dir.glob(f"{stem}.*"))), None)


def _download_enclosure(
    url: str, cache_dir: Path, stem: str, transport: Transport
) -> Path | Classification:
    try:
        response = transport(url)
    except OSError as e:
        return classify_connection(e)
    if not response.ok:
        return classify_http(response.status)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{stem}.{_audio_ext(url)}"
    path.write_bytes(response.body)
    return path


def _audio_ext(url: str) -> str:
    tail = urlsplit(url).path.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else ""
    if not ext or len(ext) > 4 or not ext.isalnum():  # noqa: PLR2004 — 4-char extension heuristic
        return _AUDIO_EXT_DEFAULT
    return ext


def _prompt(*parts: str | None) -> str:
    """Known-vocabulary priming, single-line, bounded (§6)."""
    text = " — ".join(" ".join(part.split()) for part in parts if part)
    return text[:_PROMPT_MAX_CHARS]


# ---------------------------------------------------------------------------
# Transcript bodies. Raw transcripts, stamped via/model by the caller (§6);
# corrections live downstream in digest/wiki where judgment operates.
# ---------------------------------------------------------------------------


def youtube_body(description: str, transcript: str) -> str:
    """Description + transcript sections — the youtube driver's own pattern."""
    if description:
        return f"## Description\n\n{description}\n\n{_TRANSCRIPT_HEADING}\n\n{transcript}"
    return transcript


def podcast_body(show_notes: str, transcript: str) -> str:
    """Show notes (from the feed, §9) followed by the transcript section."""
    if show_notes:
        return f"{show_notes}\n\n{_TRANSCRIPT_HEADING}\n\n{transcript}"
    return f"{_TRANSCRIPT_HEADING}\n\n{transcript}"


# ---------------------------------------------------------------------------
# Enrichment reads. The inverse of run.py's renderer, for the two fields the
# drain needs (frontmatter pointers, the pre-transcript body) — enrichment
# files are machine-written, so the simple shape is guaranteed.
# ---------------------------------------------------------------------------


def read_enrichment(path: Path) -> tuple[dict[str, str], str]:
    """Parse one enrichment file into (frontmatter fields, body).

    Args:
        path: The enrichment markdown file.

    Returns:
        The fields (JSON-quoted values unquoted) and the stripped body.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text.strip()
    head, _sep, body = text[4:].partition("\n---\n")
    fields: dict[str, str] = {}
    for line in head.split("\n"):
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        value = raw.strip()
        if value.startswith('"'):
            # On a malformed quote the raw text stands — a pointer beats a crash.
            with contextlib.suppress(json.JSONDecodeError):
                value = json.loads(value)
        fields[key.strip()] = value
    return fields, body.strip()
