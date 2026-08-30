"""Corpus-item creation from capture files: ``enrich item new``.

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

``parse_capture`` is the canonical capture-file reader (``inbox.py``
imports it from here) and ``write_capture`` the engine's one writer of the
same format — the file every capture client produces, whatever the
transport that carries it.
"""

import datetime
import hashlib
import re
from collections.abc import Callable, Sequence
from pathlib import Path

from dex_engine import corpus

from .detect import canonical_url, detect_kind, sniff_format
from .types import Instance, Kind, SourceDriver

__all__ = ["URL_RE", "item_new", "parse_capture", "slugify", "write_capture"]

# The URL-in-prose pattern every corpus-item creator extracts with:
# `normalize` reads chat exports through the same regex, so one route in
# cannot see a URL the other misses.
URL_RE = re.compile(r"https?://[^\s>\)\]]+")
_STAMP_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-\d{6}$")
_STAMP_FORMAT = "%Y%m%d-%H%M%S"  # what _STAMP_RE reads back
_MEDIA_RE = re.compile(r"^media/([0-9a-f]{6})/[^/]+$")

# How far the stamp may walk forward looking for a free second. A capturer
# is unique per second by the format's own contract, so needing more than a
# minute of them means something other than a burst of captures.
_STAMP_ATTEMPTS = 60


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


def write_capture(instance: Instance, *, url: str, note: str, now: datetime.datetime) -> Path:
    """Write one capture into ``inbox/`` — the file every capture client writes.

    Body is the URL, the note, or both with a blank line between them, and
    nothing else: it becomes the corpus item's body verbatim, so anything
    added here is something the owner never said.

    Args:
        instance: The instance to capture into.
        url: The URL being captured, or ``""`` for a note-only capture.
        note: Why it was worth saving, or ``""`` for a bare link.
        now: The capture moment, stamped into the filename — which is where
            ``item_new`` reads the item's capture date back out of.

    Returns:
        The capture file's path.

    Raises:
        ValueError: Neither a URL nor a note was given, or a whole minute of
            stamps is already taken.
        OSError: ``inbox/`` or the file itself cannot be written.
    """
    body = "\n\n".join(part for part in (url.strip(), note.strip()) if part)
    if not body:
        raise ValueError("a capture is a URL, a note, or both — this one is neither")
    inbox = instance.inbox_dir
    inbox.mkdir(parents=True, exist_ok=True)
    for second in range(_STAMP_ATTEMPTS):
        stamp = now + datetime.timedelta(seconds=second)
        path = inbox / f"{stamp:{_STAMP_FORMAT}}.md"
        try:
            # Exclusive create, and a taken stamp walks forward a second
            # rather than growing a suffix: the filename IS the capture date
            # the processor reads back, and only a whole stamp parses.
            with path.open("x", encoding="utf-8") as file:
                file.write(body + "\n")
        except FileExistsError:
            continue
        return path
    raise ValueError(
        f"{inbox} already holds a capture for every second from "
        f"{now:{_STAMP_FORMAT}} onwards — this one was not written"
    )


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
    note = URL_RE.sub("", body).strip()
    if note:
        return note[:80]
    if urls:
        return urls[0].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return ""


def slugify(text: str, max_len: int = 40) -> str:
    """A filename slug: lowercase, hyphenated, bounded, never empty.

    The item-id slug rule, shared with ``normalize``: item ids are read
    back by shape (the scrubber redacts them, lint checks them), so every
    creator must emit the one grammar.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def _media_kind(instance: Instance, media_path: str) -> str:
    """``file`` for document formats (extract queue), ``image`` otherwise."""
    file_path = instance.root / media_path
    data = file_path.read_bytes() if file_path.is_file() else b""
    fmt = sniff_format(data, name=media_path.rsplit("/", 1)[-1])
    return Kind.FILE.value if fmt is not None else Kind.IMAGE.value


def item_new(  # noqa: PLR0913 — every input is injected, none ambient
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
            authoritative).
        today: Injected clock — the date when the filename carries no
            capture timestamp.
        slug: Judgment's slug for the id; ``None`` derives one mechanically
            from the note (then the URL path tail).
        shared_by: The owner's display name for provenance.

    Returns:
        A one-line confirmation naming the created file.

    Raises:
        ValueError: The capture is unreadable, still carries a staged
            asset (``dex inbox`` has not run), its media pointer is not a
            ``dex inbox`` path, or the item already exists.
        OSError: The capture file cannot be read.
    """
    frontmatter, body = parse_capture(capture_path.read_text(encoding="utf-8"))
    # Punctuation strips BEFORE dedupe: "…/post." and "…/post" in one note
    # are the same URL and must land in the frontmatter once.
    urls = list(dict.fromkeys(url.rstrip(".,") for url in URL_RE.findall(body)))
    media = frontmatter.get("media")
    staged = [key for key in ("asset", "name") if key in frontmatter]
    if media is None and staged:
        # The binary is still on the staging release: creating a text item
        # here would drop the reference silently and lose the provenance.
        raise ValueError(
            f"{capture_path.name}: capture still carries a staged asset "
            f"({', '.join(staged)}) — run `dex inbox` first (it downloads the "
            "asset, files it under media/<id>/<name>, and rewrites the pointer)"
        )
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
    item_id = f"{date.isoformat()}-{slugify(slug or _slug_source(body, urls))}-{shortid}"
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
