"""Corpus-item creation from capture files: ``enrich item new`` (§14).

Corpus items are created by this verb, never freehand — code writes
frontmatter, Claude writes prose. Creation was always mechanical work (the
only judgment is the scope check before it): the id follows the id rules,
provenance is stamped from the capture, and the body is the capture's note
verbatim. Claude's interpretive context belongs in the digest, never the
item body.

Both id paths:

- **url-hash**: link/text captures — ``sha1("manual/<canonical-url>")[:6]``,
  falling back to ``sha1("manual/<capture-filename>")[:6]`` for note-only
  captures;
- **materialized-media directory**: the item id was fixed when
  ``dex inbox`` filed the binary at ``media/<id>/<name>`` — the directory
  name IS the shortid.

``parse_capture`` is the canonical capture-file reader; ``inbox.py``
imports it from here.
"""

import datetime
import hashlib
import re
from collections.abc import Callable, Sequence
from pathlib import Path

from dex_engine import corpus

from .detect import canonical_url, detect_kind, sniff_format
from .types import Instance, Kind, SourceDriver

__all__ = ["item_new", "parse_capture"]

_URL_RE = re.compile(r"https?://[^\s>\)\]]+")
_STAMP_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-\d{6}$")
_MEDIA_RE = re.compile(r"^media/([0-9a-f]{6})/[^/]+$")


def parse_capture(text: str) -> tuple[dict[str, str], str]:
    """Split a capture file into (frontmatter, body), tolerantly.

    Phone clients produce BOMs and CRLF line endings; both are normalized.
    A capture without frontmatter is all body.

    Args:
        text: The capture file content.

    Returns:
        The frontmatter mapping and the body.
    """
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        return {}, text
    frontmatter: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, _, value = line.partition(":")
            frontmatter[key.strip()] = value.strip()
    return frontmatter, "\n".join(lines[end + 1 :]).lstrip("\n")


def _capture_date(name: str, today: Callable[[], datetime.date]) -> datetime.date:
    """The capture timestamp from the filename (``yyyyMMdd-HHmmss.md``), else today."""
    match = _STAMP_RE.match(Path(name).stem)
    if match is None:
        return today()
    try:
        return datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return today()


def _slug_source(body: str, urls: list[str]) -> str:
    """Mechanical slug derivation: note text first, then the URL's path tail."""
    note = _URL_RE.sub("", body).strip()
    if note:
        return note[:80]
    if urls:
        return urls[0].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return ""


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def _media_kind(instance: Instance, media_path: str) -> str:
    """``file`` for document formats (extract queue), ``image`` otherwise (§3)."""
    file_path = instance.root / media_path
    data = file_path.read_bytes() if file_path.is_file() else b""
    fmt = sniff_format(data, name=media_path.rsplit("/", 1)[-1])
    return Kind.FILE.value if fmt is not None else Kind.IMAGE.value


def item_new(  # noqa: PLR0913 — every input is injected, none ambient (§14)
    capture_path: Path,
    *,
    instance: Instance,
    drivers: Sequence[SourceDriver],
    today: Callable[[], datetime.date],
    slug: str | None = None,
    shared_by: str = "owner",
) -> str:
    """Create the corpus item for one capture file.

    Args:
        capture_path: The capture file (in ``inbox/``, media already
            materialized by ``dex inbox``).
        instance: The instance.
        drivers: The driver registry — URL canonicalization and pattern-only
            kind detection (provisional forever; the ledger is
            authoritative, §1).
        today: Injected clock (§14) — the date when the filename carries no
            capture timestamp.
        slug: Judgment's slug for the id; ``None`` derives one mechanically
            from the note (then the URL path tail).
        shared_by: The owner's display name for provenance.

    Returns:
        A one-line confirmation naming the created file.

    Raises:
        ValueError: The capture is unreadable, its media pointer is not a
            ``dex inbox`` path, or the item already exists.
        OSError: The capture file cannot be read.
    """
    frontmatter, body = parse_capture(capture_path.read_text(encoding="utf-8"))
    urls = [url.rstrip(".,") for url in dict.fromkeys(_URL_RE.findall(body))]
    media = frontmatter.get("media")
    kinds: list[str]
    media_list: list[str] = []
    if media is not None:
        match = _MEDIA_RE.match(media)
        if match is None:
            raise ValueError(
                f"{capture_path.name}: media pointer {media!r} is not a "
                "media/<id>/<name> path — run `dex inbox` first (it materializes "
                "staged assets and rewrites the pointer)"
            )
        shortid = match.group(1)  # the id was fixed at materialization
        media_list = [media]
        kinds = sorted({_media_kind(instance, media), *map(_url_kind(drivers), urls)})
    elif urls:
        canonical = canonical_url(urls[0], drivers)
        shortid = _shortid(f"manual/{canonical}")
        kinds = sorted({_url_kind(drivers)(url) for url in urls})
    else:
        shortid = _shortid(f"manual/{capture_path.name}")
        kinds = [Kind.TEXT.value]

    date = _capture_date(capture_path.name, today)
    item_id = f"{date.isoformat()}-{_slugify(slug or _slug_source(body, urls))}-{shortid}"
    target = instance.corpus_dir / f"{date:%Y}" / f"{item_id}.md"
    if target.exists():
        raise ValueError(
            f"{target} already exists — captures are processed once; if this capture "
            "was already ingested, delete the capture file instead"
        )
    item = corpus.CorpusItem(
        id=item_id,
        source="inbox",
        channel="inbox",
        shared_by=shared_by,
        date=date,
        urls=urls,
        kinds=kinds,
        media=media_list,
        body=body if body.endswith("\n") or not body else body + "\n",
    )
    corpus.write_item(target, item)
    rel = target.relative_to(instance.root)
    if urls or Kind.FILE.value in kinds:
        return f"created {rel} — `dex enrich run` fetches its sources"
    return (
        f"created {rel} — no fetchable sources; describe/digest it in this session "
        "(the run report lists it as a no-source item until digested)"
    )


def _url_kind(drivers: Sequence[SourceDriver]) -> Callable[[str], str]:
    def kind(url: str) -> str:
        return detect_kind(url, drivers).value

    return kind


def _shortid(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]  # noqa: S324 — content key, not a security context
