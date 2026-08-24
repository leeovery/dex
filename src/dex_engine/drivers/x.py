"""The x driver (renamed from tweet): fxtwitter fetch + thread walk-up.

A post's identity is its status id: every share shape — the username form,
``/i/web/status/<id>``, ``/i/status/<id>``, the legacy ``/statuses/``
spelling, the twitter.com and mobile.twitter.com hosts — canonicalizes to
one id-keyed form, so one post is one work unit however it was shared.

The chain above a captured post is context, not new first-class sources:
one enrichment file, one ledger entry. The walk follows fxtwitter parent
pointers bottom-to-top (bounded at 100 hops, paced a second apart, ending
at the first id it has already walked); storage is reading order — root
first, captured post last, each post attributed. Quoted posts stay inline
as blockquotes; promoting a quote is a harvest judgment. Chain media is
pooled, the captured post's first — **photos and videos alike**: both are
URL downloads, and the media stage already meets an oversize one with its
own honest outcome (``skipped``, the 10MB ceiling named, charged to the
media unit). A media-only post is therefore ``done`` with a minimal
attributed body, whatever the media is; the "no text or media" park is
said only when the payload truly holds neither.

Incomplete chains are recorded, never silently presented as complete: a
parent fetch failing mid-walk, or a chain looping back on itself, sets
``chain_incomplete`` in meta with how far the walk got. Walk-down is
explicitly unsolved — backlog.

Long-form articles carry their prose under ``article`` (``text`` is empty
and ``raw_text`` holds only the shortlink), so they render from title and
preview text. A body that is nothing but a shortlink is not content: the
"no text or media" manual park applies rather than ledgering a post done
on a URL.
"""

import json
import re
import time
import urllib.parse
from collections.abc import Callable

from dex_engine.pipeline.classify import Classification
from dex_engine.pipeline.types import Kind, Result, Status, WorkUnit
from dex_engine.pipeline.urls import base_canonical, host_of

from .fetch import FetchFailure, fetch_classified
from .transport import Transport, urllib_transport

__all__ = ["XDriver"]

_API = "https://api.fxtwitter.com/"
_HOSTS = frozenset({"x.com", "twitter.com", "mobile.twitter.com"})

# Every status-URL path shape in the wild: /<user>/status/<id>,
# /i/web/status/<id>, /i/status/<id>, the legacy /statuses/ spelling —
# with or without a /photo/1-style tail.
_STATUS_PATH_RE = re.compile(r"/status(?:es)?/(\d+)")

# Thread walk-up bound: 100 parent hops above the captured post. A thread
# is ONE piece of content — one work unit, one enrichment file — and the
# walk is a linear chain of cheap calls with no fan-out, so this is a
# sanity bound against a chain that never ends, not an editorial one:
# 30-post threads are ordinary, and half an argument stored as though it
# were whole is the failure worth avoiding. Cycles do NOT reach it: an id
# already walked ends the walk where it repeats.
MAX_HOPS = 100

# Pacing between parent fetches. The driver's own 4s politeness is spent
# between UNITS, and a walk is many requests inside one — a 30-post thread
# went out as 30 back-to-back calls to a free community API. One second a
# hop keeps an ordinary thread under a minute while making the walk a
# paced sequence rather than a burst.
HOP_SLEEP = 1.0

# A body that is nothing but x's own t.co shortlink is a pointer to
# content, never the content itself.
_SHORTLINK_ONLY_RE = re.compile(r"\s*https?://t\.co/\S+\s*")

# fxtwitter nests a post's media under `photos` and `videos`, and repeats
# the union of both under `all` in post order. `all` leads so a mixed post
# keeps its order; the typed lists follow so a payload without `all` loses
# nothing, and the URL dedupe absorbs the overlap. Reading `photos` alone
# made a video-only post look medialess: it parked `manual` saying
# "fxtwitter returned no text or media" over a payload holding media, and
# dropped the video the media stage would have fetched.
_MEDIA_LISTS = ("all", "photos", "videos")


class XDriver:
    """Fetch one x.com/twitter.com post with its parent chain as context."""

    kind: Kind = Kind.X
    sleep: float = 4.0

    def __init__(
        self,
        *,
        transport: Transport = urllib_transport,
        pace: Callable[[float], None] = time.sleep,
    ) -> None:
        """Wire the HTTP seam (fxtwitter is plain JSON over GET) and its pacing.

        Args:
            transport: The HTTP seam; injected so tests are hermetic.
            pace: The walk's own pacing seam — ``sleep`` names the driver's
                per-unit politeness, which the run layer spends. Injected
                so tests are hermetic.
        """
        self._transport = transport
        self._pace = pace

    def matches(self, url: str) -> bool:
        """True for x.com, twitter.com, and mobile.twitter.com hosts."""
        return host_of(url) in _HOSTS

    def canonical(self, url: str) -> str:
        """Status URLs collapse to ``https://x.com/i/status/<id>`` — the id is the identity."""
        status_id = _status_id(url)
        if status_id is not None:
            return f"https://x.com/i/status/{status_id}"
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Result:
        """Fetch the captured post, walk its parent chain, render reading order."""
        status_id = _status_id(unit.url)
        if status_id is None:
            return Result(
                status=Status.MANUAL,
                meta={},
                reason="not a status URL — fxtwitter serves posts only",
            )
        # Bare status/<id> is the one fxtwitter path that serves every share
        # shape: the API ignores a username segment but 404s on /i/web/…,
        # and that 404 would misread as a dead post.
        captured = self._fetch_post(f"status/{status_id}")
        if isinstance(captured, Classification):
            return captured.to_result()
        posts, walk_meta = self._walk_up(captured)
        return _render(captured, posts, walk_meta)

    def _fetch_post(self, api_path: str) -> dict | Classification:
        """One fxtwitter post payload, or the classified failure."""
        outcome = fetch_classified(self._transport, _API + api_path)
        if isinstance(outcome, FetchFailure):
            return outcome.classification
        try:
            payload = json.loads(outcome.text())
        except json.JSONDecodeError:
            return Classification(
                status=Status.BLOCKED, reason="fxtwitter returned unparseable JSON"
            )
        tweet = payload.get("tweet") if isinstance(payload, dict) else None
        if not isinstance(tweet, dict):
            return Classification(
                status=Status.BLOCKED, reason="fxtwitter response had no tweet object"
            )
        return tweet

    def _walk_up(self, captured: dict) -> tuple[list[dict], dict[str, str | int | None]]:
        """Follow parent pointers up the chain; record gaps and cap hits in meta.

        The ids already walked are remembered because fxtwitter will
        happily point a post at itself: without that, one such post spent
        the whole hop bound on 100 back-to-back requests.
        """
        posts = [captured]
        walk_meta: dict[str, str | int | None] = {}
        current = captured
        seen = {str(captured["id"])} if captured.get("id") is not None else set()
        while (parent_id := current.get("replying_to_status")) is not None:
            if str(parent_id) in seen:
                # A post naming itself (or an ancestor) as its parent: the
                # chain above is a loop, not more thread. Recorded like any
                # short walk — never presented as a complete chain.
                walk_meta["chain_incomplete"] = "true"
                walk_meta["chain_note"] = (
                    f"parent chain loops back to post {parent_id} after {len(posts)} post(s)"
                )
                break
            if len(posts) - 1 >= MAX_HOPS:
                # Cap hit: recorded in meta (and thereby the enrichment
                # frontmatter), never on user-facing surfaces.
                walk_meta["thread_cap_hit"] = "true"
                break
            seen.add(str(parent_id))
            screen = current.get("replying_to") or "i"
            self._pace(HOP_SLEEP)
            parent = self._fetch_post(f"{screen}/status/{parent_id}")
            if isinstance(parent, Classification):
                # Mid-walk fetch failure is mechanical: record the gap, keep
                # what we have — never silently present it as complete.
                walk_meta["chain_incomplete"] = "true"
                walk_meta["chain_note"] = (
                    f"parent fetch failed after {len(posts)} post(s): {parent.reason}"
                )
                break
            posts.append(parent)
            current = parent
        return posts, walk_meta


def _status_id(url: str) -> str | None:
    """The post's status id, or None for a non-status URL."""
    match = _STATUS_PATH_RE.search(urllib.parse.urlsplit(url).path)
    return match.group(1) if match else None


def _render(captured: dict, posts: list[dict], walk_meta: dict[str, str | int | None]) -> Result:
    """Assemble the Result: reading-order body, pooled media, attribution meta."""
    has_content = any(
        _text_of(post) or isinstance(post.get("quote"), dict) or _media_urls(post) for post in posts
    )
    body = "\n\n".join(_render_post(post) for post in reversed(posts))  # root -> captured
    author = captured.get("author") or {}
    meta: dict[str, str | int | None] = {
        "author": f"{author.get('name') or 'unknown'} (@{author.get('screen_name') or 'unknown'})",
        "tweeted": captured.get("created_at"),
        "via": "fxtwitter",
    }
    if len(posts) > 1:
        meta["thread_length"] = len(posts)
    meta.update(walk_meta)
    if not has_content:
        return Result(status=Status.MANUAL, meta=meta, reason="fxtwitter returned no text or media")
    # Media-only posts are DONE, not manual: the body stays a minimal
    # attributed record and the media stage fetches the files themselves —
    # under its 4-file cap and 10MB ceiling, which a big video meets as
    # `skipped — media exceeds 10MB ceiling`, charged to the media unit
    # with the size stated, rather than as a manual park on the post.
    media = [url for post in posts for url in _media_urls(post)]  # captured post's first
    return Result(status=Status.DONE, meta=meta, body=body, media=media)


def _render_post(post: dict) -> str:
    author = post.get("author") or {}
    lines = [f"@{author.get('screen_name') or 'unknown'} — {post.get('created_at') or 'undated'}"]
    text = _text_of(post)
    if text:
        lines.append("")
        lines.append(text)
    elif (note := _media_note(post)) is not None:
        lines.append("")
        lines.append(note)
    quote = post.get("quote")
    if isinstance(quote, dict):
        quote_author = quote.get("author") or {}
        quoted = f"Quoting @{quote_author.get('screen_name') or 'unknown'}: {_text_of(quote)}"
        lines.append("")
        lines.extend("> " + line if line else ">" for line in quoted.split("\n"))
    return "\n".join(lines)


def _text_of(post: dict) -> str:
    """The post's prose, or "" when the payload holds no prose at all.

    Long-form articles carry theirs under ``article``, with ``text`` empty
    and ``raw_text`` holding nothing but the shortlink to the article — a
    body that is only a shortlink is a pointer, not content, and must not
    ledger a post done on ~70 characters of URL.
    """
    text = post.get("text") or _article_text(post) or _raw_text(post)
    return "" if _SHORTLINK_ONLY_RE.fullmatch(text) else text


def _article_text(post: dict) -> str:
    article = post.get("article")
    if not isinstance(article, dict):
        return ""
    pieces = [str(article.get(field) or "").strip() for field in ("title", "preview_text")]
    pieces.append(_raw_text(post).strip())
    return "\n\n".join(piece for piece in pieces if piece)


def _raw_text(post: dict) -> str:
    raw_text = post.get("raw_text")
    return raw_text.get("text", "") if isinstance(raw_text, dict) else ""


def _media_entries(post: dict) -> list[dict]:
    """Every media object on a post, in post order, deduped by URL."""
    media = post.get("media")
    if not isinstance(media, dict):
        return []
    entries: list[dict] = []
    seen: set[str] = set()
    for key in _MEDIA_LISTS:
        for entry in media.get(key) or []:
            url = entry.get("url") if isinstance(entry, dict) else None
            if isinstance(url, str) and url and url not in seen:
                seen.add(url)
                entries.append(entry)
    return entries


def _media_urls(post: dict) -> list[str]:
    """The post's media URLs, photos and videos alike — the media stage's input."""
    return [entry["url"] for entry in _media_entries(post)]


def _media_note(post: dict) -> str | None:
    """The stand-in line for a post with media and no prose, or None.

    Named by what the payload actually holds: a video-only post rendered
    "(photo post)" would be the same false statement the manual park used
    to make, moved into the body.
    """
    entries = _media_entries(post)
    if not entries:
        return None
    types = {entry.get("type") for entry in entries}
    if types == {"photo"}:
        return "(photo post)"
    if types <= {"video", "gif"}:
        return "(video post)"
    return "(media post)"  # mixed, or a type this driver has not met
