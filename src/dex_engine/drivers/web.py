"""The web driver: registry catch-all, urllib fetch + trafilatura extraction.

Fetching and extraction are split on purpose: ``trafilatura.fetch_url``'s
failure mode — ``None`` for everything — is what caused the motivating
incident. The transport fetches with visible status codes; trafilatura is
demoted to extraction only, with ``include_links=True`` because the old
``include_links=False`` stripped the very URLs harvest reasons over.

Wayback fallback stays for failed fetches, and its failures are classified
like any fetch, never swallowed. A 200 whose extraction comes back thin is
``manual`` (reason ``thin-extraction``), never ``dead`` — our tooling can't
read it; that doesn't mean it's gone.

A 200 whose BODY is a document (magic bytes say PDF/OOXML/…, or the
content type maps to an extractable format) is neither thin nor web work at
all: detection's HEAD lied or was inconclusive. The driver signals a
redetection to ``file`` work and the run layer re-routes the unit — never
``manual`` for content the file driver can read.
"""

import html as html_lib
import json
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

from dex_engine.pipeline.classify import (
    MIN_SUBSTANTIAL_CHARS,
    THIN_EXTRACTION_REASON,
    Classification,
    classify_connection,
    classify_http,
)
from dex_engine.pipeline.detect import CONTENT_TYPE_FORMATS, sniff_format
from dex_engine.pipeline.types import Format, Kind, Redetection, Result, Status, WorkUnit
from dex_engine.pipeline.urls import base_canonical

from .transport import Transport, urllib_transport

__all__ = ["HtmlExtract", "WebDriver", "trafilatura_extract"]

# html -> markdown, or None when nothing extractable was found.
HtmlExtract = Callable[[str], str | None]

_WAYBACK_AVAILABLE = "https://archive.org/wayback/available?url="

_OG_IMAGE_RES = (
    re.compile(r"<meta[^>]+(?:property|name)=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)"),
    re.compile(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']og:image[\"']"),
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_MAX_TITLE_CHARS = 200


def trafilatura_extract(html: str) -> str | None:
    """Extract markdown from HTML via trafilatura — extraction ONLY.

    ``include_links=True`` is load-bearing: harvest reads the preserved
    hyperlinks.
    """
    import trafilatura  # noqa: PLC0415 — lazy: heavy dep, loaded only when extracting

    return trafilatura.extract(
        html, output_format="markdown", include_links=True, include_comments=False
    )


@dataclass(frozen=True, slots=True)
class _Page:
    """A successfully fetched page: decoded text plus what the wire said."""

    html: str
    body: bytes = b""
    content_type: str = ""


class WebDriver:
    """Fetch any web page: the ordered registry's catch-all, always last."""

    kind: Kind = Kind.WEB
    sleep: float = 1.0

    def __init__(
        self,
        *,
        transport: Transport = urllib_transport,
        extract: HtmlExtract = trafilatura_extract,
    ) -> None:
        """Wire the fetch and extraction seams.

        Args:
            transport: The HTTP seam; injected so tests are hermetic.
            extract: html -> markdown; injected for the same reason.
        """
        self._transport = transport
        self._extract = extract

    def matches(self, url: str) -> bool:  # noqa: ARG002 — the catch-all matches everything
        """True always — the catch-all; ordering makes this safe."""
        return True

    def canonical(self, url: str) -> str:
        """The generic canonical form (pipeline/urls.py rules)."""
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Result:
        """Fetch and extract one page, wayback fallback on fetch failure."""
        page = self._fetch_page(unit.url)
        if isinstance(page, _Page):
            fmt = _document_format(page)
            if fmt is not None:
                # Detection said web but the body is a document — a HEAD
                # lied or was inconclusive. Re-route, never thin-manual.
                return Result(
                    status=Status.QUEUED,
                    meta={},
                    redetect=Redetection(kind=Kind.FILE, format=fmt),
                )
            return self._extracted(page.html, allow_media=True) or Result(
                status=Status.MANUAL,
                meta=_title_meta(page.html),
                reason=THIN_EXTRACTION_REASON,
            )
        return self._wayback_fallback(unit.url, page)

    def _fetch_page(self, url: str) -> _Page | Classification:
        try:
            response = self._transport(url)
        except OSError as e:
            return classify_connection(e)
        if response.ok:
            return _Page(
                html=response.text(), body=response.body, content_type=response.content_type
            )
        return classify_http(response.status)

    def _extracted(self, html: str, *, allow_media: bool) -> Result | None:
        """A done Result when extraction is substantial, else None."""
        body = self._extract(html)
        if body is None or len(body) < MIN_SUBSTANTIAL_CHARS:
            return None
        media = [image] if allow_media and (image := _og_image(html)) else []
        return Result(status=Status.DONE, meta=_title_meta(html), body=body, media=media)

    def _wayback_fallback(self, url: str, failure: Classification) -> Result:
        """Try the wayback machine; on any rescue failure, the direct truth stands."""
        snapshot_url, note = self._wayback_snapshot(url)
        if snapshot_url is not None:
            page = self._fetch_page(snapshot_url)
            if isinstance(page, _Page):
                # Snapshot og:images point at web.archive.org — skip media.
                rescued = self._extracted(page.html, allow_media=False)
                if rescued is not None:
                    meta = dict(rescued.meta)
                    meta["via"] = "wayback"
                    meta["snapshot"] = snapshot_url
                    return Result(status=Status.DONE, meta=meta, body=rescued.body)
                note = "wayback snapshot extraction was thin"
            else:
                note = f"wayback fetch failed: {page.reason}"
        reason = failure.reason if note is None else f"{failure.reason}; {note}"
        return Result(status=failure.status, meta={}, reason=reason)

    def _wayback_snapshot(self, url: str) -> tuple[str | None, str | None]:
        """The closest snapshot URL, or (None, why the lookup yielded nothing)."""
        lookup = _WAYBACK_AVAILABLE + urllib.parse.quote(url, safe="")
        try:
            response = self._transport(lookup)
        except OSError as e:
            return None, f"wayback lookup failed: {classify_connection(e).reason}"
        if not response.ok:
            return None, f"wayback lookup failed: HTTP {response.status}"
        try:
            snapshots = json.loads(response.text())
        except json.JSONDecodeError:
            return None, "wayback lookup returned unparseable JSON"
        if not isinstance(snapshots, dict):
            return None, "wayback lookup returned an unexpected shape"
        archived = snapshots.get("archived_snapshots")
        closest = archived.get("closest") if isinstance(archived, dict) else None
        if not isinstance(closest, dict) or not closest.get("available") or not closest.get("url"):
            return None, "no wayback snapshot"
        return closest["url"], None


def _document_format(page: _Page) -> Format | None:
    """The extractable Format of a fetched body, or None for real web content.

    Magic bytes decide first (authoritative — servers lie in both
    directions); the declared content type is the fallback that catches
    signature-less formats (CSV). No filename-extension guessing here: a
    web URL's path tail is routing, not identity.
    """
    magic = sniff_format(page.body)
    if magic is not None:
        return magic
    return CONTENT_TYPE_FORMATS.get(page.content_type)


def _og_image(html: str) -> str | None:
    for pattern in _OG_IMAGE_RES:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def _title_meta(html: str) -> dict[str, str | int | None]:
    match = _TITLE_RE.search(html)
    if not match:
        return {}
    title = " ".join(html_lib.unescape(match.group(1)).split())[:_MAX_TITLE_CHARS]
    return {"title": title} if title else {}
