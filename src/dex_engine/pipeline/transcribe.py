"""Transcribe-drain mechanics: audio acquisition, priming, bodies.

**Audio acquisition belongs to the drain, not the drivers**: drivers only
ever emit ``needs: transcribe`` with the pointer — a YouTube URL, or an
enclosure URL recorded in the enrichment file's frontmatter. The drain
obtains the audio itself, into ``cache/audio/<hash>.<ext>``; the run layer
(``pipeline/run.py``) owns the control flow and every ledger write, and
calls the pure-ish helpers here.

Audio lifecycle: stays in cache while pending/failed (retries don't
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

from dex_engine import atomic
from dex_engine.drivers.transport import HttpResponse, Transport
from dex_engine.drivers.youtube import ProbeError, classify_probe_failure

from .classify import Classification, classify_connection, classify_http
from .detect import looks_like_html
from .types import LedgerEntry, Status

__all__ = [
    "PROMPT_MAX_CHARS",
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

# Per-run transcription cap: a first sync's resurrected backlog must
# never monopolize a machine. `enrich transcribe --limit` overrides.
TRANSCRIBE_RUN_CAP = 10

# Whisper's prompt window is ~224 tokens and it keeps the LAST of them:
# an overlong prompt loses its FRONT, which is exactly the title/show the
# priming exists to carry. So the whole composed prompt — vocabulary here
# plus whisper-api's continuity tail — is budgeted to ~200 tokens. Jargon
# tokenizes badly: 800 chars of this text measured ~270 tokens (≈2.96
# chars/token), so 3 chars/token is the conservative rate and 600 the cap.
PROMPT_MAX_CHARS = 600

_SEPARATOR = " — "
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
    existing = _cached_audio(cache_dir, stem)
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
        fallback = _cached_audio(cache_dir, stem)
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


@dataclass(frozen=True, slots=True, kw_only=True)
class Acquired:
    """Audio in hand, plus everything the transcript's enrichment needs."""

    audio: Path
    meta: dict[str, str | int | None]
    prompt: str  # initial_prompt priming — the item's known vocabulary
    prefix: str  # body text the transcript follows (description / show notes)


def acquire_youtube_audio(
    entry: LedgerEntry, cache_dir: Path, download: DownloadAudio
) -> Acquired | Classification:
    """Acquire a YouTube unit's audio via the yt-dlp seam.

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
    # The same shape as the captions path's meta (drivers/youtube.py
    # _video_meta): one frontmatter contract per kind, whichever route
    # produced the transcript.
    meta: dict[str, str | int | None] = {
        "title": audio.title,
        "channel": audio.channel,
        "duration_min": audio.duration_min,
        "upload_date": audio.upload_date,
    }
    prompt = _prompt(audio.title, audio.channel, audio.description)
    return Acquired(audio=audio.path, meta=meta, prompt=prompt, prefix=audio.description)


def acquire_podcast_audio(
    entry: LedgerEntry, enrichment_path: Path, cache_dir: Path, transport: Transport
) -> Acquired | Classification:
    """Acquire a podcast unit's audio from its recorded enclosure.

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
    notes = _pre_transcript(body)
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


def _pre_transcript(body: str) -> str:
    """The show-notes half of a park/output body — everything before the transcript.

    A drained no-notes episode's body STARTS with the transcript heading;
    the newline-anchored split below would miss it and hand the previous
    transcript back as "notes", duplicating it on a re-drain.
    """
    if body == _TRANSCRIPT_HEADING or body.startswith(f"{_TRANSCRIPT_HEADING}\n"):
        return ""
    return body.split(f"\n{_TRANSCRIPT_HEADING}\n", maxsplit=1)[0].rstrip()


# In-flight artifacts: yt-dlp's `.part` bodies (also `.part-Frag…`) and
# `.ytdl` state files, the enclosure download's atomic-write `.tmp` temps,
# and the odd `.download`/`.temp` from other tooling.
_PARTIAL_MARKERS = (".part", ".ytdl", ".download", ".temp", ".tmp")


def _is_partial(name: str) -> bool:
    lowered = name.lower()
    return lowered.endswith(_PARTIAL_MARKERS) or ".part-" in lowered


def _cached_audio(cache_dir: Path, stem: str) -> Path | None:
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


def _download_enclosure(
    url: str, cache_dir: Path, stem: str, transport: Transport
) -> Path | Classification:
    try:
        response = transport(url)
    except OSError as e:
        return classify_connection(e)
    if not response.ok:
        return classify_http(response.status)
    unusable = _not_audio(response)
    if unusable is not None:
        # Never cached under <hash>.<ext>: a stored error page is
        # indistinguishable from audio on the retry, and the provider
        # would park the episode manual forever over the CDN's mistake.
        return Classification(status=Status.BLOCKED, reason=unusable)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{stem}.{_audio_ext(url)}"
    # Atomic: a crash mid-write must never leave a truncated file under the
    # final cache name — _cached_audio would reuse it as completed audio.
    atomic.write_bytes(path, response.body)
    return path


def _not_audio(response: HttpResponse) -> str | None:
    """Why this 200 body cannot be the episode's audio, or None.

    A CDN serving an error page — or half a body — under a 200 says
    nothing about the EPISODE, so it is ``blocked`` and retried, not the
    ``manual`` a provider would produce after failing to decode the
    garbage. Only shapes that are certainly not audio are caught here;
    real audio that turns out to be broken still reaches the provider and
    still parks manual with what the decoder said.
    """
    if not response.body:
        return "enclosure returned an empty response body"
    if response.content_length is not None and len(response.body) < response.content_length:
        return (
            f"enclosure body is truncated ({len(response.body)} of "
            f"{response.content_length} declared bytes)"
        )
    if looks_like_html(response.body):
        return f"enclosure served an HTML page ({response.content_type or 'no content type'})"
    return None


def _audio_ext(url: str) -> str:
    tail = urlsplit(url).path.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else ""
    if not ext or len(ext) > 4 or not ext.isalnum():  # noqa: PLR2004 — 4-char extension heuristic
        return _AUDIO_EXT_DEFAULT
    return ext


def _prompt(title: str | None, show: str | None, vocabulary: str) -> str:
    """Known-vocabulary priming, single-line, budgeted head-first.

    The head (title, then channel/show) always survives: whisper reads a
    prompt from its END, so the VOCABULARY is what gets truncated — from
    its own tail — to fit :data:`PROMPT_MAX_CHARS`. Truncating the
    composed string instead would keep 600 characters of show notes and
    throw away the two names the priming was built for.

    Args:
        title: The episode/video title.
        show: The show or channel name.
        vocabulary: Show notes or description — the trimmable part.

    Returns:
        The priming text, at most :data:`PROMPT_MAX_CHARS` characters.
    """
    head = _SEPARATOR.join(_flat(part) for part in (title, show) if part and part.strip())
    vocab = _flat(vocabulary)
    if not head:
        return vocab[:PROMPT_MAX_CHARS]
    head = head[:PROMPT_MAX_CHARS]
    room = PROMPT_MAX_CHARS - len(head) - len(_SEPARATOR)
    if not vocab or room <= 0:
        return head
    return f"{head}{_SEPARATOR}{vocab[:room]}"


def _flat(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Transcript bodies. Raw transcripts, stamped via/model by the caller;
# corrections live downstream in digest/wiki where judgment operates.
# ---------------------------------------------------------------------------


def youtube_body(description: str, transcript: str) -> str:
    """Description + transcript sections — the youtube driver's own pattern."""
    if description:
        return f"## Description\n\n{description}\n\n{_TRANSCRIPT_HEADING}\n\n{transcript}"
    return transcript


def podcast_body(show_notes: str, transcript: str) -> str:
    """Show notes (from the feed) followed by the transcript section."""
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
