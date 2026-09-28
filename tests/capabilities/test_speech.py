"""Voice detection before a post's video is transcribed: silence, the length bound, bad input."""

import shutil
import struct
import subprocess
import wave
from contextlib import contextmanager
from pathlib import Path

import av
import faster_whisper.audio
import faster_whisper.vad
import pytest

from dex_engine.capabilities.transcribe import speech
from dex_engine.capabilities.transcribe.speech import cannot_tell, hears_speech

SPEECH = Path(__file__).resolve().parents[1] / "fixtures" / "audio" / "speech.opus"


def silence(path: Path, seconds: int) -> Path:
    """A 16kHz mono WAV of digital silence."""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b"\x00\x00" * 16000 * seconds)
    return path


def silence_tagged_badly(path: Path) -> Path:
    """One second of silence whose title tag is not UTF-8, as a phone's export can carry."""
    fmt = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
    title = b"\xff\xfe\xfa take 1\x00"
    info = b"INFO" + b"INAM" + struct.pack("<I", len(title)) + title
    data = b"\x00\x00" * 16000
    # A RIFF chunk of odd length is followed by a pad byte its size leaves out.
    chunks = b"".join(
        name + struct.pack("<I", len(body)) + body + b"\x00" * (len(body) % 2)
        for name, body in ((b"fmt ", fmt), (b"LIST", info), (b"data", data))
    )
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks)
    return path


def refuse_decode(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("the clip was decoded")


class TestHearsSpeech:
    def test_silence_holds_no_speech(self, tmp_path):
        # The real decoder and the real detector: a silent screen recording
        # is heard as holding nothing, where a transcriber would invent text.
        assert hears_speech(silence(tmp_path / "quiet.wav", 2)) is False

    def test_real_speech_is_heard(self):
        # The real decoder and the real detector on a voice: a detector gone
        # deaf would park every talking video as holding no speech. Three
        # seconds of Neil Armstrong on the Moon (NASA, public domain, from
        # Wikimedia Commons' "Armstrong Small Step.ogg"), radio noise and all.
        assert hears_speech(SPEECH) is True

    def test_speech_found_anywhere_is_speech(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            faster_whisper.vad, "get_speech_timestamps", lambda *_a, **_k: [{"start": 0, "end": 8}]
        )
        assert hears_speech(silence(tmp_path / "talk.wav", 2)) is True

    def test_a_clip_whose_metadata_is_not_utf8_is_still_asked_about(self, tmp_path):
        # Read strictly, the tag fails the open, and the silent clip would
        # reach a transcriber unasked.
        assert hears_speech(silence_tagged_badly(tmp_path / "tagged.wav")) is False

    def test_the_clip_is_decoded_at_the_rate_the_detector_reads(self, tmp_path, monkeypatch):
        rates: list[object] = []
        decode = faster_whisper.audio.decode_audio

        def spy(path: str, *, sampling_rate: int | None = None, split_stereo: bool = False):
            rates.append(sampling_rate)
            return decode(path, sampling_rate=sampling_rate or 16000, split_stereo=split_stereo)

        monkeypatch.setattr(faster_whisper.audio, "decode_audio", spy)
        assert hears_speech(silence(tmp_path / "quiet.wav", 1)) is False
        assert rates == [16000]

    def test_audio_that_does_not_decode_cannot_tell(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42 not a video")
        assert hears_speech(clip) is None

    def test_a_container_with_no_audio_stream_cannot_tell(self, tmp_path):
        # faster-whisper's decoder indexes the stream list unguarded; the
        # transcriber then reports the missing stream itself, as it always has.
        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg is not on PATH")
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
        assert hears_speech(video) is None

    def test_a_clip_past_the_bound_is_never_decoded(self, tmp_path, monkeypatch):
        # Decoding holds the whole clip in memory, so a long one is not asked
        # about: it reaches the transcriber as every clip did before.
        monkeypatch.setattr(speech, "DETECTION_MAX_SECONDS", 1)
        monkeypatch.setattr(faster_whisper.audio, "decode_audio", refuse_decode)
        assert hears_speech(silence(tmp_path / "long.wav", 2)) is None

    def test_a_clip_exactly_at_the_bound_is_asked_about(self, tmp_path, monkeypatch):
        monkeypatch.setattr(speech, "DETECTION_MAX_SECONDS", 2)
        assert hears_speech(silence(tmp_path / "edge.wav", 2)) is False

    def test_a_container_stating_no_duration_cannot_tell(self, tmp_path, monkeypatch):
        class Unmeasured:
            duration = None

        @contextmanager
        def opened(*_args: object, **_kwargs: object):
            yield Unmeasured()

        monkeypatch.setattr(av, "open", opened)
        monkeypatch.setattr(faster_whisper.audio, "decode_audio", refuse_decode)
        assert hears_speech(silence(tmp_path / "stream.wav", 1)) is None

    @pytest.mark.parametrize(
        "failure",
        [IndexError("tuple index out of range"), EOFError(), RuntimeError("decoder"), OSError()],
    )
    def test_a_decode_failure_cannot_tell(self, tmp_path, monkeypatch, failure):
        def decode(*_args: object, **_kwargs: object) -> None:
            raise failure

        monkeypatch.setattr(faster_whisper.audio, "decode_audio", decode)
        assert hears_speech(silence(tmp_path / "clip.wav", 1)) is None

    def test_a_failure_of_the_detector_itself_propagates(self, tmp_path, monkeypatch):
        # Not the clip's fault: swallowing it would silently let every silent
        # video through to a transcriber again.
        def broken(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("onnxruntime could not load the model")

        monkeypatch.setattr(faster_whisper.vad, "get_speech_timestamps", broken)
        with pytest.raises(RuntimeError, match="onnxruntime"):
            hears_speech(silence(tmp_path / "clip.wav", 1))


def test_the_null_seam_cannot_tell():
    assert cannot_tell(Path("clip.mp4")) is None
