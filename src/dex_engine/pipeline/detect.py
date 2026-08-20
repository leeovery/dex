"""Detection (§1): ordered pattern match, HEAD content-type sniff when inconclusive.

Detection is authoritative for kind. URL patterns decide first, in registry
order — a match on any specialized driver is conclusive. A URL that only the
trailing catch-all claims is *inconclusive*: one HEAD request may reveal a
binary (a PDF served from an arbitrary URL) and reroute it to ``file`` work
with its :class:`~dex_engine.pipeline.types.Format`.

``normalize`` imports this same module for its pattern-only stamping (kinds
are provisional forever; the ledger is authoritative) — which is what kills
the historical ``kind_of`` duplication and its m.youtube.com divergence.

Local files (``file:<repo-path>`` work keys) are ``file`` work by
construction; their Format comes from :func:`sniff_format` — anydoc's
byte-signature detection when the wheel is present, a magic-number fallback
for the obvious signatures otherwise, the filename extension for
signature-less formats (CSV) and unsmudged LFS pointers.
"""

import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO

from .types import Format, Kind, SourceDriver

__all__ = [
    "CONTENT_TYPE_FORMATS",
    "Detection",
    "Sniff",
    "canonical_url",
    "detect",
    "detect_kind",
    "sniff_format",
]

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
    """
    if url.startswith("file:"):
        # Local-file work keys are file work by construction; the FORMAT is
        # sniffed from bytes where the file is readable (the seeding layer
        # and the file driver both call sniff_format — the driver's sniff
        # is the authoritative one, §1).
        return Detection(kind=Kind.FILE)
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


# ---------------------------------------------------------------------------
# Byte-signature format detection (§1): anydoc when present, magic-number
# fallback for the obvious ones, extension fallback for signature-less
# formats. Only formats in the Format enum are recognized — images, audio,
# and legacy binaries (.doc/.ppt) return None and never become file work.
# ---------------------------------------------------------------------------

_EXTENSION_FORMATS: dict[str, Format] = {fmt.value: fmt for fmt in Format}

# ZIP-container package identities: OOXML content directories and ODF/EPUB
# mimetype values.
_ZIP_DIR_FORMATS = (("word/", Format.DOCX), ("ppt/", Format.PPTX), ("xl/", Format.XLSX))
_ZIP_MIMETYPE_FORMATS = {
    "application/vnd.oasis.opendocument.text": Format.ODT,
    "application/vnd.oasis.opendocument.spreadsheet": Format.ODS,
    "application/vnd.oasis.opendocument.presentation": Format.ODP,
    "application/epub+zip": Format.EPUB,
}


def sniff_format(data: bytes, *, name: str | None = None) -> Format | None:
    """The Format of a byte blob, or None when it is not extractable work.

    anydoc's own detection decides when its wheel is importable; the
    magic-number fallback covers the obvious signatures without it. The
    filename extension is the last resort — it is what catches CSV (no
    signature exists) and unsmudged LFS pointers (text bytes, honest name).

    Args:
        data: The leading bytes (the whole file is fine).
        name: The filename, when one exists, for the extension fallback.

    Returns:
        The detected format, or None (unrecognized / not a Format).
    """
    detected = _anydoc_sniff(data) or _magic_sniff(data)
    if detected is not None:
        return detected
    if name is not None and "." in name:
        return _EXTENSION_FORMATS.get(name.rsplit(".", 1)[-1].lower())
    return None


def _anydoc_sniff(data: bytes) -> Format | None:
    try:
        import anydoc  # noqa: PLC0415 — lazy: heavy dep, and detection must work without it (§14)
    except (ImportError, OSError):
        return None  # the magic fallback below carries detection
    detected = anydoc.format_from_bytes(data)
    if detected is None:
        return None
    try:
        return Format(detected)
    except ValueError:
        return None  # anydoc knows formats (doc, ppt) the pipeline does not extract


def _magic_sniff(data: bytes) -> Format | None:
    if data.startswith(b"%PDF-"):
        return Format.PDF
    if data.startswith(b"{\\rtf"):
        return Format.RTF
    if data.startswith(b"PK\x03\x04"):
        return _zip_sniff(data)
    return None


def _zip_sniff(data: bytes) -> Format | None:
    """Resolve a ZIP container to its package identity (OOXML / ODF / EPUB)."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            if "mimetype" in names:
                mimetype = archive.read("mimetype").decode("ascii", "replace").strip()
                return _ZIP_MIMETYPE_FORMATS.get(mimetype)
            if "[Content_Types].xml" in names:
                for prefix, fmt in _ZIP_DIR_FORMATS:
                    if any(entry.startswith(prefix) for entry in names):
                        return fmt
    except (zipfile.BadZipFile, OSError, ValueError):
        return None  # truncated or lying container — not extractable work
    return None
