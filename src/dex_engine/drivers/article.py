"""The shared article-fetch seam: fetch a page, extract it, rescue via wayback.

Two drivers read pages as articles — the web catch-all for everything it
matches, and the paper driver for the non-arxiv hosts (openreview,
hf-papers) whose pages read fine as articles — and the whole route is one
behaviour: classified fetch, mid-fetch re-detection of bodies that are not
web content at all, trafilatura extraction with links kept, thin-extraction
parking, and the wayback fallback for failed fetches. It lives here, beside
:mod:`dex_engine.drivers.transport` and the other shared seams, because a
seam several drivers share is not itself a driver, and a driver must never
import another driver.

Fetching and extraction are split on purpose: ``trafilatura.fetch_url``'s
failure mode — ``None`` for everything — is what caused the motivating
incident. The transport fetches with visible status codes; trafilatura is
demoted to extraction only, with ``include_links=True`` because the old
``include_links=False`` stripped the very URLs harvest reasons over.

That flag has a price, and the page is prepared before extraction to pay
it: with links on, an anchor carrying no text of its own does not merely
render as nothing, it swallows the content beside it — heading words after
a permalink, whole fenced blocks behind mkdocs-material's per-line
anchors. Extraction therefore runs twice over the prepared page, once
without comments and once with, because the two settings answer different
questions: an article's comment section is chrome, and a discussion page's
comments are the entire artifact.

Wayback fallback stays for failed fetches, and its failures are classified
like any fetch, never swallowed. A 200 whose extraction comes back thin is
``Unusable`` (evidence ``thin-extraction``), never ``Missing`` — our
tooling can't read it; that doesn't mean it's gone.

A 200 whose BODY is a document (magic bytes say PDF/OOXML/…, or the
content type maps to an extractable format) is neither thin nor article
work at all: detection's HEAD lied or was inconclusive. The route signals a
redetection to ``file`` work and the run layer re-routes the unit — never
``manual`` for content the file driver can read.

The same mid-fetch discovery carries indie podcast episode pages to the
``podcast`` driver, decided on content rather than on URL patterns that
cannot tell ``/feed`` the blog from ``/feed`` the show. What counts as
an episode signal is narrow, because the two mistakes cost differently:
handing an article to the podcast driver parks it awaiting transcription
with an EMPTY body — the article is never extracted and its links are
never harvested — while handing an episode page to ``web`` merely stores
its show notes. So an episode is a page whose ``og:audio`` names the audio as the
page's own object, or one carrying a player and no body worth extracting.
An ``<audio>`` element beside a real article is a read-aloud widget or a
media sample, and the article wins.
"""

import html as html_lib
import json
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lxml.html import HtmlElement

from dex_engine.pipeline.classify import (
    MIN_SUBSTANTIAL_CHARS,
    THIN_EXTRACTION_REASON,
    Classification,
)
from dex_engine.pipeline.detect import CONTENT_TYPE_FORMATS, sniff_format
from dex_engine.pipeline.types import Content, Format, Kind, Outcome, Redetected, Unusable

from .audio import audio_enclosure
from .fetch import FetchFailure, fetch_classified
from .transport import Transport

__all__ = ["HtmlExtract", "fetch_article", "trafilatura_extract"]

# html -> markdown, or None when nothing extractable was found.
HtmlExtract = Callable[[str], str | None]

_WAYBACK_AVAILABLE = "https://archive.org/wayback/available?url="

# The captured value runs from the opening quote to the closing one and may
# not cross a line break: a wrapped content attribute has no og:image this
# seam will vouch for. Capturing up to the break instead yielded the
# truncated head of the URL ("https://cdn.example.test/"), which is a
# perfectly well-formed request for a resource that does not exist — a
# guaranteed junk fetch ledgered as a real media unit.
_OG_IMAGE_RES = (
    re.compile(
        r"<meta[^>]+(?:property|name)=[\"']og:image[\"'][^>]+content=[\"']([^\"'\r\n]+)[\"']"
    ),
    re.compile(
        r"<meta[^>]+content=[\"']([^\"'\r\n]+)[\"'][^>]+(?:property|name)=[\"']og:image[\"']"
    ),
)
# A discussion page's comments ARE the artifact; an article's are chrome,
# and dropping them is right for the article. Nothing in the markup tells
# the two apart, but the SIZE does, and not marginally: measured over docs
# sites, blogs, a news post and a Django reference, every article extracts
# to exactly the same length either way (ratio 1.00), while a Hacker News
# thread grows 38x — 1,058 characters of masthead against the 40,353 the
# 115 comments actually are. There is nothing in between to misjudge, so
# the bar sits far from both: a page whose comments more than double it is
# a discussion, and the discussion is what was shared.
_DISCUSSION_RATIO = 2.0

_WHITESPACE_RUN_RE = re.compile(r"\s+")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_MAX_TITLE_CHARS = 200


def trafilatura_extract(html: str) -> str | None:
    """Extract markdown from HTML via trafilatura — extraction ONLY.

    ``include_links=True`` is load-bearing: harvest reads the preserved
    hyperlinks. It is also, unaccompanied, destructive — see
    :func:`_prepare_page`, which is why the page is prepared first and
    never handed to the extractor raw.
    """
    prepared = _prepare_page(html)
    body = _extract_markdown(prepared, comments=False)
    discussion = _extract_markdown(prepared, comments=True)
    if discussion is not None and len(discussion) > len(body or "") * _DISCUSSION_RATIO:
        return discussion
    return body


def _extract_markdown(html: str, *, comments: bool) -> str | None:
    import trafilatura  # noqa: PLC0415 — lazy: heavy dep, loaded only when extracting

    return trafilatura.extract(
        html, output_format="markdown", include_links=True, include_comments=comments
    )


def _prepare_page(html: str) -> str:
    """Repair the page shapes that break extraction, before trafilatura sees it.

    Two repairs over one parse. First, drop every anchor carrying no text
    — permalinks, icon links, line anchors. An anchor with nothing inside
    it is chrome, and with ``include_links`` on it is chrome that eats
    content: a heading whose permalink precedes its words
    (``<h2><a class="headerlink" href="#x"></a>Text</h2>`` — the
    Sphinx/mkdocs/Docusaurus spelling) extracts as a bare ``##`` with the
    words gone, and mkdocs-material's per-line code anchors take whole
    fenced blocks with them (a llama-cpp docs page: 42 fences down to
    none). Dropping them is lossless — there is nothing inside an empty
    anchor to lose, and an anchor with no text renders no link. On that
    same page this recovers 92 fences AND keeps all 42 hyperlinks, which
    neither link setting manages alone.

    Second, flatten the inside of headings: drop ``<br>`` elements and
    collapse whitespace runs to one space. A markdown heading is one line
    by construction, and a heading holding a line break
    (``<h3><span>Use Case: <br></span></h3>`` — Hugging Face model cards)
    extracts as the marker alone on its line with the words below it,
    among the span's source-formatting tabs and newlines emitted verbatim
    — an empty heading to any consumer keying on the heading line. The
    ``<br>`` is the break; the whitespace collapse is what puts the words
    beside the marker cleanly instead of behind a run of tabs.

    A page lxml cannot parse goes through untouched: preparation is a
    repair, never a gate.
    """
    from lxml.etree import LxmlError  # noqa: PLC0415 — lazy: pulled in with trafilatura
    from lxml.html import HtmlElement, fromstring, tostring  # noqa: PLC0415

    try:
        tree = fromstring(html)
    except (LxmlError, ValueError):
        return html
    # Materialized before the loop: drop_tree() detaches from the live tree,
    # and mutating what you are iterating skips siblings. drop_tree is also
    # the right removal — it MERGES the anchor's tail into the preceding
    # text, which is exactly where a heading's words live.
    for anchor in list(tree.iter("a")):
        if isinstance(anchor, HtmlElement) and not (anchor.text_content() or "").strip():
            anchor.drop_tree()
    for heading in tree.iter("h1", "h2", "h3", "h4", "h5", "h6"):
        if isinstance(heading, HtmlElement):
            _flatten_heading(heading)
    return tostring(tree, encoding="unicode")


def _flatten_heading(heading: "HtmlElement") -> None:
    from lxml.html import HtmlElement  # noqa: PLC0415 — lazy: pulled in with trafilatura

    for br in list(heading.iter("br")):
        if isinstance(br, HtmlElement):
            br.drop_tree()
    for node in heading.iter():
        if not isinstance(node, HtmlElement):
            continue
        if node.text:
            node.text = _WHITESPACE_RUN_RE.sub(" ", node.text)
        # The heading's own tail is the text AFTER it — not its content.
        if node is not heading and node.tail:
            node.tail = _WHITESPACE_RUN_RE.sub(" ", node.tail)


@dataclass(frozen=True, slots=True)
class _Page:
    """A successfully fetched page: decoded text plus what the wire said."""

    html: str
    body: bytes = b""
    content_type: str = ""


def fetch_article(transport: Transport, extract: HtmlExtract, url: str) -> Outcome:
    """Fetch and extract one page as an article, wayback fallback on failure.

    Args:
        transport: The HTTP seam.
        extract: html -> markdown; injected so callers are hermetic.
        url: The page URL.

    Returns:
        What the fetch found: Content with body and media, a Redetected
        for a body that is not web content, a thin-extraction Unusable,
        or the classified fetch failure with the wayback rescue's fate
        noted on its evidence.
    """
    page = _fetch_page(transport, url)
    if isinstance(page, _Page):
        fmt = _document_format(page)
        if fmt is not None:
            # Detection said web but the body is a document — a HEAD
            # lied or was inconclusive. Re-route, never thin-unusable.
            return Redetected(kind=Kind.FILE, format=fmt)
        enclosure = audio_enclosure(page.html, url)
        if enclosure is not None and enclosure.declared:
            return Redetected(kind=Kind.PODCAST)
        extracted = _extracted(extract, page.html, base_url=url, allow_media=True)
        if extracted is not None:
            return extracted
        if enclosure is not None:
            # A player and nothing worth extracting: the audio is what
            # the page is. Ordering is the whole rule — an article's
            # read-aloud widget was reached above, by its own body.
            return Redetected(kind=Kind.PODCAST)
        return Unusable(evidence=THIN_EXTRACTION_REASON)
    return _wayback_fallback(transport, extract, url, page)


def _fetch_page(transport: Transport, url: str) -> _Page | Classification:
    outcome = fetch_classified(transport, url)
    if isinstance(outcome, FetchFailure):
        return outcome.classification
    return _Page(html=outcome.text(), body=outcome.body, content_type=outcome.content_type)


def _extracted(
    extract: HtmlExtract, html: str, *, base_url: str, allow_media: bool
) -> Content | None:
    """The page as Content when extraction is substantial, else None."""
    body = extract(html)
    if body is None or len(body) < MIN_SUBSTANTIAL_CHARS:
        return None
    media = [image] if allow_media and (image := _og_image(html, base_url)) else []
    return Content(meta=_title_meta(html), body=body, media=media)


def _wayback_fallback(
    transport: Transport, extract: HtmlExtract, url: str, failure: Classification
) -> Outcome:
    """Try the wayback machine; on any rescue failure, the direct truth stands."""
    snapshot_url, note = _wayback_snapshot(transport, url)
    if snapshot_url is not None:
        page = _fetch_page(transport, snapshot_url)
        if isinstance(page, _Page):
            # Snapshot og:images point at web.archive.org — skip media.
            rescued = _extracted(extract, page.html, base_url=snapshot_url, allow_media=False)
            if rescued is not None:
                meta = dict(rescued.meta)
                meta["via"] = "wayback"
                meta["snapshot"] = snapshot_url
                return Content(meta=meta, body=rescued.body)
            note = "wayback snapshot extraction was thin"
        else:
            note = f"wayback fetch failed: {page.reason}"
    if note is not None:
        # The rescue's fate is appended context on the direct truth —
        # the classification's own finding and evidence still decide.
        failure = replace(failure, reason=f"{failure.reason}; {note}")
    return failure.to_outcome()


def _wayback_snapshot(transport: Transport, url: str) -> tuple[str | None, str | None]:
    """The closest snapshot URL, or (None, why the lookup yielded nothing)."""
    lookup = _WAYBACK_AVAILABLE + urllib.parse.quote(url, safe="")
    outcome = fetch_classified(transport, lookup)
    if isinstance(outcome, FetchFailure):
        # A failed rescue is only a note on the direct failure — the
        # wire fact, never the classifier's status framing.
        return None, f"wayback lookup failed: {outcome.detail}"
    try:
        snapshots = json.loads(outcome.text())
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


def _og_image(html: str, base_url: str) -> str | None:
    """The page's og:image as an absolute http(s) URL, or None.

    The media stage fetches what it is handed verbatim, so a media URL must
    be fetchable by construction: relative and protocol-relative values
    resolve against the page they were found on, entities unescape, and
    anything that is not http(s) afterwards (``data:``, ``javascript:``, a
    template placeholder) is not a media URL at all.
    """
    for pattern in _OG_IMAGE_RES:
        match = pattern.search(html)
        if match is None:
            continue
        candidate = urllib.parse.urljoin(base_url, html_lib.unescape(match.group(1)).strip())
        if candidate.startswith(("http://", "https://")):
            return candidate
    return None


def _title_meta(html: str) -> dict[str, str | int | None]:
    match = _TITLE_RE.search(html)
    if not match:
        return {}
    title = " ".join(html_lib.unescape(match.group(1)).split())[:_MAX_TITLE_CHARS]
    return {"title": title} if title else {}
