"""The paper driver: arxiv API + HTML full text; openreview/hf-papers as articles.

arxiv papers get the abstract from the export API (Atom, parsed properly —
no regex-over-XML) plus full text from arxiv's own HTML rendering, falling
back to ar5iv. A missing full text degrades to abstract-only with a note —
the abstract in hand is not thrown away over a rendering gap.

openreview and huggingface.co/papers pages read fine as articles, so they
delegate to the web driver's fetch (classified statuses, wayback fallback
and all); the ledger kind stays ``paper`` — detection, not the delegate,
owns kind.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlsplit

from dex_engine.pipeline.classify import (
    Classification,
    classify_connection,
    classify_http,
)
from dex_engine.pipeline.types import Kind, Result, Status, WorkUnit
from dex_engine.pipeline.urls import base_canonical, host_of

from .transport import Transport, urllib_transport
from .web import HtmlExtract, WebDriver, trafilatura_extract

__all__ = ["PaperDriver"]

ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([\d.]+?)(?:v\d+)?(?:\.pdf)?/?$")

_ARXIV_API = "https://export.arxiv.org/api/query?id_list="
_ATOM = "{http://www.w3.org/2005/Atom}"

# Below this, an "HTML full text" is navigation chrome, not the paper.
_MIN_FULLTEXT_CHARS = 2000


class PaperDriver:
    """Fetch papers: arxiv natively, openreview/hf-papers via the web driver."""

    kind: Kind = Kind.PAPER
    sleep: float = 3.0

    def __init__(
        self,
        *,
        transport: Transport = urllib_transport,
        extract: HtmlExtract = trafilatura_extract,
        web: WebDriver | None = None,
    ) -> None:
        """Wire the HTTP/extraction seams and the web-driver delegate.

        Args:
            transport: The HTTP seam.
            extract: html -> markdown for the arxiv full-text pages.
            web: The delegate for non-arxiv paper pages; built from the same
                seams when not given.
        """
        self._transport = transport
        self._extract = extract
        self._web = web or WebDriver(transport=transport, extract=extract)

    def matches(self, url: str) -> bool:
        """True for arxiv.org, openreview.net, and huggingface.co/papers."""
        host = host_of(url)
        if host in ("arxiv.org", "openreview.net"):
            return True
        return host == "huggingface.co" and urlsplit(url).path.startswith("/papers")

    def canonical(self, url: str) -> str:
        """The generic canonical form (ARXIV_ID handles abs/pdf/html variants)."""
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Result:
        """Arxiv ids go through the API; everything else reads as an article."""
        match = ARXIV_ID.search(unit.url)
        if match is None:
            return self._web.fetch(unit)
        return self._fetch_arxiv(match.group(1))

    def _fetch_arxiv(self, arxiv_id: str) -> Result:
        entry = self._fetch_feed_entry(arxiv_id)
        if isinstance(entry, Classification):
            return Result(status=entry.status, meta={}, reason=entry.reason)
        meta: dict[str, str | int | None] = {
            "title": entry.title,
            "published": entry.published,
            "arxiv_id": arxiv_id,
        }
        body = f"## Abstract\n\n{entry.summary}" if entry.summary else ""
        full_text = self._fetch_full_text(arxiv_id)
        if full_text:
            body = f"{body}\n\n## Full text\n\n{full_text}" if body else full_text
        else:
            meta["note"] = "abstract only"
        if not body:
            return Result(
                status=Status.MANUAL, meta=meta, reason="arxiv entry had no abstract or full text"
            )
        return Result(status=Status.DONE, meta=meta, body=body)

    def _fetch_feed_entry(self, arxiv_id: str) -> "_Entry | Classification":
        try:
            response = self._transport(_ARXIV_API + arxiv_id)
        except OSError as e:
            return classify_connection(e)
        if not response.ok:
            return classify_http(response.status)
        try:
            feed = ET.fromstring(response.text())  # noqa: S314 — arxiv's own API; no DTD/entity risk in Atom
        except ET.ParseError:
            return Classification(
                status=Status.BLOCKED, reason="arxiv API returned unparseable XML"
            )
        entry = feed.find(f"{_ATOM}entry")
        if entry is None:
            return Classification(status=Status.DEAD, reason=f"arxiv: no such id ({arxiv_id})")
        published = entry.findtext(f"{_ATOM}published") or ""
        return _Entry(
            title=_collapse(entry.findtext(f"{_ATOM}title")),
            summary=_collapse(entry.findtext(f"{_ATOM}summary")),
            published=published[:10] or None,
        )

    def _fetch_full_text(self, arxiv_id: str) -> str | None:
        """Arxiv's HTML rendering, ar5iv fallback; None degrades to abstract-only."""
        for url in (
            f"https://arxiv.org/html/{arxiv_id}",
            f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}",
        ):
            try:
                response = self._transport(url)
            except OSError:
                continue  # the abstract stands; the miss is noted in meta
            if not response.ok:
                continue
            text = self._extract(response.text())
            if text and len(text) >= _MIN_FULLTEXT_CHARS:
                return text
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class _Entry:
    """One parsed arxiv feed entry."""

    title: str | None
    summary: str | None
    published: str | None


def _collapse(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(text.split())
    return collapsed or None
