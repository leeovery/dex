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
import unicodedata
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
    "HEAD_MAX_TOKENS",
    "PROMPT_MAX_TOKENS",
    "TRANSCRIBE_RUN_CAP",
    "Acquired",
    "DownloadAudio",
    "YoutubeAudio",
    "acquire_podcast_audio",
    "acquire_youtube_audio",
    "estimated_tokens",
    "keep_first_tokens",
    "keep_last_tokens",
    "podcast_body",
    "read_enrichment",
    "youtube_body",
    "yt_dlp_audio",
]

# Per-run transcription cap: a first sync's resurrected backlog must
# never monopolize a machine. `enrich transcribe --limit` overrides.
TRANSCRIBE_RUN_CAP = 10

# Whisper's prompt window is 223 tokens (`previous_tokens[-(448 // 2 - 1):]`)
# and it keeps the LAST of them, so anything overlong loses its FRONT. The
# composed prompt therefore ends with the title/show and begins with the
# trimmable vocabulary: whatever whisper drops, it drops from the show notes.
# The budgets below decide how much vocabulary survives, never whether the
# names do — which is why an estimated token count is enough and no
# tokenizer is loaded here (see _token_cost). 200 leaves the window room
# for whisper's own framing tokens.
PROMPT_MAX_TOKENS = 200

# The title/show anchor's own ceiling. A real one costs ~20 tokens; the cap
# exists so a pathological title cannot eat the whole prompt.
HEAD_MAX_TOKENS = 60

_SEPARATOR = " — "
_AUDIO_EXT_DEFAULT = "mp3"
# The body sections both transcribable kinds compose around. The youtube
# driver writes the same two headings on its parks (drivers/youtube.py);
# it cannot import them from here (this module imports that one), so the
# pairing is pinned by test.
_TRANSCRIPT_HEADING = "## Transcript"
_DESCRIPTION_HEADING = "## Description"

# The frontmatter key the transcriber stamps (run.py writes via/model onto
# every transcript it composes). Neither park writes it, so it is the one
# fact on disk that says whether a body already holds a transcript.
_TRANSCRIBED_FIELD = "via"


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
    entry: LedgerEntry, enrichment_path: Path, cache_dir: Path, download: DownloadAudio
) -> Acquired | Classification:
    """Acquire a YouTube unit's audio via the yt-dlp seam.

    The description the park already wrote is the body the transcript
    joins — the same composition the podcast path uses — so a re-drain
    appends to what is on disk instead of writing over it. A park that had
    no description to write falls back to the probe's own.

    Args:
        entry: The waiting ledger entry.
        enrichment_path: ``enrichment/<item>/youtube-<hash6>.md``.
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
    description = _stored_description(enrichment_path) or audio.description
    prompt = _prompt(audio.title, audio.channel, description)
    return Acquired(audio=audio.path, meta=meta, prompt=prompt, prefix=description)


def _stored_description(path: Path) -> str:
    """The description a youtube park already wrote, or "" when there is none."""
    if not path.exists():
        return ""
    fields, body = read_enrichment(path)
    head = _pre_transcript(fields, body)
    if head.startswith(_DESCRIPTION_HEADING):
        return head[len(_DESCRIPTION_HEADING) :].strip()
    return head


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
    notes = _pre_transcript(fields, body)
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


def _pre_transcript(fields: dict[str, str], body: str) -> str:
    """The show-notes half of a park/output body — everything before the transcript.

    A body only holds a transcript section if this drain composed it, and
    the frontmatter is what says so. A park's body is notes end to end,
    however many "## Transcript" lines a description or a publisher's show
    notes happen to contain: reading it by the heading alone truncated the
    notes at the author's own line, and the drain then wrote that
    truncation back to disk, losing the tail for good.

    A drained no-notes episode's body STARTS with the transcript heading;
    the newline-anchored split below would miss it and hand the previous
    transcript back as "notes", duplicating it on a re-drain. That split
    takes the LAST section, because the transcript is what the drain
    appended last.
    """
    if _TRANSCRIBED_FIELD not in fields:
        return body
    if body == _TRANSCRIPT_HEADING or body.startswith(f"{_TRANSCRIPT_HEADING}\n"):
        return ""
    return body.rsplit(f"\n{_TRANSCRIPT_HEADING}\n", maxsplit=1)[0].rstrip()


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
    """Known-vocabulary priming, single-line, with the names LAST.

    Whisper reads a prompt from its END and discards the front of anything
    that overflows its 223-token window, so the head — title, then
    channel/show — is written at the END, behind the vocabulary. Two things
    then have to go wrong before the names are lost rather than one: the
    prompt must overflow the window AND the overflow must reach past the
    show notes. The vocabulary is trimmed from its own tail (show notes
    open with the description and close with sponsor links), the head from
    its own tail, both against a token estimate.

    Args:
        title: The episode/video title.
        show: The show or channel name.
        vocabulary: Show notes or description — the trimmable part.

    Returns:
        The priming text, an estimated :data:`PROMPT_MAX_TOKENS` at most.
    """
    parts = (_flat(part) for part in (title, show) if part and part.strip())
    head = keep_first_tokens(_SEPARATOR.join(parts), HEAD_MAX_TOKENS).strip()
    vocab = _flat(vocabulary)
    if not head:
        return keep_first_tokens(vocab, PROMPT_MAX_TOKENS).strip()
    room = PROMPT_MAX_TOKENS - estimated_tokens(head) - estimated_tokens(_SEPARATOR)
    vocab = keep_first_tokens(vocab, room).strip() if room > 0 else ""
    if not vocab:
        return head
    return f"{vocab}{_SEPARATOR}{head}"


def _flat(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# The token budget. Whisper counts its prompt window in TOKENS, so a
# character cap is a different quantity in every script: 600 characters is
# ~180 tokens of Cyrillic but ~690 of Chinese, and the overflow is taken off
# the front. Costs below are whisper-BPE tokens per character, measured
# against openai/whisper-tiny's tokenizer over natural-language samples
# (English prose 0.22, English jargon 0.37, Cyrillic 0.29, Greek 0.41,
# Hebrew 0.56, Arabic 0.57, Hangul 0.76, Thai 0.96, kana/Han 0.80-1.15,
# Devanagari 1.12, emoji and mathematical symbols 1.7-2.2) and rounded up
# per family.
#
# An estimate, not that tokenizer: loading it means a HuggingFace fetch of
# whisper-tiny's tokenizer.json on first use, in a step that composes
# prompts for remote providers that need no local model and may not be
# running whisper's tokenizer at all. It buys exactness the placement of the
# head has already made unnecessary — a mis-estimate here costs vocabulary.
# ---------------------------------------------------------------------------


def _token_cost(char: str) -> float:
    if char < "Ā":  # ASCII and Latin-1
        return 0.4
    if unicodedata.category(char)[0] == "S":  # emoji, mathematical, other symbols
        return 2.5
    if char < "֐":  # Latin extended, IPA, Greek, Cyrillic, Armenian
        return 0.6
    if char < "ࠀ":  # Hebrew, Arabic, Syriac, Thaana
        return 0.8
    return 1.5  # Indic, Thai, Hangul, kana, Han, and every other script


def estimated_tokens(text: str) -> float:
    """Estimate what whisper's tokenizer will make of ``text``.

    Args:
        text: Any prompt fragment.

    Returns:
        The estimated token count — deliberately generous per character.
    """
    return sum(_token_cost(char) for char in text)


def keep_first_tokens(text: str, budget: float) -> str:
    """The longest PREFIX of ``text`` estimated to fit ``budget`` tokens.

    Args:
        text: The text to trim.
        budget: The token allowance.

    Returns:
        ``text`` itself when it already fits, else its trimmed front.
    """
    spent = 0.0
    for index, char in enumerate(text):
        spent += _token_cost(char)
        if spent > budget:
            return text[:index]
    return text


def keep_last_tokens(text: str, budget: float) -> str:
    """The longest SUFFIX of ``text`` estimated to fit ``budget`` tokens.

    The suffix is what survives whisper's own truncation, so this is the
    trim that composes with it rather than against it.

    Args:
        text: The text to trim.
        budget: The token allowance.

    Returns:
        ``text`` itself when it already fits, else its trimmed tail.
    """
    spent = 0.0
    for index, char in enumerate(reversed(text)):
        spent += _token_cost(char)
        if spent > budget:
            return text[len(text) - index :]
    return text


# ---------------------------------------------------------------------------
# Transcript bodies. Raw transcripts, stamped via/model by the caller;
# corrections live downstream in digest/wiki where judgment operates.
# ---------------------------------------------------------------------------


def youtube_body(description: str, transcript: str) -> str:
    """Description + transcript sections — the youtube driver's own pattern.

    The transcript is always its own labelled section: a re-drain splits
    the stored body on that heading, and a bare transcript would come back
    as "description" and be duplicated under itself.
    """
    if description:
        return f"{_DESCRIPTION_HEADING}\n\n{description}\n\n{_TRANSCRIPT_HEADING}\n\n{transcript}"
    return f"{_TRANSCRIPT_HEADING}\n\n{transcript}"


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
