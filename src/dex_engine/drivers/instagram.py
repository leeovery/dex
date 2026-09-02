"""The instagram driver: instagram.com's ``og:`` metadata + an embed-proxy probe walk.

A post's identity is its shortcode: ``/p/<code>/``, ``/reel/<code>/``,
``/tv/<code>/`` and the username-prefixed ``/<user>/p/<code>/`` and
``/<user>/reel/<code>/`` are one post shared five ways, and all of them
canonicalize to one code-keyed form — the same rule ``XDriver`` applies to
X's status-URL spellings. Two shortcode grammars exist in the wild: the
ordinary 11-character code and the ~39-character token a restricted post's
share yields. Both are owned; the token resolves to its short code at
fetch time, off the page's own ``rel="canonical"`` link, because
canonicalization is offline and keys the ledger before any network.

Two anonymous sources, split by what each is good at. instagram.com
itself, asked as a link-preview bot, answers a public post with the full
``og:`` set — the untruncated caption with its line breaks intact, the
author's display name and handle, and the post date — and no usable media.
The embed proxy serves media and nothing else worth having: its
``/images/<code>/<n>`` and ``/videos/<code>/<n>`` endpoints redirect any
caller to the signed CDN URL, while its own page endpoint truncates
captions at ~255 characters. Neither fetch involves an account: both are
what an anonymous link preview does.

Media is enumerated by probing those endpoints one index at a time,
because nothing that claims to describe a post's media tells the truth
about it. A video transcribes and its binary is dropped; images are kept
in either shape of post — a stills-only carousel, and the stills standing
beside a carousel's video — as proxy URLs, never the signed CDN URLs they
redirect to: those expire, and a ledger identity must not.

The driver owns the whole instagram.com host, post URLs and everything
else alike, because the alternative is the web catch-all fetching
Instagram's JS app shell and ledgering garbage. What it cannot reach it
says plainly: a private or unavailable post, a story, a profile root each
park with the shape named and the way through stated.
"""

import html
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

from dex_engine.pipeline.types import (
    Content,
    Kind,
    Need,
    NeedsCapability,
    Outcome,
    Refused,
    Unusable,
    WorkUnit,
)
from dex_engine.pipeline.urls import base_canonical, host_of

from .fetch import FetchFailure, fetch_classified
from .transport import Transport, bot_transport

__all__ = ["DEFAULT_BASE_URL", "MAX_MEDIA_PROBES", "PROBE_SLEEP", "InstagramDriver"]

# The embed proxy the media endpoints are fetched from. Configurable per
# instance because the lineage decays: upstream InstaFix was archived and
# ddinstagram.com is already dead, so the surviving hosts run unmaintained
# code and the criterion for one is media-endpoint reliability alone.
DEFAULT_BASE_URL = "https://uuinstagram.com"

_HOST = "instagram.com"  # host_of strips the www. and m. spellings

# Every post-path shape in the wild, with or without the username segment
# ahead of it. The code segment carries no length rule on purpose: an
# ordinary share yields an 11-character shortcode and a restricted post's
# share yields a ~39-character token, and a grammar that fits one drops the
# other onto the web catch-all.
_POST_PATH_RE = re.compile(r"^/(?:[^/]+/)?(?:p|reel|tv)/([A-Za-z0-9_-]+)")

_STORIES_PREFIX = "/stories/"

# Single-segment paths that are not somebody's profile.
_RESERVED_ROOTS = frozenset({"explore", "reels", "accounts", "directory", "about", "legal"})

# The og: set, either attribute order. The value is deliberately allowed to
# span newlines: a caption's own line breaks sit raw inside the og:title
# attribute, and a pattern that excludes \r\n reads back its first line and
# throws the rest of the caption away.
_OG_TAG_RES = (
    re.compile(r'<meta[^>]+property="(?P<key>og:[^"]+)"[^>]+content="(?P<value>[^"]*)"'),
    re.compile(r'<meta[^>]+content="(?P<value>[^"]*)"[^>]+property="(?P<key>og:[^"]+)"'),
)

# `<display name> on Instagram: "<caption>"`. The caption runs to the LAST
# quote: entity-decoding turns the post's own &quot; into real quotes
# inside it.
_OG_TITLE_RE = re.compile(r'^(?P<name>.*?) on Instagram: "(?P<caption>.*)"', re.DOTALL)

# `60M likes, 4M comments - world_record_egg on January 4, 2019: "…"`. The
# engagement counts lead and are never captured: a snapshot of a number is
# noise, and the parse that finds the handle is where it gets discarded.
_OG_DESCRIPTION_RE = re.compile(
    r"(?P<handle>[A-Za-z0-9._]+) on (?P<posted>[A-Z][a-z]+ \d{1,2}, \d{4}):"
)

_CANONICAL_LINK_RES = (
    re.compile(r'<link[^>]+rel="canonical"[^>]+href="(?P<href>[^"]+)"', re.IGNORECASE),
    re.compile(r'<link[^>]+href="(?P<href>[^"]+)"[^>]+rel="canonical"', re.IGNORECASE),
)

# A private or unavailable post serves this bare shell and nothing else: a
# public post carries the same <title> over the full og: set, so the title
# alone says nothing — it is the fingerprint only where the og: set is
# absent.
_SHELL_TITLE_RE = re.compile(r"<title>\s*Instagram\s*</title>", re.IGNORECASE)

# The probe walk runs to the media stage's pooled cap plus one — carousel
# slides are the post itself, bounded by the pooled safety backstop, never
# the tight embedded-extras cap — so a post carrying more media than the
# pipeline will fetch shows the truncation at the cap instead of looking
# complete. Kept equal to MEDIA_MAX_FILES_POOLED + 1 by a driver test, not
# an import: the run layer imports the drivers, so this module cannot
# import it back. The walk still ends at the first empty body, so a real
# carousel spends one probe per slide, not the whole bound.
MAX_MEDIA_PROBES = 101

# Pacing between probes, the X driver's hop courtesy for the same reason:
# the driver's own politeness (`sleep`) is spent between UNITS, and the walk
# is one request per slide inside one — unpaced, every carousel goes out as
# back-to-back calls to an unmaintained free host.
PROBE_SLEEP = 1.0

_PRIVATE_EVIDENCE = "private or unavailable; screenshot it and share that if you want it"
_STORY_EVIDENCE = (
    "an Instagram story — stories are login-walled and expire; "
    "screenshot it and share that if you want it"
)
_GARBAGE_EVIDENCE = (
    "instagram.com answered with neither post metadata nor its own app shell — "
    "an error page from somewhere in between"
)
_PARK_REASON = "post resolved — video awaits transcription"


class _Item(Enum):
    """What one media index turned out to hold."""

    VIDEO = auto()
    IMAGE = auto()


@dataclass(frozen=True, slots=True, kw_only=True)
class _Post:
    """What a public post's ``og:`` set says about it."""

    handle: str
    display_name: str
    posted: str
    caption: str
    code: str  # the shortcode rel="canonical" names — a share token resolved


class InstagramDriver:
    """Fetch one Instagram post: caption from instagram.com, media from the proxy."""

    kind: Kind = Kind.INSTAGRAM
    sleep: float = 4.0

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: Transport = bot_transport,
        pace: Callable[[float], None] = time.sleep,
    ) -> None:
        """Wire the media proxy, the HTTP seam, and the probe walk's pacing.

        Args:
            base_url: The embed proxy serving ``/images`` and ``/videos``.
                Public because it is configured identity, not an
                implementation secret: the run report and every fetched
                post's meta name the host that served the media.
            transport: The HTTP seam. The bot UA is the default because
                both page shapes this driver reads are served to link
                preview bots and withheld from browsers; injected so tests
                are hermetic.
            pace: The probe walk's own pacing seam — ``sleep`` names the
                driver's per-unit politeness, which the run layer spends.
                Injected so tests are hermetic.
        """
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self._pace = pace

    def matches(self, url: str) -> bool:
        """True for the whole instagram.com host — posts and everything else.

        Whole-host deliberately: an Instagram URL this driver declined
        would fall to the web catch-all, which fetches the JS app shell and
        ledgers it as content.
        """
        return host_of(url) == _HOST

    def canonical(self, url: str) -> str:
        """Post URLs collapse to ``https://www.instagram.com/p/<code>/`` — the code is the identity.

        Offline, like every canonicalization: a share token stays in place
        here and resolves to its short code at fetch time.
        """
        code = _shortcode(url)
        return _post_url(code) if code is not None else base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Outcome:
        """Read the post's metadata off instagram.com, then probe the proxy for its media."""
        requested = _shortcode(unit.url)
        if requested is None:
            return _non_post(urllib.parse.urlsplit(unit.url).path)
        post = self._post(requested)
        if not isinstance(post, _Post):
            return post
        items = self._probe_media(post.code)
        if isinstance(items, Refused):
            return items
        return self._assemble(post, requested=requested, items=items)

    def _post(self, requested: str) -> "_Post | Outcome":
        """One post's ``og:`` metadata, or the park the landed page earns."""
        fetched = fetch_classified(self._transport, _post_url(requested))
        if isinstance(fetched, FetchFailure):
            return fetched.classification.to_outcome()
        return _parse_post(fetched.text(), requested=requested)

    def _probe_media(self, code: str) -> "list[_Item] | Refused":
        """Type the post's media, one index at a time, or refuse the unit.

        A failed probe fails the whole unit rather than yielding what it
        found: the caption is cheap to refetch on the retry, and silently
        storing a carousel minus the images the walk never reached is the
        loss no later stage can see.
        """
        items: list[_Item] = []
        for index in range(1, MAX_MEDIA_PROBES + 1):
            if index > 1:
                self._pace(PROBE_SLEEP)
            # One byte of body is the whole question: out-of-range answers
            # 200 with a zero-byte body, never a 404, so status codes cannot
            # terminate the walk and only the body can.
            fetched = fetch_classified(
                self._transport, self._media_url("videos", code, index), limit=0
            )
            if isinstance(fetched, FetchFailure):
                return self._probe_refusal(index, fetched.detail)
            if not fetched.body:
                return items
            item = _typed_item(fetched.content_type)
            if item is None:
                return self._probe_refusal(index, f"served {fetched.content_type or 'no type'}")
            items.append(item)
        return items

    def _probe_refusal(self, index: int, detail: str) -> Refused:
        return Refused(
            evidence=(
                f"the media proxy {host_of(self.base_url)} failed on media {index} ({detail}) — "
                "point instagram_base_url at another host if this one has died"
            )
        )

    def _assemble(self, post: _Post, *, requested: str, items: list[_Item]) -> Outcome:
        """One outcome for the post: the transcribe park, content, or the honest park."""
        meta: dict[str, str | int | None] = {
            "author": f"{post.display_name or 'unknown'} (@{post.handle or 'unknown'})",
            "posted": post.posted or None,
            "shortcode": post.code,
            "share_token": requested if requested != post.code else None,
            "canonical": _post_url(post.code),
            # NOT `via`: that key is the transcript-provenance stamp, and a
            # park carrying it would tell the drain this body already holds
            # a transcript.
            "media_via": host_of(self.base_url),
        }
        videos = _indexes(items, _Item.VIDEO)
        images = _indexes(items, _Item.IMAGE)
        image_urls = [self._media_url("images", post.code, index) for index in images]
        if videos:
            # `enclosure` is the key the transcribe acquisition reads back
            # out of the park file's frontmatter — and the condition under
            # which the run layer writes that file at all, so a caption-less
            # reel spelling it any other way would park with nothing on disk
            # and loop manual on its absence.
            meta["enclosure"] = self._media_url("videos", post.code, videos[0])
            return NeedsCapability(
                need=Need.TRANSCRIBE,
                meta=meta,
                body=_attributed(post) if post.caption else None,
                media=image_urls,
                reason=_PARK_REASON,
            )
        if not post.caption and not images:
            return Unusable(evidence="no text or media")
        return Content(
            meta=meta,
            body=_attributed(post, stand_in="(image post)" if images else None),
            media=image_urls,
        )

    def _media_url(self, family: str, code: str, index: int) -> str:
        return f"{self.base_url}/{family}/{code}/{index}"


def _shortcode(url: str) -> str | None:
    """The post's shortcode (or share token), or None for a non-post URL."""
    match = _POST_PATH_RE.match(urllib.parse.urlsplit(url).path)
    return match.group(1) if match else None


def _post_url(code: str) -> str:
    return f"https://www.instagram.com/p/{code}/"


def _non_post(path: str) -> Unusable:
    """The park for an Instagram URL addressing no single post."""
    if path.startswith(_STORIES_PREFIX):
        return Unusable(evidence=_STORY_EVIDENCE)
    segments = [segment for segment in path.split("/") if segment]
    shape = (
        f"the profile @{segments[0]}"
        if len(segments) == 1 and segments[0].lower() not in _RESERVED_ROOTS
        else f"the page at /{'/'.join(segments)}"
    )
    return Unusable(evidence=f"not an Instagram post — this URL addresses {shape}", rescuable=False)


def _parse_post(text: str, *, requested: str) -> "_Post | Outcome":
    """Read a landed instagram.com page: the post, or what its absence means."""
    tags = _og_tags(text)
    if not tags:
        # The public/private signal is the page itself — but only Instagram's
        # own bare shell says "private". Any other og:-less body is a proxy
        # or CDN error page, which is the world misbehaving and retryable;
        # condemning it as private would be a claim about content nobody
        # checked.
        if _SHELL_TITLE_RE.search(text):
            return Unusable(evidence=_PRIVATE_EVIDENCE)
        return Refused(evidence=_GARBAGE_EVIDENCE)
    title = _OG_TITLE_RE.match(tags.get("og:title", ""))
    description = _OG_DESCRIPTION_RE.search(tags.get("og:description", ""))
    return _Post(
        handle=description["handle"] if description else "",
        display_name=title["name"] if title else "",
        posted=description["posted"] if description else "",
        caption=title["caption"] if title else "",
        code=_canonical_code(text) or requested,
    )


def _og_tags(text: str) -> dict[str, str]:
    """Every ``og:`` meta tag on the page, entities decoded."""
    tags: dict[str, str] = {}
    for pattern in _OG_TAG_RES:
        for match in pattern.finditer(text):
            tags.setdefault(match["key"].lower(), html.unescape(match["value"]))
    return tags


def _canonical_code(text: str) -> str | None:
    """The shortcode the page's own ``rel="canonical"`` link names, or None.

    This is what resolves a share token: the token addresses the post and
    the canonical link spells it as the short code the media endpoints
    know.
    """
    for pattern in _CANONICAL_LINK_RES:
        match = pattern.search(text)
        if match:
            return _shortcode(html.unescape(match["href"]))
    return None


def _typed_item(content_type: str) -> _Item | None:
    """What a probe's content type says the item is, or None for a non-media answer.

    The type the CDN serves is the only truthful signal about a post's
    media: the proxy's own page metadata types an all-image carousel as
    ``og:video`` over a URL that redirects to a JPEG.
    """
    if content_type.startswith("video/"):
        return _Item.VIDEO
    if content_type.startswith("image/"):
        return _Item.IMAGE
    return None


def _indexes(items: list[_Item], wanted: _Item) -> list[int]:
    """The 1-based media indexes holding ``wanted`` — the proxy counts from 1."""
    return [index for index, item in enumerate(items, 1) if item is wanted]


def _attributed(post: _Post, *, stand_in: str | None = None) -> str:
    """The caption as an attributed body, ``stand_in`` standing for an empty one.

    A media post whose caption is empty is content, not a park: the body
    stays a minimal attributed record and the media carries the knowledge.
    """
    header = f"@{post.handle or 'unknown'} — {post.posted or 'undated'}"
    tail = post.caption or stand_in
    return f"{header}\n\n{tail}" if tail else header
