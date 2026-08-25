"""The drivers' audio-on-a-page seam: what audio a page carries as its own.

Two drivers read this one signal from opposite ends, and neither owns it:
the web driver asks it of every page it fetches, to decide whether the page
in hand is an episode page at all, and the podcast driver asks it again of
a page already routed, to resolve the enclosure the transcribe drain will
reach for.

It sits beside :mod:`dex_engine.drivers.transport` and
:mod:`dex_engine.drivers.gh`, for those modules' reason: a seam several
drivers share is not itself a driver, and a driver must never import
another driver. Like them it reaches only for the stdlib — nothing here
knows about work units or results.
"""

import html as html_lib
import re
from dataclasses import dataclass
from urllib.parse import urljoin

__all__ = ["AudioEnclosure", "audio_enclosure"]

# The two ways a page carries audio: the og:audio pointer and an <audio>
# element (its own src, or a nested <source>).
_OG_AUDIO_RES = (
    re.compile(
        r"<meta[^>]+(?:property|name)=[\"']og:audio[\"'][^>]+content=[\"']([^\"'\r\n]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"<meta[^>]+content=[\"']([^\"'\r\n]+)[\"'][^>]+(?:property|name)=[\"']og:audio[\"']",
        re.IGNORECASE,
    ),
)
_AUDIO_ELEMENT_RE = re.compile(
    r"<audio\b[^>]*>.*?</audio\s*>|<audio\b[^>]*/?>", re.IGNORECASE | re.DOTALL
)
_SRC_RE = re.compile(r"\bsrc=[\"']([^\"'\r\n]+)[\"']", re.IGNORECASE)


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioEnclosure:
    """Audio a page carries, and how strongly the page claims it.

    ``declared`` separates the two markups, because they say different
    things. ``og:audio`` is the publisher naming this audio as the page's
    own object — the machine-readable claim "this page IS the audio".  An
    ``<audio>`` element is only a player: an episode page uses one, and so
    does an article's read-aloud widget or an encyclopedia's media sample.
    """

    url: str
    declared: bool


def audio_enclosure(page: str, base_url: str) -> AudioEnclosure | None:
    """The audio this page carries as its own, or None.

    A bare link to an audio file is deliberately not audio the page
    carries: a post linking one mp3 is still a post.

    Args:
        page: The fetched HTML.
        base_url: The page's URL — relative sources resolve against it.

    Returns:
        The enclosure (absolute http(s) URL, and whether ``og:audio``
        declared it), or None.
    """
    for pattern in _OG_AUDIO_RES:
        match = pattern.search(page)
        if match:
            resolved = _absolute(match.group(1), base_url)
            if resolved is not None:
                return AudioEnclosure(url=resolved, declared=True)
    for element in _AUDIO_ELEMENT_RE.finditer(page):
        for source in _SRC_RE.finditer(element.group(0)):
            resolved = _absolute(source.group(1), base_url)
            if resolved is not None:
                return AudioEnclosure(url=resolved, declared=False)
    return None


def _absolute(value: str, base_url: str) -> str | None:
    candidate = urljoin(base_url, html_lib.unescape(value).strip())
    return candidate if candidate.startswith(("http://", "https://")) else None
