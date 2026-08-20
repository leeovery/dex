"""The cognitive OCR floor (§6): Claude vision at ingest, the only OCR provider.

Engine OCR providers may come later; the interface is already there — OCR is
extract-shaped (document bytes in, markdown out), so providers implement the
same :class:`~dex_engine.pipeline.types.Extractor` protocol and slot into
the same per-format resolution when they exist.
"""

from dex_engine.capabilities.extract.cognitive import COGNITIVE_MECHANICAL_MESSAGE
from dex_engine.pipeline.types import Availability, Extraction, Format

__all__ = ["CognitiveOcr"]


class CognitiveOcr:
    """The OCR floor: scanned pages are read with eyes at ingest."""

    name: str = "cognitive"

    def supports(self, fmt: Format) -> bool:  # noqa: ARG002 — the floor is total by design
        """True for every format — the floor holds unconditionally (§6)."""
        return True

    def available(self) -> Availability:
        """Always available — a session is always present in this system (§12)."""
        return Availability(ok=True, reason="the ingest session reads scanned pages with eyes")

    def extract(self, data: bytes, fmt: Format) -> Extraction:  # noqa: ARG002 — the floor never reads its inputs
        """Never called mechanically — calling it is an engine bug.

        Raises:
            RuntimeError: Always — cognitive OCR is session work (§6).
        """
        raise RuntimeError(COGNITIVE_MECHANICAL_MESSAGE)
