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
An article is more than its prose blocks: an ``atomic`` block renders from
the ``content.entityMap`` entry it names — a code listing verbatim, a rule,
an embedded post as its status link, a figure as an image marker where it
sat — and a link keeps its target inline, because harvest reads links out
of the stored body. The figures themselves, and the cover, join the post's
media pool in reading order.

A body that is nothing but a link is not content in any spelling: not
x's own ``t.co``, and not the expanded ``x.com/i/article/<id>`` fxtwitter
actually hands over. Such a post parks for judgment rather than ledgering
done on a URL. A shared article URL parks too, and says why: an article
id is not a status id, and fxtwitter will not resolve one to the post
that announced it.
"""

import itertools
import json
import re
import time
import urllib.parse
from collections.abc import Callable, Iterator

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
            return _status_url(status_id)
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


def _status_url(status_id: str) -> str:
    return f"https://x.com/i/status/{status_id}"


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
    blocks, entities = _article_content(article)
    images = _article_images(article)
    lines: list[str] = []
    ordinal = 0
    for block in _code_runs(blocks):
        kind = str(block.get("type") or "")
        text = _block_markdown(block, kind, entities, images)
        if not text.strip():
            ordinal = 0  # a blank block ends whatever list was running
            continue
        if kind == "ordered-list-item":
            ordinal += 1
            prefix = f"{ordinal}. "
        else:
            ordinal = 0
            prefix = _BLOCK_PREFIX.get(kind, "")
        lines.append(_prefixed(kind, prefix, text))
    return "\n\n".join(lines)


def _prefixed(kind: str, prefix: str, text: str) -> str:
    """A block's markdown under its prefix, carried onto every line the text breaks to.

    A block's text can hold line breaks, and prefixed at its head alone a
    quote's second paragraph falls out of the quote and a list item's
    continuation breaks the list. So every quote line carries the marker
    (a blank one bare, keeping the quote whole) and a list item's later
    lines sit indented under it.
    """
    lines = text.split("\n")
    if kind == "blockquote":
        return "\n".join(prefix + line if line.strip() else prefix.rstrip() for line in lines)
    if kind.endswith("list-item"):
        indent = " " * len(prefix)
        continuation = (indent + line if line.strip() else "" for line in lines[1:])
        return "\n".join([prefix + lines[0], *continuation])
    return prefix + text


def _article_content(article: dict) -> tuple[list[dict], dict[str, dict]]:
    """The article's draft.js blocks and its entities, keyed as a range names them.

    fxtwitter relays ``entityMap`` as a list of ``{"key", "value"}`` pairs,
    and a range's integer ``key`` names the pair whose ``key`` string
    matches — never a list position: a real article's first pair is "45".
    """
    content = article.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("blocks"), list):
        return [], {}
    pairs = content.get("entityMap")
    entities = {
        str(pair.get("key")): pair["value"]
        for pair in (pairs if isinstance(pairs, list) else [])
        if isinstance(pair, dict) and isinstance(pair.get("value"), dict)
    }
    return [block for block in content["blocks"] if isinstance(block, dict)], entities


def _code_runs(blocks: list[dict]) -> Iterator[dict]:
    """The blocks, each run of ``code-block`` lines merged into one block.

    draft.js keeps a listing one line to a block and draws a run of them as
    one ``<pre>``; rendered a block at a time, every line of it would stand
    as a listing of its own.
    """
    for is_code, run in itertools.groupby(
        blocks, key=lambda block: block.get("type") == "code-block"
    ):
        if is_code:
            lines = [str(line.get("text") or "") for line in run]
            yield {"type": "code-block", "text": "\n".join(lines)}
        else:
            yield from run


def _block_markdown(
    block: dict, kind: str, entities: dict[str, dict], images: dict[str, str]
) -> str:
    """One block as markdown, ahead of any heading, list or quote prefix."""
    match kind:
        case "atomic":
            return _atomic_markdown(_atomic_entity(block, entities), images)
        case "code-block":
            # Verbatim, never stripped: a listing's indentation is its meaning.
            return f"```\n{block['text']}\n```" if block["text"].strip() else ""
        case _:
            return _linked_text(block, entities).strip()


def _atomic_markdown(entity: dict, images: dict[str, str]) -> str:
    """What an ``atomic`` block shows, or "" for an entity this driver cannot draw.

    Its own text is a lone space; what it holds lives in its one entity.
    A figure keeps its URL in the marker: that is the URL the media pool
    fetches, so the marker and the download are one unit, never a second
    one for harvest to promote.
    """
    data = entity.get("data")
    data = data if isinstance(data, dict) else {}
    match entity.get("type"):
        case "MARKDOWN":
            return str(data.get("markdown") or "")
        case "DIVIDER":
            return "---"
        case "TWEET":
            tweet_id = data.get("tweetId")
            return _status_url(str(tweet_id)) if tweet_id else ""
        case "MEDIA":
            return "\n".join(f"![figure]({url})" for url in _figures(entity, images))
        case _:
            return ""


def _linked_text(block: dict, entities: dict[str, dict]) -> str:
    """The block's text with each range naming a URL spelled as a markdown link.

    Ranges count UTF-16 code units, as JavaScript strings do, and Python
    counts code points: every emoji ahead of a range shifts it one unit
    further, so both ends convert before they slice.
    """
    text = str(block.get("text") or "")
    links = sorted(
        (span, url)
        for entity_range in _ranges(block)
        if (url := _link_url(_entity_of(entity_range, entities))) is not None
        and (span := _span(text, entity_range)) is not None
    )
    pieces: list[str] = []
    cursor = 0
    for (start, end), url in links:
        if start < cursor:
            continue  # inside the link before it: spliced twice, its text would repeat
        anchor = text[start:end]
        pieces += [text[cursor:start], url if anchor == url else f"[{anchor}]({url})"]
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _ranges(block: dict) -> list[dict]:
    ranges = block.get("entityRanges")
    return [r for r in ranges if isinstance(r, dict)] if isinstance(ranges, list) else []


def _entity_of(entity_range: dict, entities: dict[str, dict]) -> dict:
    return entities.get(str(entity_range.get("key")), {})


def _atomic_entity(block: dict, entities: dict[str, dict]) -> dict:
    """The entity an ``atomic`` block renders from — the one its lone range names."""
    ranges = _ranges(block)
    return _entity_of(ranges[0], entities) if ranges else {}


def _link_url(entity: dict) -> str | None:
    data = entity.get("data")
    url = data.get("url") if isinstance(data, dict) else None
    return url if isinstance(url, str) and url else None


def _span(text: str, entity_range: dict) -> tuple[int, int] | None:
    """A range's ``offset``/``length`` as code-point indexes into ``text``."""
    offset, length = entity_range.get("offset"), entity_range.get("length")
    if not isinstance(offset, int) or not isinstance(length, int) or offset < 0 or length <= 0:
        return None
    return _code_points(text, offset), _code_points(text, offset + length)


def _code_points(text: str, units: int) -> int:
    """How many characters of ``text`` its first ``units`` UTF-16 code units hold."""
    return len(text.encode("utf-16-le")[: 2 * units].decode("utf-16-le", errors="ignore"))


def _figures(entity: dict, images: dict[str, str]) -> list[str]:
    """The image URLs a MEDIA entity places, in its own order."""
    data = entity.get("data")
    items = data.get("mediaItems") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    ids = [str(item["mediaId"]) for item in items if isinstance(item, dict) and "mediaId" in item]
    return [images[media_id] for media_id in ids if media_id in images]


def _article_images(article: dict) -> dict[str, str]:
    """Every image ``media_entities`` lists, media id to URL."""
    listed = article.get("media_entities")
    return {
        str(media.get("media_id")): url
        for media in (listed if isinstance(listed, list) else [])
        if isinstance(media, dict) and (url := _image_url(media)) is not None
    }


def _image_url(media: object) -> str | None:
    info = media.get("media_info") if isinstance(media, dict) else None
    url = info.get("original_img_url") if isinstance(info, dict) else None
    return url if isinstance(url, str) and url else None


def _article_media(post: dict) -> list[str]:
    """An article's images in reading order: the cover, then each figure where it sits.

    ``media_entities`` lists them in no reading order — a figure's place is
    the atomic block naming it — and need not list the cover. An image no
    block places still pools, after the placed ones.
    """
    article = post.get("article")
    if not isinstance(article, dict):
        return []
    blocks, entities = _article_content(article)
    images = _article_images(article)
    placed = [
        url
        for block in blocks
        if block.get("type") == "atomic"
        for url in _figures(_atomic_entity(block, entities), images)
    ]
    ordered = [_image_url(article.get("cover_media")), *placed, *images.values()]
    return list(dict.fromkeys(url for url in ordered if url is not None))


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
    """The post's media URLs, photos, videos and article images alike — the media stage's input."""
    return [entry["url"] for entry in _media_entries(post)] + _article_media(post)


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
