"""Tests for whisper-local: fake model seams; the real model is live-only."""

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from dex_engine.capabilities.transcribe.whisper_local import WhisperLocal
from dex_engine.pipeline.classify import ProviderInputError, ProviderUnavailableError


class Segment:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeModel:
    def __init__(self, texts: list[str], *, raise_: Exception | None = None) -> None:
        self.texts = texts
        self.raise_ = raise_
        self.calls: list[tuple[str, str | None]] = []

    def transcribe(self, audio: str, *, initial_prompt: str | None):
        self.calls.append((audio, initial_prompt))
        if self.raise_ is not None:
            raise self.raise_
        return (Segment(t) for t in self.texts), object()


def local(model_obj, *, model: str = "medium", cached: bool = True) -> WhisperLocal:
    return WhisperLocal(model=model, load=lambda _name: model_obj, cached=lambda _m: cached)


class TestAvailability:
    def test_available_with_cached_model(self):
        assert local(FakeModel([])).available().ok is True
        assert local(FakeModel([])).available().reason == ""

    def test_uncached_model_is_available_with_the_first_run_note(self):
        # The first run downloads the model — availability holds, and
        # the note lets the report explain a slow first transcription.
        availability = local(FakeModel([]), cached=False).available()
        assert availability.ok is True
        assert "first transcription downloads it" in availability.reason

    def test_unknown_model_name_is_unavailable_with_reason(self):
        availability = local(FakeModel([]), model="enormous").available()
        assert availability.ok is False
        assert "unknown whisper model" in availability.reason

    def test_hf_repo_ids_are_accepted(self):
        availability = local(FakeModel([]), model="Systran/faster-whisper-large-v3").available()
        assert availability.ok is True

    def test_a_repo_id_huggingface_rejects_is_unavailable_not_a_crash(self):
        # `Systran/faster-whisper/large-v3` (an easy typo for the real
        # `Systran/faster-whisper-large-v3`) has a slash, so the name check
        # waves it through and HF's cache lookup raises HFValidationError.
        # The real cache seam runs here — the point is that it never
        # escapes the probe every CLI verb makes.
        availability = WhisperLocal(model="Systran/faster-whisper/large-v3").available()
        assert availability.ok is False
        assert "unknown whisper model" in availability.reason

    def test_an_unreadable_cache_is_unavailable_with_its_own_reason(self):
        def cached(_model: str) -> bool:
            raise PermissionError(13, "Permission denied")

        model = FakeModel([])
        provider = WhisperLocal(model="medium", load=lambda _n: model, cached=cached)
        availability = provider.available()
        assert availability.ok is False
        assert "cache is unreadable" in availability.reason

    def test_import_probe_never_crashes(self):
        # On this machine faster-whisper imports; the probe path for a
        # broken install is the (ImportError, OSError) net — asserted by
        # the availability contract being a value, not an exception.
        assert isinstance(WhisperLocal().available().ok, bool)


class TestTranscribe:
    def test_joins_segments_and_passes_the_prompt(self):
        model = FakeModel([" Hello", " world."])
        provider = local(model)
        text = provider.transcribe(Path("/audio/a.m4a"), "Names: anydoc, ffmpeg")
        assert text == "Hello world."
        assert model.calls == [("/audio/a.m4a", "Names: anydoc, ffmpeg")]

    def test_empty_prompt_disables_priming(self):
        model = FakeModel(["ok"])
        local(model).transcribe(Path("/audio/a.m4a"), "")
        assert model.calls[0][1] is None

    def test_decode_failure_is_bad_input(self):
        # PyAV surfaces corrupt audio as OSError/ValueError during the lazy
        # segment iteration — bad input, the manual path.
        model = FakeModel([], raise_=ValueError("Invalid data found when processing input"))
        with pytest.raises(ProviderInputError, match="could not decode"):
            local(model).transcribe(Path("/audio/corrupt.mp3"), "")

    def test_a_container_with_no_audio_stream_is_bad_input(self):
        model = FakeModel([], raise_=IndexError("tuple index out of range"))
        with pytest.raises(ProviderInputError, match=r"no audio stream in videoonly\.mp4"):
            local(model).transcribe(Path("/audio/videoonly.mp4"), "")

    def test_real_video_only_container_raises_the_indexerror_we_map(self, tmp_path):
        # Pins the world half of the mapping above: faster-whisper's own
        # decode indexes the stream list unguarded, so a video-only file
        # surfaces as IndexError and not as any decode error. Decoding
        # needs no model, so this stays off the live marker.
        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg is not on PATH")
        from faster_whisper.audio import decode_audio  # noqa: PLC0415 — heavy dep, one test

        video = tmp_path / "videoonly.mp4"
        subprocess.run(  # noqa: S603 — fixed args, no shell
            [
                shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
            ],
            check=True,
            capture_output=True,
        )  # fmt: skip
        with pytest.raises(IndexError):
            decode_audio(str(video))

    def test_silent_audio_is_bad_input(self):
        with pytest.raises(ProviderInputError, match="no speech"):
            local(FakeModel([])).transcribe(Path("/audio/silence.wav"), "")

    def test_model_load_failure_keeps_the_job_waiting(self):
        def load(_name: str):
            raise OSError("connection reset downloading model.bin")

        provider = WhisperLocal(model="medium", load=load, cached=lambda _m: False)
        with pytest.raises(ProviderUnavailableError, match="could not load model"):
            provider.transcribe(Path("/audio/a.m4a"), "")

    def test_hf_rate_limit_keeps_the_job_waiting(self):
        # An HF 429/5xx during the model download is an availability
        # failure — it must park waiting, never reach the run loop's
        # broad except as an "engine bug". Named explicitly: the OSError
        # ancestry of hf-hub's errors is a transport detail.
        import httpx  # noqa: PLC0415 — heavy transitive dep, loaded only for this pin
        from huggingface_hub.errors import HfHubHTTPError  # noqa: PLC0415 — same

        response = httpx.Response(
            429, request=httpx.Request("GET", "https://huggingface.co/model.bin")
        )

        def load(_name: str):
            raise HfHubHTTPError("429 Client Error: Too Many Requests", response=response)

        provider = WhisperLocal(model="medium", load=load, cached=lambda _m: False)
        with pytest.raises(ProviderUnavailableError, match="could not load model"):
            provider.transcribe(Path("/audio/a.m4a"), "")


@pytest.mark.live
@pytest.mark.ci_hostile  # a cold runner pulls the whole ~75MB model from
# HuggingFace, which throttles anonymous datacenter traffic; the outcome
# reports HF's mood, not the provider's plumbing.
class TestLive:
    def test_tiny_model_transcribes_a_generated_wav(self, tmp_path):
        # Downloads the `tiny` model on first run (~75MB, HF cache). A tone
        # sweep proves the decode/transcribe plumbing end-to-end; whisper
        # may hear anything or nothing in it, so only the shape is pinned.
        wav = tmp_path / "tone.wav"
        with wave.open(str(wav), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(16000)
            frames = bytearray()
            for n in range(16000):
                frames += struct.pack("<h", int(12000 * math.sin(n / 30)))
            f.writeframes(bytes(frames))
        provider = WhisperLocal(model="tiny")
        assert provider.available().ok is True
        try:
            text = provider.transcribe(wav, "a test tone")
        except ProviderInputError:
            return  # "no speech" is a legitimate hearing of a sine sweep
        assert isinstance(text, str)
