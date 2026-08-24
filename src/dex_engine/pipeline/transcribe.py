"""Transcribe-drain mechanics: audio acquisition and prompt priming.

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

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from dex_engine import atomic
from dex_engine.drivers.transport import HttpResponse, Transport
from dex_engine.drivers.ytdlp import (
    DownloadAudio,
    ProbeError,
    cached_audio,
    classify_probe_failure,
)

from .classify import Classification, classify_connection, classify_http
from .detect import looks_like_html
from .enrichment import DESCRIPTION_HEADING, pre_transcript, read_enrichment
from .types import LedgerEntry, Status
from .urls import ext_of

__all__ = [
    "HEAD_MAX_TOKENS",
    "PROMPT_MAX_TOKENS",
    "TRANSCRIBE_RUN_CAP",
    "Acquired",
    "acquire_podcast_audio",
    "acquire_youtube_audio",
    "estimated_tokens",
    "keep_first_tokens",
    "keep_last_tokens",
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
    head = pre_transcript(fields, body)
    if head.startswith(DESCRIPTION_HEADING):
        return head[len(DESCRIPTION_HEADING) :].strip()
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
    notes = pre_transcript(fields, body)
    meta: dict[str, str | int | None] = {
        key: value for key, value in fields.items() if key not in ("url", "fetched")
    }
    prompt = _prompt(fields.get("title"), fields.get("show"), notes)
    audio = cached_audio(cache_dir, entry.hash)
    if audio is None:
        outcome = _download_enclosure(enclosure, cache_dir, entry.hash, transport)
        if isinstance(outcome, Classification):
            return outcome
        audio = outcome
    return Acquired(audio=audio, meta=meta, prompt=prompt, prefix=notes)


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
    path = cache_dir / f"{stem}.{ext_of(url, default='mp3')}"
    # Atomic: a crash mid-write must never leave a truncated file under the
    # final cache name — cached_audio would reuse it as completed audio.
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
