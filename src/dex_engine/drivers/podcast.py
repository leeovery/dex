"""The podcast driver (§9): episode links resolve to the feed's enclosure.

Podcasting is RSS underneath; the audio lives in the feed's ``<enclosure>``:

- **Apple link** → iTunes lookup API (public, keyless) → show RSS → match
  episode → enclosure.
- **Spotify link** → og-title from the page → iTunes *search* → RSS → match.
  Spotify exclusives fail honestly → ``manual`` (Claude may rescue via the
  show's own site).
- **Direct RSS / indie episode page** → enclosure, or the ``<link rel>``
  feed pointer in the page head.

The driver only ever RESOLVES: the enclosure URL and the show notes (from
the feed — richer than the page) ride the waiting Result's meta/body, the
run layer writes them into the enrichment file, and the transcribe drain
acquires the audio from that recorded pointer (§6 — audio acquisition
belongs to the drain, not the drivers).
"""

import html as html_lib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlsplit

from dex_engine.pipeline.classify import Classification, classify_connection, classify_http
from dex_engine.pipeline.types import Kind, Need, Result, Status, WorkUnit
from dex_engine.pipeline.urls import base_canonical, host_of

from .transport import Transport, urllib_transport

__all__ = ["PodcastDriver"]

_ITUNES_LOOKUP = "https://itunes.apple.com/lookup?id={id}&entity=podcastEpisode"
_ITUNES_SEARCH = "https://itunes.apple.com/search?media=podcast&entity=podcastEpisode&term={term}"

_APPLE_EPISODE_PARAM = "i"
_RSS_HOST_PREFIXES = ("feeds.", "feed.")
_RSS_PATH_SUFFIXES = (".rss", "/feed", "/rss")

_OG_TITLE_RES = (
    re.compile(r"<meta[^>]+(?:property|name)=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)"),
    re.compile(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']og:title[\"']"),
)
_FEED_LINK_RE = re.compile(
    r"<link[^>]+type=[\"']application/(?:rss|atom)\+xml[\"'][^>]*>", re.IGNORECASE
)
_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)

_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
_ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"

_RESOLVED_REASON = "episode resolved — audio awaits transcription"


@dataclass(frozen=True, slots=True, kw_only=True)
class _Episode:
    """One matched feed item, enclosure and show notes in hand."""

    title: str | None
    show: str | None
    enclosure: str
    published: str | None
    notes: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _Feed:
    """A parsed RSS feed."""

    show: str | None
    items: list[ET.Element]


class PodcastDriver:
    """Resolve Apple/Spotify/RSS episode links to enclosure + show notes."""

    kind: Kind = Kind.PODCAST
    sleep: float = 2.0

    def __init__(self, *, transport: Transport = urllib_transport) -> None:
        """Wire the HTTP seam (iTunes APIs, feeds, and pages all ride it).

        Args:
            transport: The HTTP seam; injected so tests are hermetic.
        """
        self._transport = transport

    def matches(self, url: str) -> bool:
        """True for Apple/Spotify episode links and RSS-ish URLs."""
        host = host_of(url)
        if host == "podcasts.apple.com":
            return True
        path = urlsplit(url).path.rstrip("/")
        if host == "open.spotify.com":
            return path.startswith("/episode/")
        return host.startswith(_RSS_HOST_PREFIXES) or path.lower().endswith(_RSS_PATH_SUFFIXES)

    def canonical(self, url: str) -> str:
        """Apple keeps only the episode param; everything else is generic."""
        if host_of(url) == "podcasts.apple.com":
            return base_canonical(url, keep_params=frozenset({_APPLE_EPISODE_PARAM}))
        if host_of(url) == "open.spotify.com":
            return base_canonical(url, keep_params=frozenset())
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Result:
        """Resolve the episode; success parks ``waiting`` for the drain (§9)."""
        host = host_of(unit.url)
        if host == "podcasts.apple.com":
            resolved = self._resolve_apple(unit.url)
        elif host == "open.spotify.com":
            resolved = self._resolve_spotify(unit.url)
        else:
            resolved = self._resolve_rss_ish(unit.url)
        if isinstance(resolved, Result):
            return resolved
        meta: dict[str, str | int | None] = {
            "title": resolved.title,
            "show": resolved.show,
            "published": resolved.published,
            "enclosure": resolved.enclosure,
        }
        return Result(
            status=Status.WAITING,
            meta=meta,
            body=resolved.notes or None,
            needs=Need.TRANSCRIBE,
            reason=_RESOLVED_REASON,
        )

    # -- Apple (§9): iTunes lookup → RSS → match --------------------------

    def _resolve_apple(self, url: str) -> "_Episode | Result":
        episode_ids = parse_qs(urlsplit(url).query).get(_APPLE_EPISODE_PARAM, [])
        if not episode_ids or not episode_ids[0].isdigit():
            return _manual(
                "Apple link names a show, not an episode — capture an episode link "
                "(the ?i= parameter)"
            )
        lookup = self._json(_ITUNES_LOOKUP.format(id=episode_ids[0]))
        if isinstance(lookup, Result):
            return lookup
        episode = _itunes_episode(lookup, track_id=int(episode_ids[0]))
        if episode is None:
            return _manual("iTunes lookup does not know this episode — rescue by hand")
        return self._episode_from_feed(episode)

    # -- Spotify (§9): og-title → iTunes search → RSS → match -------------

    def _resolve_spotify(self, url: str) -> "_Episode | Result":
        page = self._transport_result(url)
        if isinstance(page, Result):
            return page
        title = _og_title(page)
        if title is None:
            return _manual("Spotify page exposed no episode title — rescue by hand")
        search = self._json(_ITUNES_SEARCH.format(term=quote(title)))
        if isinstance(search, Result):
            return search
        episode = _itunes_episode(search, title=title)
        if episode is None:
            # The honest Spotify-exclusive outcome (§9).
            return _manual(
                f"episode {title!r} is not in the podcast index — possibly a Spotify "
                "exclusive; rescue via the show's own site"
            )
        return self._episode_from_feed(episode)

    def _episode_from_feed(self, episode: "_ItunesEpisode") -> "_Episode | Result":
        if episode.feed_url is None:
            return _manual("iTunes knows the episode but not its feed — rescue by hand")
        feed = self._feed(episode.feed_url)
        if isinstance(feed, Result):
            return feed
        item = _match_item(feed.items, guid=episode.guid, title=episode.title)
        if item is None:
            return _manual(
                f"episode {episode.title or episode.guid!r} is not in the show's feed — "
                "it may have been pulled; rescue by hand"
            )
        return _episode(item, show=feed.show or episode.show)

    # -- Direct RSS / indie episode pages (§9) ----------------------------

    def _resolve_rss_ish(self, url: str) -> "_Episode | Result":
        body = self._transport_result(url)
        if isinstance(body, Result):
            return body
        if _looks_like_xml(body):
            feed = _parse_feed(body)
            if isinstance(feed, Result):
                return feed
            if len(feed.items) == 1:
                return _episode(feed.items[0], show=feed.show)
            return _manual(
                f"feed lists {len(feed.items)} episodes — capture an episode link, "
                "not the show feed"
            )
        return self._resolve_episode_page(url, body)

    def _resolve_episode_page(self, url: str, page: str) -> "_Episode | Result":
        feed_url = _feed_link(page)
        if feed_url is None:
            return _manual("page exposes no RSS feed link — rescue by hand")
        feed = self._feed(feed_url)
        if isinstance(feed, Result):
            return feed
        item = _match_item(feed.items, link=url, title=_og_title(page))
        if item is None:
            return _manual("episode not found in the site's feed — rescue by hand")
        return _episode(item, show=feed.show)

    # -- transport plumbing ------------------------------------------------

    def _transport_result(self, url: str) -> "str | Result":
        try:
            response = self._transport(url)
        except OSError as e:
            return _classified(classify_connection(e))
        if not response.ok:
            return _classified(classify_http(response.status))
        return response.text()

    def _json(self, url: str) -> "dict | Result":
        body = self._transport_result(url)
        if isinstance(body, Result):
            return body
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return Result(
                status=Status.BLOCKED, meta={}, reason="iTunes API returned unparseable JSON"
            )
        if not isinstance(payload, dict):
            return Result(
                status=Status.BLOCKED, meta={}, reason="iTunes API returned an unexpected shape"
            )
        return payload

    def _feed(self, url: str) -> "_Feed | Result":
        body = self._transport_result(url)
        if isinstance(body, Result):
            return body
        return _parse_feed(body)


# ---------------------------------------------------------------------------
# iTunes payloads.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class _ItunesEpisode:
    """What iTunes knows about an episode: enough to find it in its feed."""

    title: str | None
    show: str | None
    feed_url: str | None
    guid: str | None


def _itunes_episode(
    payload: dict, *, track_id: int | None = None, title: str | None = None
) -> _ItunesEpisode | None:
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    episodes = [
        entry
        for entry in results
        if isinstance(entry, dict) and entry.get("wrapperType") == "podcastEpisode"
    ]
    for entry in episodes:
        if track_id is not None and entry.get("trackId") == track_id:
            return _itunes_entry(entry)
        if title is not None and _normalize(str(entry.get("trackName") or "")) == _normalize(title):
            return _itunes_entry(entry)
    if title is not None:
        # Fall back to containment: Spotify og-titles sometimes carry show
        # suffixes the index does not.
        wanted = _normalize(title)
        for entry in episodes:
            have = _normalize(str(entry.get("trackName") or ""))
            if have and (have in wanted or wanted in have):
                return _itunes_entry(entry)
    return None


def _itunes_entry(entry: dict) -> _ItunesEpisode:
    feed = entry.get("feedUrl")
    guid = entry.get("episodeGuid")
    return _ItunesEpisode(
        title=str(entry["trackName"]) if entry.get("trackName") else None,
        show=str(entry["collectionName"]) if entry.get("collectionName") else None,
        feed_url=str(feed) if isinstance(feed, str) and feed else None,
        guid=str(guid) if isinstance(guid, str) and guid else None,
    )


# ---------------------------------------------------------------------------
# Feeds.
# ---------------------------------------------------------------------------


def _looks_like_xml(body: str) -> bool:
    head = body.lstrip("﻿ \t\r\n")[:200].lower()
    return head.startswith(("<?xml", "<rss", "<feed"))


def _parse_feed(body: str) -> _Feed | Result:
    try:
        root = ET.fromstring(body)  # noqa: S314 — feeds are parsed for text; no DTD/entity expansion in ET
    except ET.ParseError:
        return Result(status=Status.BLOCKED, meta={}, reason="feed XML is unparseable")
    channel = root.find("channel")
    if channel is None:
        return Result(
            status=Status.MANUAL,
            meta={},
            reason="not an RSS feed (no channel) — rescue by hand",
        )
    return _Feed(show=_text(channel.find("title")), items=channel.findall("item"))


def _match_item(
    items: list[ET.Element],
    *,
    guid: str | None = None,
    title: str | None = None,
    link: str | None = None,
) -> ET.Element | None:
    """Best identification wins: guid, then the item link, then title fuzz."""
    matchers = (
        _match_by_guid(items, guid),
        _match_by_link(items, link),
        _match_by_title(items, title),
    )
    return next((item for item in matchers if item is not None), None)


def _match_by_guid(items: list[ET.Element], guid: str | None) -> ET.Element | None:
    if guid is None:
        return None
    return next((item for item in items if (_text(item.find("guid")) or "").strip() == guid), None)


def _match_by_link(items: list[ET.Element], link: str | None) -> ET.Element | None:
    if link is None:
        return None
    wanted = base_canonical(link)
    for item in items:
        item_link = _text(item.find("link"))
        if item_link and base_canonical(item_link) == wanted:
            return item
    return None


def _match_by_title(items: list[ET.Element], title: str | None) -> ET.Element | None:
    wanted = _normalize(title) if title is not None else ""
    if not wanted:
        return None
    for item in items:
        have = _normalize(_text(item.find("title")) or "")
        if have and (have == wanted or have in wanted or wanted in have):
            return item
    return None


def _episode(item: ET.Element, *, show: str | None) -> _Episode | Result:
    enclosure = item.find("enclosure")
    audio = enclosure.get("url") if enclosure is not None else None
    if not audio:
        return _manual("episode item has no audio enclosure — rescue by hand")
    published = _text(item.find("pubDate"))
    notes_html = (
        _text(item.find(f"{_CONTENT_NS}encoded"))
        or _text(item.find("description"))
        or _text(item.find(f"{_ITUNES_NS}summary"))
        or ""
    )
    return _Episode(
        title=_text(item.find("title")),
        show=show,
        enclosure=audio,
        published=published,
        notes=_html_to_markdown(notes_html),
    )


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    collapsed = " ".join(element.text.split()) if "\n" not in element.text else element.text.strip()
    return collapsed or None


def _normalize(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", title.casefold()).split())


# ---------------------------------------------------------------------------
# Pages.
# ---------------------------------------------------------------------------


def _og_title(page: str) -> str | None:
    for pattern in _OG_TITLE_RES:
        match = pattern.search(page)
        if match:
            title = " ".join(html_lib.unescape(match.group(1)).split())
            if title:
                return title
    return None


def _feed_link(page: str) -> str | None:
    for tag in _FEED_LINK_RE.finditer(page):
        href = _HREF_RE.search(tag.group(0))
        if href:
            return html_lib.unescape(href.group(1))
    return None


# ---------------------------------------------------------------------------
# Show notes: HTML fragment → markdown-ish text, links KEPT (§9 — their
# links are harvestable like any content).
# ---------------------------------------------------------------------------

_A_RE = re.compile(r"<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_BREAK_RE = re.compile(r"<(?:br\s*/?|/p|/div|/h[1-6])>", re.IGNORECASE)
_LI_RE = re.compile(r"<li[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_markdown(fragment: str) -> str:
    text = _A_RE.sub(lambda m: f"[{' '.join(m.group(2).split())}]({m.group(1)})", fragment)
    text = _BREAK_RE.sub("\n", text)
    text = _LI_RE.sub("\n- ", text)
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    collapsed = "\n".join(line for line in lines)
    return re.sub(r"\n{3,}", "\n\n", collapsed).strip()


def _manual(reason: str) -> Result:
    return Result(status=Status.MANUAL, meta={}, reason=reason)


def _classified(classification: Classification) -> Result:
    return Result(status=classification.status, meta={}, reason=classification.reason)
