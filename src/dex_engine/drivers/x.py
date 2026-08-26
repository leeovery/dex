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

Long-form articles carry their prose under ``article``, and where a post
has one the article IS the post: its ``content.blocks[]`` render as the
body, falling back to ``preview_text`` where fxtwitter relays no blocks.
The announcement's own ``text`` is never the answer — it is a link to the
article and nothing more, and reading it first threw whole articles away.

A body that is nothing but a link is not content in any spelling: not
x's own ``t.co``, and not the expanded ``x.com/i/article/<id>`` fxtwitter
actually hands over. Such a post parks for judgment rather than ledgering
done on a URL. A shared article URL parks too, and says why: an article
id is not a status id, and fxtwitter will not resolve one to the post
that announced it.
"""

import json
import re
import time
import urllib.parse
from collections.abc import Callable

from dex_engine.pipeline.classify import Classification
from dex_engine.pipeline.types import Content, Kind, Outcome, Status, Unusable, WorkUnit
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

# A body that is nothing but a link is a pointer to content, never the
# content itself. x's own t.co is one spelling of that and not the one
# that bit: fxtwitter EXPANDS shortlinks, so an article announcement
# arrives with `text` already reading `https://x.com/i/article/<id>` —
# past a t.co-only guard, ledgered done, storing a two-line enrichment
# file for an article whose whole body sat unread in the same response.
_LINK_ONLY_RE = re.compile(r"\s*https?://\S+\s*")

# fxtwitter serves posts; an article id is not a status id and does not
# resolve to the post that announced it (the endpoint 404s), so a shared
# article URL is a park with instructions, not a fetch.
_ARTICLE_PATH_RE = re.compile(r"/article/\d+")

# draft.js block types, as fxtwitter relays them under
# `article.content.blocks[]`. Anything unlisted renders as a paragraph:
# a block type we have not met is still prose, and dropping it would lose
# the very content this driver exists to keep.
_BLOCK_PREFIX = {
    "header-one": "# ",
    "header-two": "## ",
    "header-three": "### ",
    "header-four": "#### ",
    "header-five": "##### ",
    "header-six": "###### ",
    "unordered-list-item": "- ",
    "blockquote": "> ",
    "code-block": "    ",
}

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

    def fetch(self, unit: WorkUnit) -> Outcome:
        """Fetch the captured post, walk its parent chain, render reading order."""
        status_id = _status_id(unit.url)
        if status_id is None:
            if _ARTICLE_PATH_RE.search(urllib.parse.urlsplit(unit.url).path):
                return Unusable(
                    evidence="an x article URL — fxtwitter serves posts, and an article id "
                    "does not resolve to the post that announced it; capture that post instead"
                )
            return Unusable(evidence="not a status URL — fxtwitter serves posts only")
        # Bare status/<id> is the one fxtwitter path that serves every share
        # shape: the API ignores a username segment but 404s on /i/web/…,
        # and that 404 would misread as a dead post.
        captured = self._fetch_post(f"status/{status_id}")
        if isinstance(captured, Classification):
            return captured.to_outcome()
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


def _render(
    captured: dict, posts: list[dict], walk_meta: dict[str, str | int | None]
) -> Content | Unusable:
    """Assemble the outcome: reading-order body, pooled media, attribution meta."""
    has_content = any(
        _text_of(post) or isinstance(post.get("quote"), dict) or _media_urls(post) for post in posts
    )
    if not has_content:
        return Unusable(evidence="fxtwitter returned no text or media")
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
    # Media-only posts are content, not unusable: the body stays a minimal
    # attributed record and the media stage fetches the files themselves —
    # under its 4-file cap and 10MB ceiling, which a big video meets as
    # `skipped — media exceeds 10MB ceiling`, charged to the media unit
    # with the size stated, rather than as a park on the post.
    media = [url for post in posts for url in _media_urls(post)]  # captured post's first
    return Content(meta=meta, body=body, media=media)


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

    Where a post carries an ``article``, the article IS the post: an
    announcement's own ``text`` is a link to it and nothing else worth
    keeping. Reading ``text`` first therefore threw the body away — the
    payload's ``article`` was never consulted, and the file written held
    the bare link. Asking for the article first costs nothing: a post
    without one has no article text to find.
    """
    article = _article_text(post)
    if article:
        return article
    text = post.get("text") or _raw_text(post)
    return "" if _LINK_ONLY_RE.fullmatch(text) else text


def _article_text(post: dict) -> str:
    """A long-form article as markdown: title, body, and the link to it.

    The body is ``content.blocks[]`` where fxtwitter relays it — the whole
    article, tens of thousands of characters of it. Where it does not, the
    ``preview_text`` is all there is and stands in for it. The trailing
    link is kept either way: harvest reads links out of stored bodies, and
    on a preview it is the only pointer at the rest.
    """
    article = post.get("article")
    if not isinstance(article, dict):
        return ""
    title = str(article.get("title") or "").strip()
    body = _article_blocks(article) or str(article.get("preview_text") or "").strip()
    pieces = [f"# {title}" if title else "", body, _raw_text(post).strip()]
    return "\n\n".join(piece for piece in pieces if piece)


def _article_blocks(article: dict) -> str:
    """``content.blocks[]`` rendered as markdown, or "" when there are none."""
    content = article.get("content")
    blocks = content.get("blocks") if isinstance(content, dict) else None
    if not isinstance(blocks, list):
        return ""
    lines: list[str] = []
    ordinal = 0
    for block in blocks:
        text = str(block.get("text") or "").strip() if isinstance(block, dict) else ""
        kind = str(block.get("type") or "") if isinstance(block, dict) else ""
        if not text:
            ordinal = 0  # a blank block ends whatever list was running
            continue
        if kind == "ordered-list-item":
            ordinal += 1
            lines.append(f"{ordinal}. {text}")
            continue
        ordinal = 0
        lines.append(_BLOCK_PREFIX.get(kind, "") + text)
    return "\n\n".join(lines)


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
