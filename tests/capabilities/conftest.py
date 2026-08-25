"""Shared capability-test plumbing: fake providers, fixture documents."""

from pathlib import Path

from dex_engine.pipeline.types import Availability, Extraction, Format

FILE_FIXTURES = Path(__file__).parent.parent / "fixtures" / "files"


def fixture_bytes(name: str) -> bytes:
    return (FILE_FIXTURES / name).read_bytes()


class FakeTranscriber:
    """A scriptable Transcriber: canned text, or an exception to raise."""

    def __init__(  # noqa: PLR0913 — a scriptable fake mirrors its protocol's knobs
        self,
        name: str = "fake-transcriber",
        *,
        text: str = "the fake transcript",
        ok: bool = True,
        reason: str = "",
        model: str = "fake-model",
        raise_: Exception | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.text = text
        self.ok = ok
        self.reason = reason
        self.raise_ = raise_
        self.calls: list[tuple[Path, str]] = []

    def available(self) -> Availability:
        return Availability(ok=self.ok, reason=self.reason)

    def transcribe(self, audio: Path, initial_prompt: str) -> str:
        self.calls.append((audio, initial_prompt))
        if self.raise_ is not None:
            raise self.raise_
        return self.text


class FakeExtractor:
    """A scriptable Extractor keyed to a format set."""

    def __init__(  # noqa: PLR0913 — a scriptable fake mirrors its protocol's knobs
        self,
        name: str = "fake-extractor",
        *,
        formats: frozenset[Format] = frozenset(Format),
        ok: bool = True,
        reason: str = "",
        markdown: str = "extracted markdown body",
        raise_: Exception | None = None,
    ) -> None:
        self.name = name
        self.formats = formats
        self.ok = ok
        self.reason = reason
        self.markdown = markdown
        self.raise_ = raise_
        self.calls: list[tuple[bytes, Format]] = []

    def supports(self, fmt: Format) -> bool:
        return fmt in self.formats

    def available(self) -> Availability:
        return Availability(ok=self.ok, reason=self.reason)

    def extract(self, data: bytes, fmt: Format) -> Extraction:
        self.calls.append((data, fmt))
        if self.raise_ is not None:
            raise self.raise_
        return Extraction(markdown=self.markdown)
