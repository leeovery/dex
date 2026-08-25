"""whisper-api: one OpenAI-compatible transcription provider class.

``base_url`` + key come from config (``transcribe_base_url`` /
``transcribe_api_key``), falling back to the ``OPENAI_BASE_URL`` /
``OPENAI_API_KEY`` environment variables — read at availability time, never
at import; the enrich CLI folds the instance's gitignored ``.env`` into the
environment first, so that file is where the key normally lives. Pointed at
Groq et al for GPU-fast pennies-per-hour runs; an explicitly configured
base_url with no key is a keyless local server.

**ffmpeg chunking** removes upload limits for any provider, including
OpenAI's 25MB cap: the audio is always segmented (~20-minute mp3 chunks,
transcoded — the source extension is a guess and stream-copy into it would
fail on honest audio), each chunk transcribed with prompt continuity (the
running transcript's tail primes the next chunk), and the transcripts
concatenated. Audio at or under one chunk simply yields a single segment —
one uniform path.
"""

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from dex_engine.pipeline.classify import ProviderInputError, ProviderUnavailableError, scrub
from dex_engine.pipeline.types import Availability

__all__ = [
    "CHUNK_SECONDS",
    "DEFAULT_API_MODEL",
    "DEFAULT_BASE_URL",
    "MultipartPost",
    "WhisperApi",
    "run_ffmpeg",
    "urllib_multipart_post",
]

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_MODEL = "whisper-1"

# ~20-minute segments: comfortably under every known provider's upload
# cap at any sane audio bitrate.
CHUNK_SECONDS = 1200

# Whisper's prompt window is ~224 tokens; the continuity tail plus the
# vocabulary priming must fit, so the tail is bounded in characters.
_CONTINUITY_TAIL_CHARS = 400

_HTTP_OK_FLOOR, _HTTP_OK_CEILING = 200, 300
_HTTP_BAD_REQUEST = 400
_TIMEOUT_SECONDS = 600.0


class MultipartPost(Protocol):
    """The injected HTTP seam: one multipart/form-data POST."""

    def __call__(
        self,
        url: str,
        *,
        api_key: str | None,
        fields: Mapping[str, str],
        filename: str,
        file_bytes: bytes,
    ) -> tuple[int, bytes]:
        """POST the form; return (status, body). Connection failures raise OSError."""
        ...


def urllib_multipart_post(
    url: str,
    *,
    api_key: str | None,
    fields: Mapping[str, str],
    filename: str,
    file_bytes: bytes,
) -> tuple[int, bytes]:
    """The one real :class:`MultipartPost`: urllib, bearer auth when keyed.

    Args:
        url: The transcriptions endpoint.
        api_key: Bearer token; ``None`` for a keyless local server.
        fields: Plain form fields (model, prompt, ...).
        filename: The uploaded file's name (providers key the container
            format off its extension).
        file_bytes: The audio chunk.

    Returns:
        ``(status, body)`` — HTTP failures return, never raise.

    Raises:
        OSError: Connection-level failure (DNS, refused, reset, timeout).
    """
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 — https endpoint from config
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def run_ffmpeg(args: list[str]) -> None:
    """Run ffmpeg with ``args`` (binary and log flags prepended), loudly.

    Args:
        args: Everything after ``ffmpeg -nostdin -loglevel error``.

    Raises:
        subprocess.CalledProcessError: ffmpeg exited nonzero (stderr captured).
        OSError: ffmpeg is not runnable.
    """
    subprocess.run(  # noqa: S603 — engine-built args, no shell
        ["ffmpeg", "-nostdin", "-loglevel", "error", *args],  # noqa: S607 — PATH resolution is the availability contract
        check=True,
        capture_output=True,
    )


class WhisperApi:
    """Transcribe via any OpenAI-compatible /audio/transcriptions endpoint."""

    name: str = "whisper-api"

    def __init__(  # noqa: PLR0913 — constructor injection of every seam
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = DEFAULT_API_MODEL,
        post: MultipartPost = urllib_multipart_post,
        segment: Callable[[list[str]], None] = run_ffmpeg,
        which: Callable[[str], str | None] = shutil.which,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Wire credentials and the transport/ffmpeg seams.

        Args:
            base_url: Config ``transcribe_base_url``; falls back to
                ``OPENAI_BASE_URL``, then the OpenAI default.
            api_key: Config ``transcribe_api_key``; falls back to
                ``OPENAI_API_KEY``.
            model: The API-side model name (``--model`` overrides per call
                by rebuilding the provider).
            post: The multipart HTTP seam (tests fake it).
            segment: The ffmpeg runner seam (tests fake it).
            which: PATH lookup for the ffmpeg availability check.
            env: Environment mapping; ``None`` reads ``os.environ`` lazily.
        """
        self.model = model
        self._base_url = base_url
        self._api_key = api_key
        self._post = post
        self._segment = segment
        self._which = which
        self._env = env

    def _environ(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def _key(self) -> str | None:
        return self._api_key or self._environ().get("OPENAI_API_KEY") or None

    def _base(self) -> str | None:
        return self._base_url or self._environ().get("OPENAI_BASE_URL") or None

    def available(self) -> Availability:
        """Honest availability: a key (or an explicit base_url) and ffmpeg."""
        if self._key() is None and self._base() is None:
            return Availability(
                ok=False,
                reason=(
                    "set OPENAI_API_KEY (or transcribe_api_key / transcribe_base_url "
                    "in state/config.json)"
                ),
            )
        if self._which("ffmpeg") is None:
            return Availability(ok=False, reason="ffmpeg is not on PATH (required for chunking)")
        return Availability(ok=True)

    def transcribe(self, audio: Path, initial_prompt: str) -> str:
        """Segment, transcribe each chunk with prompt continuity, concatenate.

        Args:
            audio: The audio file.
            initial_prompt: Known-vocabulary priming for every chunk; later
                chunks additionally carry the running transcript's tail.

        Returns:
            The raw concatenated transcript.

        Raises:
            ProviderInputError: ffmpeg could not read/segment the audio, the
                API rejected it (HTTP 400), or it held no speech — manual.
            ProviderUnavailableError: The ffmpeg binary is not runnable, or
                the endpoint refused or failed (auth, rate limit, outage,
                connection) — the job stays waiting with the reason.
        """
        with tempfile.TemporaryDirectory(prefix="dex-whisper-") as workdir:
            chunks = self._chunk(audio, Path(workdir))
            texts: list[str] = []
            for chunk in chunks:
                prompt = _chunk_prompt(initial_prompt, texts)
                texts.append(self._transcribe_chunk(chunk, prompt))
        text = " ".join(part for part in (t.strip() for t in texts) if part)
        if not text:
            raise ProviderInputError("whisper-api heard no speech in the audio")
        return text

    def _chunk(self, audio: Path, workdir: Path) -> list[Path]:
        """Split the audio into ~20-minute mp3 segments, transcoded.

        Transcoded, never ``-c copy``: the cached filename's extension is a
        guess (extensionless enclosures default to .mp3), and stream-copying
        an AAC stream into a .mp3-named segment makes ffmpeg fail — valid
        audio mislabeled ``manual``. Decoding whatever the bytes really are
        and encoding mp3 also makes the uploaded extension honest.
        """
        template = workdir / "chunk-%03d.mp3"
        try:
            self._segment(
                [
                    "-i",
                    str(audio),
                    "-vn",  # cover-art video streams are not audio
                    "-acodec",
                    "libmp3lame",
                    "-b:a",
                    "96k",
                    "-f",
                    "segment",
                    "-segment_time",
                    str(CHUNK_SECONDS),
                    str(template),
                ]
            )
        except OSError as e:
            # The binary is missing or not runnable: an availability failure
            # discovered at call time — the job stays waiting, never
            # manual; the audio said nothing about itself yet.
            raise ProviderUnavailableError(f"ffmpeg is not runnable: {scrub(str(e))}") from e
        except subprocess.CalledProcessError as e:
            detail = e.stderr.decode(errors="replace") if e.stderr else str(e)
            raise ProviderInputError(f"ffmpeg could not segment the audio: {scrub(detail)}") from e
        chunks = sorted(workdir.glob("chunk-*.mp3"))
        if not chunks:
            raise ProviderInputError("ffmpeg produced no audio segments")
        return chunks

    def _transcribe_chunk(self, chunk: Path, prompt: str) -> str:
        base = (self._base() or DEFAULT_BASE_URL).rstrip("/")
        fields = {"model": self.model, "response_format": "json"}
        if prompt:
            fields["prompt"] = prompt
        try:
            status, body = self._post(
                f"{base}/audio/transcriptions",
                api_key=self._key(),
                fields=fields,
                filename=chunk.name,
                file_bytes=chunk.read_bytes(),
            )
        except OSError as e:
            raise ProviderUnavailableError(
                f"whisper-api endpoint unreachable: {scrub(str(e))}"
            ) from e
        if status == _HTTP_BAD_REQUEST:
            raise ProviderInputError(
                f"whisper-api rejected the audio: {scrub(body.decode(errors='replace'))}"
            )
        if not _HTTP_OK_FLOOR <= status < _HTTP_OK_CEILING:
            # Auth, rate limits, outages: the capability is, in truth, not
            # available right now — the job stays waiting and retries when
            # the drain next runs.
            raise ProviderUnavailableError(f"whisper-api returned HTTP {status}")
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise ProviderUnavailableError("whisper-api returned an unparseable response") from e
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise ProviderUnavailableError("whisper-api response carried no transcript text")
        return text


def _chunk_prompt(initial_prompt: str, previous: list[str]) -> str:
    """Vocabulary priming plus the running transcript's tail (chunk continuity)."""
    if not previous:
        return initial_prompt
    tail = " ".join(previous)[-_CONTINUITY_TAIL_CHARS:].strip()
    return f"{initial_prompt}\n{tail}".strip()
