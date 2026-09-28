"""Voice detection: whether a post's video holds speech, asked before any transcriber hears it.

A transcriber handed audio with no speech in it does not come back empty.
It comes back with text nobody said: a silent screen recording was heard as
sentences about online jobs and ibuprofen, a music-only clip as a line of
skewer emoji, and a clip primed with its post's words as those words read
back. Silero's voice detector, which faster-whisper ships, found no speech
at all in each of them, and found it along the whole length of every clip
someone talks in. So the drain asks it first, and a clip it finds silent is
heard as holding no speech, with nothing transcribed.

faster-whisper and PyAV are lazy-imported, as in ``whisper_local``.
"""

from collections.abc import Callable
from pathlib import Path

__all__ = ["DETECTION_MAX_SECONDS", "HearsSpeech", "cannot_tell", "hears_speech"]

# The seam: True when the detector finds speech, False when it finds none,
# None when it cannot tell, and the clip then reaches the transcriber.
HearsSpeech = Callable[[Path], bool | None]

# Detection decodes the whole clip into memory as 16kHz samples, so it asks
# only about a clip up to this long. A longer one reaches the transcriber
# as every clip did before.
DETECTION_MAX_SECONDS = 15 * 60

_RATE = 16000


def hears_speech(audio: Path) -> bool | None:
    """Whether voice detection finds any speech in ``audio``, or None when it cannot tell.

    It cannot tell when the container states no duration, or one past
    :data:`DETECTION_MAX_SECONDS`, or when the audio does not decode: a
    container with no audio stream at all is one, and the transcriber
    reports that itself, as it always has. A failure of the detector itself
    is not the clip's and propagates.

    Args:
        audio: The downloaded video or audio.

    Returns:
        True when any speech is found, False when none is, else None.
    """
    import av  # noqa: PLC0415 — lazy: heavy dep
    from faster_whisper.audio import decode_audio  # noqa: PLC0415 — lazy: heavy dep
    from faster_whisper.vad import VadOptions, get_speech_timestamps  # noqa: PLC0415 — lazy

    try:
        with av.open(str(audio), metadata_errors="ignore") as container:
            duration = container.duration
        if duration is None or duration > DETECTION_MAX_SECONDS * av.time_base:
            return None
        samples = decode_audio(str(audio), sampling_rate=_RATE)
    except (av.error.FFmpegError, OSError, ValueError, RuntimeError, EOFError, IndexError):
        # PyAV raises its decode failures as these; a container with no
        # audio stream fails as a bare IndexError inside faster-whisper.
        return None
    return bool(get_speech_timestamps(samples, VadOptions(), sampling_rate=_RATE))


def cannot_tell(_audio: Path) -> None:
    """The null seam: nothing is detected, so every clip reaches the transcriber."""
