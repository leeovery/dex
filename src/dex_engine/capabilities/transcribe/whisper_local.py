"""whisper-local: faster-whisper on CPU int8 — the free transcription floor (§6).

Default model ``medium`` (config ``transcribe_model``; per-call ``--model``
rebuilds the provider). The model is NOT required to be on disk for the
provider to be available: the first actual transcription downloads it to the
HF cache, once per machine — availability only *notes* an uncached model so
the report can explain a slow first run. No GPU assumptions, no file-length
limits.

faster-whisper is lazy-imported inside methods (§14): tests inject a fake
model loader; the real model runs under the ``live`` marker only.
"""

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from dex_engine.pipeline.classify import ProviderInputError, ProviderUnavailableError, scrub
from dex_engine.pipeline.types import Availability

__all__ = ["LoadedModel", "WhisperLocal", "load_whisper_model", "model_is_cached"]


class _Segment(Protocol):
    text: str


class LoadedModel(Protocol):
    """The loaded-model seam: what ``transcribe`` needs from faster-whisper."""

    def transcribe(
        self, audio: str, *, initial_prompt: str | None
    ) -> tuple[Iterable[_Segment], object]:
        """Transcribe one audio file into lazy segments (+ info, unused)."""
        ...


def load_whisper_model(model: str) -> LoadedModel:
    """Load (downloading on first use) a faster-whisper model, CPU int8 (§6).

    Args:
        model: A size name (``medium``) or a CTranslate2 HF repo id.

    Returns:
        The loaded model.
    """
    from faster_whisper import WhisperModel  # noqa: PLC0415 — lazy: heavy dep (§14)

    return WhisperModel(model, device="cpu", compute_type="int8")


def model_is_cached(model: str) -> bool:
    """Whether the model is already in the HF cache — checked without downloading.

    Args:
        model: A size name or an HF repo id.

    Returns:
        True when the converted model's weights are on disk.
    """
    from faster_whisper.utils import _MODELS  # noqa: PLC0415 — lazy (§14)
    from huggingface_hub import try_to_load_from_cache  # noqa: PLC0415 — lazy (§14)

    repo = model if "/" in model else _MODELS.get(model)
    if repo is None:
        return False
    return isinstance(try_to_load_from_cache(repo, "model.bin"), str)


def _known_model(model: str) -> bool:
    from faster_whisper.utils import _MODELS  # noqa: PLC0415 — lazy (§14)

    return "/" in model or model in _MODELS


class WhisperLocal:
    """Transcribe locally via faster-whisper; the always-available floor."""

    name: str = "whisper-local"

    def __init__(
        self,
        *,
        model: str = "medium",
        load: Callable[[str], LoadedModel] = load_whisper_model,
        cached: Callable[[str], bool] = model_is_cached,
    ) -> None:
        """Wire the model choice and the load/cache seams.

        Args:
            model: The model to run (config ``transcribe_model`` / ``--model``).
            load: model name -> loaded model; the real one builds a
                ``WhisperModel`` (tests inject a fake — §15).
            cached: Whether the model is already on disk (no download).
        """
        self.model = model
        self._load = load
        self._cached = cached

    def available(self) -> Availability:
        """Honest availability: the wheel imports and the model name resolves.

        An uncached model is still available — the first transcription
        downloads it — but the fact is noted so the report can surface the
        slow first run (§6).
        """
        try:
            import faster_whisper  # noqa: F401, PLC0415 — lazy availability probe (§14)
        except (ImportError, OSError) as e:
            # A broken native install parks jobs with the reason (§6); the
            # probe itself must never crash the run.
            return Availability(ok=False, reason=f"faster-whisper not importable: {scrub(str(e))}")
        if not _known_model(self.model):
            return Availability(
                ok=False,
                reason=(
                    f"unknown whisper model {self.model!r} — use a faster-whisper size "
                    "name or an HF repo id"
                ),
            )
        if not self._cached(self.model):
            return Availability(
                ok=True,
                reason=(
                    f"model {self.model!r} is not cached yet — the first transcription "
                    "downloads it (once per machine)"
                ),
            )
        return Availability(ok=True)

    def transcribe(self, audio: Path, initial_prompt: str) -> str:
        """Transcribe one audio file, primed with the item's vocabulary (§6).

        Args:
            audio: The audio file (any container PyAV decodes).
            initial_prompt: Known-vocabulary priming; empty disables it.

        Returns:
            The raw transcript text.

        Raises:
            ProviderUnavailableError: The model could not be loaded (a failed
                download, a broken cache) — the job stays waiting (§6).
            ProviderInputError: The audio could not be decoded or yielded no
                speech — the manual path (§5).
        """
        try:
            model = self._load(self.model)
        except (OSError, ValueError, RuntimeError) as e:
            raise ProviderUnavailableError(
                f"whisper-local could not load model {self.model!r}: {scrub(str(e))}"
            ) from e
        try:
            segments, _info = model.transcribe(str(audio), initial_prompt=initial_prompt or None)
            # faster-whisper decodes lazily — errors for corrupt audio
            # surface during iteration, so the join stays inside the try.
            text = "".join(segment.text for segment in segments).strip()
        except (OSError, ValueError, RuntimeError, EOFError) as e:
            # PyAV maps FFmpeg decode failures onto OSError/ValueError
            # subclasses: bad input, by contract (§5) — never an engine bug.
            raise ProviderInputError(
                f"whisper-local could not decode the audio: {scrub(str(e))}"
            ) from e
        if not text:
            raise ProviderInputError("whisper-local heard no speech in the audio")
        return text
