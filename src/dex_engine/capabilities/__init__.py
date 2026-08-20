"""Capability registries: per-need provider resolution, cognitive last.

Resolution is **per format / per need, first available *mechanical*
provider wins**, order set by instance config (``providers`` in
``state/config.json``); the cognitive provider is always last and is the
always-available floor for judgment-shaped work — but ``enrich run`` never
drains it. Precisely: **``waiting`` means no mechanical provider**; a job
that resolves to the cognitive floor is ``waiting`` from the queue's point
of view and work-to-do from the session's. No instance ever hard-fails on a
missing key or an unsupported format.

The typed provider tuples on :class:`Capabilities` are the Protocol-
conformance point for providers, exactly as the driver registry literal is
for drivers.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeVar, assert_never

from dex_engine.pipeline.types import Availability, Config, Extractor, Format, Need, Transcriber

from .extract.anydoc import AnydocExtractor
from .extract.cognitive import CognitiveExtractor
from .extract.csv_builtin import CsvBuiltinExtractor
from .ocr.cognitive import CognitiveOcr

__all__ = ["COGNITIVE", "DEFAULT_PROVIDER_ORDER", "Capabilities"]

COGNITIVE = "cognitive"

# Mechanical provider order per capability, before instance-config
# reordering. anydoc first: the all-formats default; csv-builtin is CSV's
# independent fallback. OCR has no mechanical providers yet.
DEFAULT_PROVIDER_ORDER: dict[Need, tuple[str, ...]] = {
    Need.TRANSCRIBE: ("whisper-local", "whisper-api"),
    Need.EXTRACT: ("anydoc", "csv-builtin"),
    Need.OCR: (),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Capabilities:
    """The resolved provider registries — mechanical lists plus the floors.

    The mechanical tuples are resolution order; the floors are structurally
    separate so nothing can ever "drain" them by iteration accident.
    """

    transcribers: tuple[Transcriber, ...]
    extractors: tuple[Extractor, ...]
    ocr_providers: tuple[Extractor, ...] = ()
    extract_floor: Extractor = field(default_factory=CognitiveExtractor)
    ocr_floor: Extractor = field(default_factory=CognitiveOcr)

    @classmethod
    def build(cls, config: Config, *, model: str | None = None) -> "Capabilities":
        """Build the registries from instance config.

        Args:
            config: The instance config — provider order, transcribe model,
                whisper-api credentials.
            model: Per-call ``--model`` override; applies to whichever
                transcriber wins resolution.

        Returns:
            The capabilities.

        Raises:
            ValueError: Config names an unknown provider, or tries to place
                ``cognitive`` (its position is not configurable — always
                last).
        """
        # Deferred so building a Capabilities object stays cheap on cold
        # paths that never transcribe.
        from .transcribe.whisper_api import WhisperApi  # noqa: PLC0415
        from .transcribe.whisper_local import WhisperLocal  # noqa: PLC0415

        transcribe_model = model or config.transcribe_model
        # The API-side model name has its own config key — local size
        # names never leak into it; per-call --model overrides both.
        api_model = model or config.transcribe_api_model
        transcribers: dict[str, Transcriber] = {
            "whisper-local": WhisperLocal(model=transcribe_model),
            "whisper-api": WhisperApi(
                base_url=config.transcribe_base_url,
                api_key=config.transcribe_api_key,
                **({"model": api_model} if api_model else {}),
            ),
        }
        extractors: dict[str, Extractor] = {
            "anydoc": AnydocExtractor(),
            "csv-builtin": CsvBuiltinExtractor(),
        }
        return cls(
            transcribers=_ordered(Need.TRANSCRIBE, transcribers, config),
            extractors=_ordered(Need.EXTRACT, extractors, config),
            ocr_providers=_ordered(Need.OCR, {}, config),
        )

    # -- resolution: first available mechanical provider wins --------

    def transcriber(self) -> Transcriber | None:
        """The winning transcriber, or None (transcription has no floor)."""
        for provider in self.transcribers:
            if provider.available().ok:
                return provider
        return None

    def extractor(self, fmt: Format) -> Extractor | None:
        """The winning mechanical extractor for ``fmt``, or None (→ the floor)."""
        for provider in self.extractors:
            if provider.supports(fmt) and provider.available().ok:
                return provider
        return None

    def available(self, need: Need, fmt: Format | None = None) -> Availability:
        """The drain seam: is a *mechanical* provider available for this job?

        ``ok=False`` carries the stated parking reason — per provider where
        providers exist, the cognitive-floor truth where they don't.

        Args:
            need: The capability.
            fmt: The format, for extract work (None checks any format).

        Returns:
            The availability.
        """
        match need:
            case Need.TRANSCRIBE:
                return _first_available(self.transcribers)
            case Need.EXTRACT:
                eligible = tuple(p for p in self.extractors if fmt is None or p.supports(fmt))
                if not eligible:
                    what = fmt.value if fmt is not None else "this work"
                    return Availability(
                        ok=False,
                        reason=(
                            f"no mechanical extractor supports {what} — "
                            "the session extracts with eyes (cognitive floor)"
                        ),
                    )
                return _first_available(eligible)
            case Need.OCR:
                if self.ocr_providers:
                    return _first_available(self.ocr_providers)
                return Availability(
                    ok=False,
                    reason=(
                        "no mechanical OCR provider — the session reads scanned "
                        "pages with eyes (cognitive floor)"
                    ),
                )
            case _:
                assert_never(need)

    def is_cognitive(self, need: Need, fmt: Format | None = None) -> bool:
        """Whether this job resolves to the cognitive floor.

        Such jobs surface on the run report for the session; a waiting
        ``transcribe`` job never does — transcription has no cognitive
        floor, it waits for a mechanical provider.
        """
        match need:
            case Need.TRANSCRIBE:
                return False
            case Need.EXTRACT | Need.OCR:
                return not self.available(need, fmt).ok
            case _:
                assert_never(need)

    # -- the capability report surface --------------------------------

    def report_payload(self) -> dict[str, object]:
        """The ``capability-report`` payload: active provider, dormant upgrades."""
        return {
            "capabilities": [
                _capability_row(Need.TRANSCRIBE, self.transcribers, floor=None),
                _capability_row(Need.EXTRACT, self.extractors, floor=self.extract_floor),
                _capability_row(Need.OCR, self.ocr_providers, floor=self.ocr_floor),
            ]
        }


class _Provider(Protocol):
    """What resolution needs from any provider: a name and honest availability."""

    name: str

    def available(self) -> Availability: ...


_P = TypeVar("_P")


def _ordered(need: Need, providers: dict[str, _P], config: Config) -> tuple[_P, ...]:
    """Config-ordered provider list: named ones first, unlisted defaults appended."""
    configured = config.providers.get(need.value, [])
    if COGNITIVE in configured:
        raise ValueError(
            f"providers.{need.value}: 'cognitive' is not configurable — the cognitive "
            "floor is always last in resolution order"
        )
    unknown = sorted(set(configured) - set(providers))
    if unknown:
        known = ", ".join(sorted(providers)) or "none"
        raise ValueError(f"providers.{need.value}: unknown provider(s) {unknown} — known: {known}")
    defaults = [name for name in DEFAULT_PROVIDER_ORDER[need] if name not in configured]
    return tuple(providers[name] for name in (*configured, *defaults))


def _first_available(providers: Sequence[_Provider]) -> Availability:
    reasons = []
    for provider in providers:
        availability = provider.available()
        if availability.ok:
            return availability
        reasons.append(f"{provider.name}: {availability.reason}")
    return Availability(ok=False, reason="; ".join(reasons) or "no mechanical providers configured")


def _capability_row(
    need: Need,
    mechanical: Sequence[_Provider],
    *,
    floor: Extractor | None,
) -> dict[str, object]:
    """One capability's providers: the winner active, the rest with their needs."""
    providers: list[dict[str, str]] = []
    active_seen = False
    for provider in mechanical:
        availability = provider.available()
        if availability.ok and not active_seen:
            providers.append({"name": provider.name, "state": "active"})
            active_seen = True
        elif availability.ok:
            providers.append({"name": provider.name, "state": "available"})
        else:
            providers.append(
                {"name": provider.name, "state": "available", "note": availability.reason}
            )
    if floor is not None:
        # The floor is active only when nothing mechanical outranks it.
        state = "available" if active_seen else "active"
        row = {"name": floor.name, "state": state}
        if note := floor.available().reason:
            row["note"] = note
        providers.append(row)
    return {"name": need.value, "providers": providers}
