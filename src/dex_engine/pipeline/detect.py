"""Detection (§1): ordered pattern match, HEAD content-type sniff when inconclusive.

Detection is authoritative for kind. URL patterns decide first, in registry
order — a match on any specialized driver is conclusive. A URL that only the
trailing catch-all claims is *inconclusive*: one HEAD request may reveal a
binary (a PDF served from an arbitrary URL) and reroute it to ``file`` work
with its :class:`~dex_engine.pipeline.types.Format`.

``normalize`` imports this same module for its pattern-only stamping (kinds
are provisional forever; the ledger is authoritative) — which is what kills
the historical ``kind_of`` duplication and its m.youtube.com divergence.

Local files (``file:<repo-path>`` work keys) are byte-signature sniffed by
the file driver — phase 3; the seam here raises loudly until it lands.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .types import Format, Kind, SourceDriver

__all__ = ["CONTENT_TYPE_FORMATS", "Detection", "Sniff", "canonical_url", "detect", "detect_kind"]

# url -> media type ("application/pdf"), or None when the HEAD itself failed.
# A failed sniff is INCONCLUSIVE, never classified: the GET that follows
# will surface the real status through the classifier.
Sniff = Callable[[str], str | None]

CONTENT_TYPE_FORMATS: dict[str, Format] = {
    "application/pdf": Format.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": Format.DOCX,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": Format.PPTX,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": Format.XLSX,
    "application/vnd.oasis.opendocument.text": Format.ODT,
    "application/vnd.oasis.opendocument.spreadsheet": Format.ODS,
    "application/vnd.oasis.opendocument.presentation": Format.ODP,
    "application/rtf": Format.RTF,
    "text/rtf": Format.RTF,
    "application/epub+zip": Format.EPUB,
    "text/csv": Format.CSV,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class Detection:
    """The detected kind, plus format for file work."""

    kind: Kind
    format: Format | None = None


def detect_kind(url: str, drivers: Sequence[SourceDriver]) -> Kind:
    """Pattern-only detection: first matching driver in registry order wins.

    This is what ``normalize`` stamps — offline and fast, no network. The
    result is provisional forever; the ledger is authoritative (§1).

    Args:
        url: The URL to detect.
        drivers: The ordered registry (web last, the catch-all).

    Returns:
        The matched driver's kind.

    Raises:
        LookupError: No driver matched — impossible with a well-formed
            registry (the catch-all matches everything); loud if it isn't.
    """
    return _match(url, drivers).kind


def canonical_url(url: str, drivers: Sequence[SourceDriver]) -> str:
    """Delegate canonicalization to the pattern-matched driver (§2).

    The result keys the ledger hash. Deliberately pattern-based: sniffing
    may reroute a URL's *kind* to ``file``, but never its key — the
    catch-all's canonical is the base canonicalization either way.
    """
    return _match(url, drivers).canonical(url)


def detect(url: str, drivers: Sequence[SourceDriver], *, sniff: Sniff | None = None) -> Detection:
    """Full detection: patterns first; one HEAD sniff only when inconclusive.

    A specialized pattern match is conclusive — no request is spent on it.
    Only a URL that fell through to the catch-all is sniffed, and only when
    a ``sniff`` seam is provided; a recognized binary media type reroutes
    the URL to ``file`` work with its format.

    Args:
        url: The URL to detect.
        drivers: The ordered registry (web last, the catch-all).
        sniff: The HEAD seam; ``None`` disables sniffing (pattern-only).

    Returns:
        The detection — kind, plus format for file work.

    Raises:
        NotImplementedError: ``url`` is a local-file work key; byte-signature
            detection arrives with the file driver (phase 3).
    """
    if url.startswith("file:"):
        raise NotImplementedError(
            "local-file byte-signature detection arrives with the file driver (phase 3)"
        )
    matched = _match(url, drivers)
    inconclusive = matched is drivers[-1]  # only the catch-all claimed it
    if inconclusive and sniff is not None:
        media_type = sniff(url)
        if media_type is not None:
            fmt = CONTENT_TYPE_FORMATS.get(media_type.split(";")[0].strip().lower())
            if fmt is not None:
                return Detection(kind=Kind.FILE, format=fmt)
    return Detection(kind=matched.kind)


def _match(url: str, drivers: Sequence[SourceDriver]) -> SourceDriver:
    for driver in drivers:
        if driver.matches(url):
            return driver
    raise LookupError(f"no driver matched {url!r} — the registry must end with the catch-all (§2)")
