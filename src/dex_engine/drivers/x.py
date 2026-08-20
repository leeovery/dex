"""The x driver (renamed from tweet): fxtwitter fetch + thread walk-up.

The chain above a captured post is context, not new first-class sources:
one enrichment file, one ledger entry. The walk follows fxtwitter parent
pointers bottom-to-top (cap 20 hops); storage is reading order — root
first, captured post last, each post attributed. Quoted posts stay inline
as blockquotes; promoting a quote is a harvest judgment. Chain media is
pooled, the captured post's first.

Incomplete chains are recorded, never silently presented as complete: a
parent fetch failing mid-walk sets ``chain_incomplete`` in meta with how
far the walk got. Walk-down is explicitly unsolved — backlog.
"""

import json
import urllib.parse

from dex_engine.pipeline.classify import (
    Classification,
    classify_connection,
    classify_http,
)
from dex_engine.pipeline.types import Kind, Result, Status, WorkUnit
from dex_engine.pipeline.urls import base_canonical, host_of

from .transport import Transport, urllib_transport

__all__ = ["XDriver"]

_API = "https://api.fxtwitter.com/"
_HOSTS = frozenset({"x.com", "twitter.com"})

# Thread walk-up cap: 20 parent hops above the captured post.
MAX_HOPS = 20


class XDriver:
    """Fetch one x.com/twitter.com post with its parent chain as context."""

    kind: Kind = Kind.X
    sleep: float = 4.0

    def __init__(self, *, transport: Transport = urllib_transport) -> None:
        """Wire the HTTP seam (fxtwitter is plain JSON over GET)."""
        self._transport = transport

    def matches(self, url: str) -> bool:
        """True for x.com and twitter.com hosts."""
        return host_of(url) in _HOSTS

    def canonical(self, url: str) -> str:
        """The generic canonical form; x.com and twitter.com hash separately (as before)."""
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Result:
        """Fetch the captured post, walk its parent chain, render reading order."""
        path = urllib.parse.urlsplit(unit.url).path.lstrip("/")
        if "/status/" not in f"/{path}":
            return Result(
                status=Status.MANUAL,
                meta={},
                reason="not a status URL — fxtwitter serves posts only",
            )
        captured = self._fetch_post(path)
        if isinstance(captured, Classification):
            return Result(status=captured.status, meta={}, reason=captured.reason)
        posts, walk_meta = self._walk_up(captured)
        return _render(captured, posts, walk_meta)

    def _fetch_post(self, api_path: str) -> dict | Classification:
        """One fxtwitter post payload, or the classified failure."""
        try:
            response = self._transport(_API + api_path)
        except OSError as e:
            return classify_connection(e)
        if not response.ok:
            return classify_http(response.status)
        try:
            payload = json.loads(response.text())
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
        """Follow parent pointers up the chain; record gaps and cap hits in meta."""
        posts = [captured]
        walk_meta: dict[str, str | int | None] = {}
        current = captured
        while (parent_id := current.get("replying_to_status")) is not None:
            if len(posts) - 1 >= MAX_HOPS:
                # Cap hit: recorded in meta (and thereby the enrichment
                # frontmatter), never on user-facing surfaces.
                walk_meta["thread_cap_hit"] = "true"
                break
            screen = current.get("replying_to") or "i"
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


def _render(captured: dict, posts: list[dict], walk_meta: dict[str, str | int | None]) -> Result:
    """Assemble the Result: reading-order body, pooled media, attribution meta."""
    has_content = any(
        _text_of(post) or isinstance(post.get("quote"), dict) or _photo_urls(post) for post in posts
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
    # Photo-only posts are DONE, not manual: the body stays a minimal
    # attributed record and the media stage fetches the photos themselves.
    media = [url for post in posts for url in _photo_urls(post)]  # captured post's first
    return Result(status=Status.DONE, meta=meta, body=body, media=media)


def _render_post(post: dict) -> str:
    author = post.get("author") or {}
    lines = [f"@{author.get('screen_name') or 'unknown'} — {post.get('created_at') or 'undated'}"]
    text = _text_of(post)
    if text:
        lines.append("")
        lines.append(text)
    elif _photo_urls(post):
        lines.append("")
        lines.append("(photo post)")
    quote = post.get("quote")
    if isinstance(quote, dict):
        quote_author = quote.get("author") or {}
        quoted = f"Quoting @{quote_author.get('screen_name') or 'unknown'}: {_text_of(quote)}"
        lines.append("")
        lines.extend("> " + line if line else ">" for line in quoted.split("\n"))
    return "\n".join(lines)


def _text_of(post: dict) -> str:
    raw_text = post.get("raw_text")
    fallback = raw_text.get("text", "") if isinstance(raw_text, dict) else ""
    return post.get("text") or fallback or ""


def _photo_urls(post: dict) -> list[str]:
    photos = (post.get("media") or {}).get("photos") or []
    return [photo["url"] for photo in photos if isinstance(photo, dict) and photo.get("url")]
