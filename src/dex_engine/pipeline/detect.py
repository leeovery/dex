"""Detection: ordered pattern match, HEAD content-type sniff when inconclusive.

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

import codecs
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
    "looks_like_html",
    "sniff_document",
    "sniff_format",
    "sniff_media_ext",
]

# HTML leads, BOM- and whitespace-tolerant. Bytes decide what a body IS,
# never the content type a server claimed: shared by the file driver's
# re-route to web work and the enclosure download's error-page guard.
_HTML_LEADS = (b"<!doctype", b"<html", b"<head", b"<body")

# Enough to carry an XML declaration, a comment and a DOCTYPE ahead of an
# SVG's root element, and short enough that a 10MB body is never copied.
_LEAD_BYTES = 512


def _text_lead(data: bytes) -> bytes:
    """The body's opening markup token: BOM- and whitespace-free, lowercased."""
    return data[:_LEAD_BYTES].removeprefix(b"\xef\xbb\xbf").lstrip().lower()


def looks_like_html(data: bytes) -> bool:
    """Whether the leading bytes read as an HTML/XML document.

    Args:
        data: The leading bytes (the whole body is fine).

    Returns:
        True for a markup lead.
    """
    return _text_lead(data).startswith(_HTML_LEADS)


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
    result is provisional forever; the ledger is authoritative.

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
    """Delegate canonicalization to the pattern-matched driver.

    The result keys the ledger hash. Deliberately pattern-based: sniffing
    may reroute a URL's *kind* to ``file``, but never its key — the
    catch-all's canonical is the base canonicalization either way.

    **A refusal is one type, decided here.** A driver's ``canonical`` is
    arbitrary code over data an owner wrote into frontmatter, and the two
    callers that key work off it read that differently: seeding guarded
    this call with ``except ValueError`` and let anything else abort the
    run, while the ownership map caught everything and quietly keyed the
    unit on the raw URL's hash — one URL crashing a run on one path and
    resolving fine on the other, which is the same URL having two
    identities. So the refusal is typed at the one place the driver is
    called: whatever a driver raises leaves here as a ``ValueError`` naming
    the original, and every caller's existing narrow guard then does its
    own right thing — seeding parks the bad seed keyed on the raw URL,
    which is exactly the identity the ownership fallback resolves to.

    Args:
        url: The URL to canonicalize.
        drivers: The ordered registry (web last, the catch-all).

    Returns:
        The canonical form that keys this URL's work unit.

    Raises:
        ValueError: The driver refused the URL, or its ``canonical`` raised
            anything at all.
    """
    driver = _match(url, drivers)
    try:
        return driver.canonical(url)
    except ValueError:
        raise
    except Exception as e:
        # A driver's canonical is arbitrary code over owner data: the catch
        # is broad because the vocabulary is, and nothing is swallowed —
        # the original travels on as the cause.
        raise ValueError(
            f"{driver.kind} canonicalization failed "
            f"({type(e).__name__}: {' '.join(str(e).split())})"
        ) from e


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
        # is the authoritative one).
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
    raise LookupError(f"no driver matched {url!r} — the registry must end with the catch-all")


# ---------------------------------------------------------------------------
# Byte-signature format detection: anydoc when present, magic-number
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
        import anydoc  # noqa: PLC0415 — lazy: heavy dep, and detection must work without it
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


# ---------------------------------------------------------------------------
# Media byte signatures: what a downloaded body IS, against what its URL and
# its server claimed. CDNs content-negotiate — a `.jpg` URL answers with PNG,
# AVIF or HEIF bytes — and a site's catch-all route answers an image URL with
# its own HTML shell under a 200.
# ---------------------------------------------------------------------------

_MEDIA_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"ID3", "mp3"),
)

_EBML_MAGIC = b"\x1a\x45\xdf\xa3"

# ISO-BMFF major brands: one container holds stills and video alike, and only
# the brand separates them. An unlisted brand is unrecognized, never guessed —
# the URL's own extension is the better answer than a wrong container name.
_FTYP_EXTENSIONS: dict[bytes, str] = {
    b"avif": "avif",
    b"avis": "avif",
    b"heic": "heic",
    b"heix": "heic",
    b"hevc": "heic",
    b"hevx": "heic",
    b"heim": "heif",
    b"heis": "heif",
    b"mif1": "heif",
    b"msf1": "heif",
    b"qt  ": "mov",
    b"avc1": "mp4",
    b"dash": "mp4",
    b"iso2": "mp4",
    b"isom": "mp4",
    b"m4v ": "mp4",
    b"mmp4": "mp4",
    b"mp41": "mp4",
    b"mp42": "mp4",
}

# What may stand between the start of an SVG document and its root element.
_SVG_PRELUDES = ((b"<?", b"?>"), (b"<!--", b"-->"), (b"<!doctype", b">"))


def sniff_media_ext(data: bytes) -> str | None:
    """The canonical extension for a media body, or None when unrecognized.

    Args:
        data: The leading bytes (the whole body is fine).

    Returns:
        The extension the bytes name, undotted, or None when they carry no
        media signature this knows.
    """
    if _svg_lead(_text_lead(data)):
        return "svg"  # the one media that is text
    return _binary_media_ext(data)


def _binary_media_ext(data: bytes) -> str | None:
    for magic, ext in _MEDIA_MAGIC:
        if data.startswith(magic):
            return ext
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp":
        return _FTYP_EXTENSIONS.get(data[8:12].lower())
    if data.startswith(_EBML_MAGIC):
        # The DocType naming which of the two containers this is sits in the
        # EBML header, ahead of everything else in the file.
        return "webm" if b"webm" in data[:_LEAD_BYTES] else "mkv"
    # An MPEG audio frame opens with 11 set sync bits — 0xFF, then a byte of
    # 0xE0 or above, which across two bytes is exactly this range.
    if b"\xff\xe0" <= data[:2] <= b"\xff\xff":
        return "mp3"
    return None


def sniff_document(data: bytes) -> str | None:
    """The document shape a body reads as — HTML, XML or JSON — or None.

    A media URL answering with one of these answered with a page, not a
    picture: an SPA catch-all route and an API error body both arrive under
    a 200. SVG is the one markup body that IS media, and never a document
    here.

    Args:
        data: The leading bytes (the whole body is fine).

    Returns:
        The shape's name, or None for anything else — media, unknown
        binary, plain text.
    """
    lead = _text_lead(data)
    if _svg_lead(lead):
        return None
    if lead.startswith(_HTML_LEADS):
        return "HTML"
    if lead.startswith(b"<?xml"):
        return "XML"
    if lead.startswith((b"{", b"[")) and _is_utf8(lead):
        return "JSON"
    return None


def _svg_lead(lead: bytes) -> bool:
    """Whether a text lead opens an SVG document, its preludes skipped."""
    while not lead.startswith(b"<svg"):
        for opener, closer in _SVG_PRELUDES:
            if lead.startswith(opener):
                _, found, lead = lead.partition(closer)
                if not found:
                    return False  # truncated or unterminated — not a root element
                lead = lead.lstrip()
                break
        else:
            return False
    return True


def _is_utf8(lead: bytes) -> bool:
    """Whether a lead is UTF-8 text, tolerating a truncated final character."""
    try:
        codecs.getincrementaldecoder("utf-8")().decode(lead)
    except UnicodeDecodeError:
        return False
    return True
