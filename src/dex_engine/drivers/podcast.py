"""The podcast driver: episode links resolve to the feed's enclosure.

Podcasting is RSS underneath; the audio lives in the feed's ``<enclosure>``:

- **Apple link** → iTunes lookup API (public, keyless) on the SHOW id →
  match the episode in the returned window → show RSS → enclosure.
- **Spotify link** → og-title from the page → iTunes *search* → RSS → match.
  Spotify exclusives fail honestly → ``Unusable`` (Claude may rescue via
  the show's own site).
- **Direct RSS / indie episode page** → the ``<link rel>`` feed pointer in
  the page head (its notes are richer), falling back to the enclosure the
  page carries itself. Such a page arrives here by re-detection from the
  web driver, on that same content signal — URL shape cannot tell an
  episode page from a post.

The driver only ever RESOLVES: the enclosure URL and the show notes (from
the feed — richer than the page) ride the ``NeedsCapability`` outcome's
meta/body, the run layer writes them into the enrichment file, and the
transcribe drain acquires the audio from that recorded pointer (audio
acquisition belongs to the drain, not the drivers).
"""

import html as html_lib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from dex_engine.pipeline.types import (
    Kind,
    Need,
    NeedsCapability,
    Outcome,
    Refused,
    Unusable,
    WorkUnit,
)
from dex_engine.pipeline.urls import base_canonical, host_of

from .audio import audio_enclosure
from .fetch import FetchFailure, fetch_classified
from .transport import Transport, urllib_transport

__all__ = ["PodcastDriver"]

# The lookup API resolves SHOW ids only: handed an episode id it answers
# `resultCount: 0` for every episode that exists, so the episode is found by
# matching trackId inside the show's episode window.
_ITUNES_LOOKUP = "https://itunes.apple.com/lookup?id={id}&entity=podcastEpisode&limit={limit}"
_ITUNES_SEARCH = "https://itunes.apple.com/search?media=podcast&entity=podcastEpisode&term={term}"
# The largest window the lookup API serves; older episodes fall outside it.
_ITUNES_EPISODE_WINDOW = 200

_APPLE_EPISODE_PARAM = "i"
_APPLE_SHOW_RE = re.compile(r"id(\d+)")

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
        """True for Apple/Spotify episode links and explicit ``.rss`` URLs.

        Deliberately narrow: bare ``/feed`` / ``/rss`` path suffixes and
        ``feeds.*`` / ``feed.*`` hosts are blog vocabulary too — the web
        driver keeps those. Indie episode pages reach this driver by
        content, not by URL shape: the web driver fetches them (registry
        order, catch-all last), finds the page carrying its own audio, and
        re-detects the unit to ``podcast`` — so no pattern here has to
        guess which ``/episodes/…`` path is a podcast and which is a blog.
        """
        host = host_of(url)
        if host == "podcasts.apple.com":
            return True
        path = urlsplit(url).path.rstrip("/")
        if host == "open.spotify.com":
            return path.startswith("/episode/")
        return path.lower().endswith(".rss")

    def canonical(self, url: str) -> str:
        """Apple keeps only the episode param; everything else is generic."""
        if host_of(url) == "podcasts.apple.com":
            return base_canonical(url, keep_params=frozenset({_APPLE_EPISODE_PARAM}))
        if host_of(url) == "open.spotify.com":
            return base_canonical(url, keep_params=frozenset())
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Outcome:
        """Resolve the episode; the resolution parks for the transcribe drain."""
        host = host_of(unit.url)
        if host == "podcasts.apple.com":
            resolved = self._resolve_apple(unit.url)
        elif host == "open.spotify.com":
            resolved = self._resolve_spotify(unit.url)
        else:
            resolved = self._resolve_rss_ish(unit.url)
        if not isinstance(resolved, _Episode):
            return resolved
        meta: dict[str, str | int | None] = {
            "title": resolved.title,
            "show": resolved.show,
            "published": resolved.published,
            "enclosure": resolved.enclosure,
        }
        return NeedsCapability(
            need=Need.TRANSCRIBE,
            meta=meta,
            body=resolved.notes or None,
            reason=_RESOLVED_REASON,
        )

    # -- Apple: iTunes lookup → RSS → match --------------------------

    def _resolve_apple(self, url: str) -> "_Episode | Outcome":
        parts = urlsplit(url)
        episode_ids = parse_qs(parts.query).get(_APPLE_EPISODE_PARAM, [])
        if not episode_ids or not episode_ids[0].isdigit():
            return _unusable(
                "Apple link names a show, not an episode — capture an episode link "
                "(the ?i= parameter)"
            )
        show_id = _apple_show_id(parts.path)
        if show_id is None:
            return _unusable("Apple link carries no show id to look up — rescue by hand")
        lookup = self._json(_ITUNES_LOOKUP.format(id=show_id, limit=_ITUNES_EPISODE_WINDOW))
        if not isinstance(lookup, dict):
            return lookup
        episode = _itunes_episode(lookup, track_id=int(episode_ids[0]))
        if episode is None:
            return _unusable(
                f"episode {episode_ids[0]} is not among the {_ITUNES_EPISODE_WINDOW} episodes "
                "iTunes lists for this show — rescue by hand"
            )
        return self._episode_from_feed(episode)

    # -- Spotify: og-title → iTunes search → RSS → match -------------

    def _resolve_spotify(self, url: str) -> "_Episode | Outcome":
        page = self._transport_result(url)
        if not isinstance(page, str):
            return page
        title = _og_title(page)
        if title is None:
            return _unusable("Spotify page exposed no episode title — rescue by hand")
        search = self._json(_ITUNES_SEARCH.format(term=quote(title)))
        if not isinstance(search, dict):
            return search
        episode = _itunes_episode(search, title=title)
        if episode is None:
            # The honest Spotify-exclusive outcome.
            return _unusable(
                f"episode {title!r} is not in the podcast index — possibly a Spotify "
                "exclusive; rescue via the show's own site"
            )
        return self._episode_from_feed(episode)

    def _episode_from_feed(self, episode: "_ItunesEpisode") -> "_Episode | Outcome":
        if episode.feed_url is None:
            return _unusable("iTunes knows the episode but not its feed — rescue by hand")
        feed = self._feed(episode.feed_url)
        if not isinstance(feed, _Feed):
            return feed
        item = _match_item(feed.items, guid=episode.guid, title=episode.title)
        if item is None:
            return _unusable(
                f"episode {episode.title or episode.guid!r} is not in the show's feed — "
                "it may have been pulled; rescue by hand"
            )
        return _episode(item, show=feed.show or episode.show)

    # -- Direct RSS / indie episode pages ----------------------------

    def _resolve_rss_ish(self, url: str) -> "_Episode | Outcome":
        body = self._transport_result(url)
        if not isinstance(body, str):
            return body
        if _looks_like_xml(body):
            feed = _parse_feed(body)
            if not isinstance(feed, _Feed):
                return feed
            if len(feed.items) == 1:
                return _episode(feed.items[0], show=feed.show)
            return _unusable(
                f"feed lists {len(feed.items)} episodes — capture an episode link, "
                "not the show feed"
            )
        return self._resolve_episode_page(url, body)

    def _resolve_episode_page(self, url: str, page: str) -> "_Episode | Outcome":
        """Resolve an indie episode page: its feed first, its own audio second.

        The feed is preferred because the feed's show notes are richer than
        the page's markup — but a page that carries its audio is resolvable
        with or without one, and it must be: the web driver routed this
        unit here on that signal, and bouncing it back would be a
        re-detection loop park.
        """
        episode = self._episode_from_page_feed(url, page)
        if isinstance(episode, _Episode):
            return episode
        enclosure = audio_enclosure(page, url)
        if enclosure is None:
            return episode  # the feed route's own failure stands
        return _Episode(
            title=_og_title(page),
            show=None,
            enclosure=enclosure.url,
            published=None,
            notes="",
        )

    def _episode_from_page_feed(self, url: str, page: str) -> "_Episode | Outcome":
        feed_url = _feed_link(page, url)
        if feed_url is None:
            return _unusable("page exposes no RSS feed link — rescue by hand")
        feed = self._feed(feed_url)
        if not isinstance(feed, _Feed):
            return feed
        item = _match_item(feed.items, link=url, title=_og_title(page))
        if item is None:
            return _unusable("episode not found in the site's feed — rescue by hand")
        return _episode(item, show=feed.show)

    # -- transport plumbing ------------------------------------------------

    def _transport_result(self, url: str) -> "str | Outcome":
        outcome = fetch_classified(self._transport, url)
        if isinstance(outcome, FetchFailure):
            return outcome.classification.to_outcome()
        return outcome.text()

    def _json(self, url: str) -> "dict | Outcome":
        body = self._transport_result(url)
        if not isinstance(body, str):
            return body
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return Refused(evidence="iTunes API returned unparseable JSON")
        if not isinstance(payload, dict):
            return Refused(evidence="iTunes API returned an unexpected shape")
        return payload

    def _feed(self, url: str) -> "_Feed | Outcome":
        body = self._transport_result(url)
        if not isinstance(body, str):
            return body
        return _parse_feed(body)


# ---------------------------------------------------------------------------
# iTunes payloads.
# ---------------------------------------------------------------------------


def _apple_show_id(path: str) -> str | None:
    """The show id from the ``/idNNNN`` segment every Apple podcast URL carries."""
    for segment in path.split("/"):
        match = _APPLE_SHOW_RE.fullmatch(segment)
        if match:
            return match.group(1)
    return None


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
        # suffixes the index does not. The LONGEST normalized match wins —
        # "Episode 10 — The Show" contains both "Episode 1" and
        # "Episode 10", and first-wins would resolve the wrong episode.
        wanted = _normalize(title)
        best: dict | None = None
        best_length = 0
        for entry in episodes:
            have = _normalize(str(entry.get("trackName") or ""))
            if have and (have in wanted or wanted in have) and len(have) > best_length:
                best, best_length = entry, len(have)
        if best is not None:
            return _itunes_entry(best)
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


def _parse_feed(body: str) -> _Feed | Outcome:
    try:
        root = ET.fromstring(body)  # noqa: S314 — feeds are parsed for text; no DTD/entity expansion in ET
    except ET.ParseError:
        return Refused(evidence="feed XML is unparseable")
    channel = root.find("channel")
    if channel is None:
        return _unusable("not an RSS feed (no channel) — rescue by hand")
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
    best: ET.Element | None = None
    best_length = 0
    for item in items:
        have = _normalize(_text(item.find("title")) or "")
        if not have:
            continue
        if have == wanted:
            return item
        # Containment prefers the LONGEST normalized match, as in the
        # iTunes matcher: "Episode 1" is inside "Episode 10 — The Show"
        # too, and first-wins would resolve the wrong episode.
        if (have in wanted or wanted in have) and len(have) > best_length:
            best, best_length = item, len(have)
    return best


def _episode(item: ET.Element, *, show: str | None) -> _Episode | Outcome:
    enclosure = item.find("enclosure")
    audio = enclosure.get("url") if enclosure is not None else None
    if not audio:
        return _unusable("episode item has no audio enclosure — rescue by hand")
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


def _feed_link(page: str, base_url: str) -> str | None:
    for tag in _FEED_LINK_RE.finditer(page):
        href = _HREF_RE.search(tag.group(0))
        if href:
            # Feed pointers are routinely relative ("/feed.xml"); the
            # driver fetches what it is handed, so it is absolutized here.
            return urljoin(base_url, html_lib.unescape(href.group(1)).strip())
    return None


# ---------------------------------------------------------------------------
# Show notes: HTML fragment → markdown-ish text, links KEPT (their
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


def _unusable(evidence: str) -> Unusable:
    return Unusable(evidence=evidence)
