"""The anydoc extractor: firecrawl-anydoc, the default for all ten formats.

Embedded assets come back as bytes — structurally, not as an anydoc quirk:
embedded images live inside the container and have no URL. PDF is the one
format whose conversion produces markdown directly with no document-model
form, so PDF extraction degrades to text-only (the contract's sanctioned
graceful degradation). Scanned/image-only documents raise
:class:`~dex_engine.pipeline.classify.ScannedDocumentError` — the OCR
path — never an empty extraction.

anydoc is lazy-imported inside methods: heavy native dep, loaded only
when a document actually extracts.
"""

from dex_engine.pipeline.classify import ProviderInputError, ScannedDocumentError, scrub
from dex_engine.pipeline.types import Asset, Availability, Extraction, Format

__all__ = ["AnydocExtractor"]

# anydoc's own marker for a PDF with no extractable text — the one
# ConvertError that means "needs OCR", not "bad input".
_SCANNED_MARKER = "OCR is required"

_ASSET_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/tiff": "tiff",
    "image/bmp": "bmp",
}


class AnydocExtractor:
    """Extract markdown + embedded assets from any of the ten formats."""

    name: str = "anydoc"

    def supports(self, fmt: Format) -> bool:  # noqa: ARG002 — total by design
        """True for every Format — anydoc is the all-formats default."""
        return True

    def available(self) -> Availability:
        """Honest availability: the anydoc wheel imports on this machine."""
        try:
            import anydoc  # noqa: F401, PLC0415 — lazy availability probe
        except (ImportError, OSError) as e:
            # A missing or broken native wheel parks jobs with the reason
            # — it must never crash the availability check itself.
            return Availability(
                ok=False, reason=f"firecrawl-anydoc not importable: {scrub(str(e))}"
            )
        return Availability(ok=True)

    def extract(self, data: bytes, fmt: Format) -> Extraction:
        """Convert one document to markdown, embedded assets as bytes.

        Args:
            data: The document bytes.
            fmt: The document's format (anydoc's format vocabulary matches
                the Format enum values one-to-one).

        Returns:
            The extraction — markdown plus embedded assets.

        Raises:
            ScannedDocumentError: No extractable text — the OCR path.
            ProviderInputError: anydoc could not parse the document
                (encrypted, malformed, truncated) — the manual path.
        """
        import anydoc  # noqa: PLC0415 — lazy: heavy dep, loaded only when extracting

        try:
            markdown = anydoc.to_markdown_bytes(data, format=fmt.value)
        except anydoc.UnsupportedError as e:
            if _SCANNED_MARKER in str(e):
                raise ScannedDocumentError(scrub(str(e))) from e
            raise ProviderInputError(f"anydoc cannot extract this document: {scrub(str(e))}") from e
        except anydoc.ConvertError as e:
            raise ProviderInputError(f"anydoc could not parse the document: {scrub(str(e))}") from e
        if not markdown.strip():
            raise ScannedDocumentError(
                "extraction produced no text — image-only document, OCR is required"
            )
        return Extraction(markdown=markdown, assets=self._assets(data, fmt))

    @staticmethod
    def _assets(data: bytes, fmt: Format) -> list[Asset]:
        """Embedded assets via the document model; text-only degradation on any miss.

        PDF has no document-model form (anydoc converts it straight to
        markdown), so PDF extractions carry no assets — sanctioned by the contract:
        an extractor either populates assets or returns none.
        """
        import anydoc  # noqa: PLC0415 — lazy

        if fmt is Format.PDF:
            return []
        try:
            document = anydoc.to_document(data, format=fmt.value)
        except anydoc.ConvertError:
            return []  # the markdown already extracted; assets degrade gracefully
        return [
            Asset(data=asset.data, suggested_ext=_ASSET_EXTENSIONS.get(asset.media_type, "bin"))
            for asset in document.assets
        ]
