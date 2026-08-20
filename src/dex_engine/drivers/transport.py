"""The drivers' HTTP seam: one urllib GET/HEAD with a browser UA (§5).

Every driver takes a :data:`Transport` in its constructor so tests are
hermetic; :func:`urllib_transport` is the one real implementation. It
returns an :class:`HttpResponse` for *any* HTTP-level response — 4xx/5xx
included, so callers can route status codes through the central classifier —
and lets connection-level failures (DNS, refused, timeout) propagate as
``OSError`` for ``classify_connection``.

The browser UA is deliberate (§5): the motivating incident was Cloudflare
challenging trafilatura's own fetch client; urllib with a browser UA avoids
the block outright more often than not.
"""

import contextlib
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "BROWSER_UA",
    "DEFAULT_TIMEOUT",
    "HttpResponse",
    "Transport",
    "urllib_transport",
]

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpResponse:
    """One HTTP response: status, media type, raw body."""

    status: int
    content_type: str  # lowercased media type, parameters stripped ("text/html")
    body: bytes

    @property
    def ok(self) -> bool:
        """True for a 2xx response."""
        return 200 <= self.status < 300  # noqa: PLR2004 — the HTTP success range is self-naming

    def text(self) -> str:
        """The body decoded as UTF-8, undecodable bytes replaced."""
        return self.body.decode("utf-8", "replace")


class Transport(Protocol):
    """The injected fetch seam: GET (or HEAD) one URL."""

    def __call__(self, url: str, *, method: str = "GET") -> HttpResponse:
        """Fetch ``url``; HTTP failures return, connection failures raise ``OSError``."""
        ...


def _media_type(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def urllib_transport(url: str, *, method: str = "GET") -> HttpResponse:
    """Fetch ``url`` over urllib with the browser UA.

    Args:
        url: An absolute http(s) URL.
        method: ``GET`` (default) or ``HEAD``.

    Returns:
        The response — 4xx/5xx included, never raised.

    Raises:
        ValueError: ``url`` is not http(s).
        OSError: Connection-level failure (DNS, refused, reset, timeout);
            ``urllib.error.URLError`` is an ``OSError`` subclass.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"transport fetches http(s) URLs only, got {url!r}")
    request = urllib.request.Request(  # noqa: S310 — scheme checked above
        url, headers={"User-Agent": BROWSER_UA}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:  # noqa: S310
            body = b"" if method == "HEAD" else response.read()
            return HttpResponse(
                status=response.status,
                content_type=_media_type(response.headers.get("Content-Type")),
                body=body,
            )
    except urllib.error.HTTPError as e:
        body = b""
        with contextlib.suppress(OSError, ValueError):
            body = e.read()
        return HttpResponse(
            status=e.code,
            content_type=_media_type(e.headers.get("Content-Type") if e.headers else None),
            body=body,
        )
