"""Tests for whisper-api: hermetic via fake transport + fake ffmpeg runner (§15)."""

import json
import subprocess
from pathlib import Path

import pytest

from dex_engine.capabilities.transcribe.whisper_api import (
    DEFAULT_BASE_URL,
    WhisperApi,
    _chunk_prompt,
)
from dex_engine.pipeline.classify import ProviderInputError, ProviderUnavailableError


class FakePost:
    """Records every multipart POST; answers from a scripted queue."""

    def __init__(self, responses: list[tuple[int, bytes] | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, *, api_key, fields, filename, file_bytes) -> tuple[int, bytes]:
        self.calls.append(
            {
                "url": url,
                "api_key": api_key,
                "fields": dict(fields),
                "filename": filename,
                "bytes": file_bytes,
            }
        )
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fake_segmenter(chunks: int):
    """A fake ffmpeg runner: writes `chunks` files where the template points."""

    def segment(args: list[str]) -> None:
        template = Path(args[-1])
        for n in range(chunks):
            path = Path(str(template).replace("%03d", f"{n:03d}"))
            path.write_bytes(f"chunk-{n}-audio".encode())

    return segment


def ok(text: str) -> tuple[int, bytes]:
    return 200, json.dumps({"text": text}).encode()


def api(  # noqa: PLR0913 — the builder mirrors the provider's seams
    post: FakePost,
    *,
    chunks: int = 1,
    api_key: str | None = "sk-test",
    base_url: str | None = None,
    env: dict[str, str] | None = None,
    segment=None,
) -> WhisperApi:
    return WhisperApi(
        api_key=api_key,
        base_url=base_url,
        post=post,
        segment=segment if segment is not None else fake_segmenter(chunks),
        which=lambda _name: "/usr/bin/ffmpeg",
        env=env if env is not None else {},
    )


class TestAvailability:
    def test_needs_a_key_or_an_explicit_base_url(self):
        bare = WhisperApi(env={}, which=lambda _n: "/usr/bin/ffmpeg")
        availability = bare.available()
        assert availability.ok is False
        assert "OPENAI_API_KEY" in availability.reason

    def test_env_key_suffices(self):
        provider = WhisperApi(env={"OPENAI_API_KEY": "sk-env"}, which=lambda _n: "/x/ffmpeg")
        assert provider.available().ok is True

    def test_config_key_wins_over_env(self):
        post = FakePost([ok("hi")])
        api(post, api_key="sk-config", env={"OPENAI_API_KEY": "sk-env"}).transcribe(
            Path("/a.mp3"), ""
        )
        assert post.calls[0]["api_key"] == "sk-config"

    def test_keyless_base_url_is_a_local_server(self):
        provider = WhisperApi(
            base_url="http://localhost:8000/v1", env={}, which=lambda _n: "/x/ffmpeg"
        )
        assert provider.available().ok is True

    def test_missing_ffmpeg_is_unavailable_with_reason(self):
        provider = WhisperApi(api_key="sk", env={}, which=lambda _n: None)
        availability = provider.available()
        assert availability.ok is False
        assert "ffmpeg" in availability.reason


class TestTranscribe:
    def test_single_chunk_posts_model_prompt_and_audio(self, tmp_path):
        audio = tmp_path / "episode.mp3"
        audio.write_bytes(b"mp3-bytes")
        post = FakePost([ok(" The transcript. ")])
        text = api(post).transcribe(audio, "Show vocab: dex, anydoc")
        assert text == "The transcript."
        call = post.calls[0]
        assert call["url"] == f"{DEFAULT_BASE_URL}/audio/transcriptions"
        assert call["fields"]["model"] == "whisper-1"
        assert call["fields"]["prompt"] == "Show vocab: dex, anydoc"
        assert call["filename"].endswith(".mp3")
        assert call["bytes"] == b"chunk-0-audio"

    def test_chunks_concatenate_with_prompt_continuity(self, tmp_path):
        # §6: ~20-min segments, each transcribed with the running
        # transcript's tail priming the next, transcripts concatenated.
        audio = tmp_path / "long.mp3"
        audio.write_bytes(b"x")
        post = FakePost([ok("First part ends here."), ok("Second part."), ok("Third.")])
        text = api(post, chunks=3).transcribe(audio, "vocab")
        assert text == "First part ends here. Second part. Third."
        prompts = [call["fields"]["prompt"] for call in post.calls]
        assert prompts[0] == "vocab"
        assert prompts[1] == "vocab\nFirst part ends here."
        assert prompts[2].startswith("vocab\n")
        assert prompts[2].endswith("Second part.")

    def test_custom_base_url_is_respected(self, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"x")
        post = FakePost([ok("hi")])
        api(post, base_url="https://api.groq.com/openai/v1/").transcribe(audio, "")
        assert post.calls[0]["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"

    def test_http_400_is_bad_input(self, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"x")
        post = FakePost([(400, b'{"error": {"message": "could not decode audio"}}')])
        with pytest.raises(ProviderInputError, match="rejected the audio"):
            api(post).transcribe(audio, "")

    @pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
    def test_other_http_failures_keep_the_job_waiting(self, tmp_path, status):
        # Auth, rate limits, outages: the capability is not truly available
        # (§6) — the job stays waiting with the reason, retried next run.
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"x")
        post = FakePost([(status, b"")])
        with pytest.raises(ProviderUnavailableError, match=f"HTTP {status}"):
            api(post).transcribe(audio, "")

    def test_connection_failure_keeps_the_job_waiting(self, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"x")
        post = FakePost([OSError("connection refused")])
        with pytest.raises(ProviderUnavailableError, match="unreachable"):
            api(post).transcribe(audio, "")

    def test_unparseable_response_keeps_the_job_waiting(self, tmp_path):
        audio = tmp_path / "a.mp3"
        audio.write_bytes(b"x")
        with pytest.raises(ProviderUnavailableError, match="unparseable"):
            api(FakePost([(200, b"<html>gateway</html>")])).transcribe(audio, "")

    def test_chunks_are_transcoded_to_mp3_never_stream_copied(self, tmp_path):
        # The cache filename's extension is a guess (extensionless
        # enclosures default .mp3): `-c copy` into that suffix fails on
        # AAC-in-.mp3 — honest audio mislabeled manual. Transcoding decodes
        # whatever the bytes really are.
        audio = tmp_path / "episode.mp3"  # possibly AAC, despite the name
        audio.write_bytes(b"x")
        recorded: dict[str, list[str]] = {}

        def segment(args: list[str]) -> None:
            recorded["args"] = args
            fake_segmenter(1)(args)

        api(FakePost([ok("hi")]), segment=segment).transcribe(audio, "")
        args = recorded["args"]
        assert "copy" not in args
        assert "libmp3lame" in args
        assert "-vn" in args  # cover art is not audio
        assert args[-1].endswith(".mp3")

    def test_ffmpeg_segment_failure_is_bad_input(self, tmp_path):
        audio = tmp_path / "corrupt.mp3"
        audio.write_bytes(b"x")

        def broken(args: list[str]) -> None:
            raise subprocess.CalledProcessError(1, args, stderr=b"Invalid data found")

        with pytest.raises(ProviderInputError, match="segment"):
            api(FakePost([]), segment=broken).transcribe(audio, "")

    def test_missing_ffmpeg_binary_keeps_the_job_waiting(self, tmp_path):
        # An OSError from the runner means the BINARY is broken — an
        # availability failure discovered at call time (§6), never a
        # judgment on the audio.
        audio = tmp_path / "fine.mp3"
        audio.write_bytes(b"x")

        def missing(_args: list[str]) -> None:
            raise OSError("No such file or directory: 'ffmpeg'")

        with pytest.raises(ProviderUnavailableError, match="not runnable"):
            api(FakePost([]), segment=missing).transcribe(audio, "")

    def test_no_segments_produced_is_bad_input(self, tmp_path):
        audio = tmp_path / "empty.mp3"
        audio.write_bytes(b"")
        with pytest.raises(ProviderInputError, match="no audio segments"):
            api(FakePost([]), chunks=0).transcribe(audio, "")

    def test_empty_transcripts_are_bad_input(self, tmp_path):
        audio = tmp_path / "silent.mp3"
        audio.write_bytes(b"x")
        with pytest.raises(ProviderInputError, match="no speech"):
            api(FakePost([ok("  ")])).transcribe(audio, "")


class TestChunkPrompt:
    def test_first_chunk_is_the_initial_prompt_alone(self):
        assert _chunk_prompt("vocab", []) == "vocab"

    def test_tail_is_bounded(self):
        prompt = _chunk_prompt("v", ["word " * 500])
        assert len(prompt) <= 402  # initial + newline + bounded tail
