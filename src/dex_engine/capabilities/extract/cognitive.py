"""The cognitive extraction floor (§6): parks work for the ingest session.

The cognitive provider is always last in resolution order and always
available — but ``enrich run`` never drains it: a job that resolves here is
``waiting`` from the queue's point of view and work-to-do from the
session's. It surfaces on the run report; the session extracts with eyes
and appends the ``done`` entry through ``enrich mark``.
"""

from dex_engine.pipeline.types import Availability, Extraction, Format

__all__ = ["COGNITIVE_MECHANICAL_MESSAGE", "CognitiveExtractor"]

COGNITIVE_MECHANICAL_MESSAGE = (
    "cognitive capability work is session work — the run report lists it and the "
    "session does it with eyes; it is never drained mechanically (§6)"
)


class CognitiveExtractor:
    """The always-available floor: no format ever hard-fails an instance."""

    name: str = "cognitive"

    def supports(self, fmt: Format) -> bool:  # noqa: ARG002 — the floor is total by design
        """True for every format — the floor holds unconditionally (§6)."""
        return True

    def available(self) -> Availability:
        """Always available — a session is always present in this system (§12)."""
        return Availability(ok=True, reason="the ingest session does this with eyes")

    def extract(self, data: bytes, fmt: Format) -> Extraction:  # noqa: ARG002 — the floor never reads its inputs
        """Never called mechanically — calling it is an engine bug.

        Raises:
            RuntimeError: Always — see :data:`COGNITIVE_MECHANICAL_MESSAGE`.
        """
        raise RuntimeError(COGNITIVE_MECHANICAL_MESSAGE)
