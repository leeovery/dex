"""The drivers' HTTP seam: one urllib GET/HEAD with a browser UA.

Every driver takes a :data:`Transport` in its constructor so tests are
hermetic; :func:`urllib_transport` is the one real implementation. It
returns an :class:`HttpResponse` for *any* HTTP-level response — 4xx/5xx
included, so callers can route status codes through the central classifier —
and lets connection-level failures (DNS, refused, timeout) propagate as
``OSError`` for ``classify_connection``. ``http.client``'s own protocol
failures are normalized into that same ``OSError`` shape here, once, rather
than at each of the eight call sites (:func:`normalize_httplib_errors`).

The browser UA is deliberate: the motivating incident was Cloudflare
challenging trafilatura's own fetch client; urllib with a browser UA avoids
the block outright more often than not.
"""

import contextlib
import http.client
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "BROWSER_UA",
    "DEFAULT_TIMEOUT",
    "HttpResponse",
    "Transport",
    "normalize_httplib_errors",
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
    content_length: int | None = None  # declared Content-Length, when the server sent one

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


def _httplib_reason(exc: http.client.HTTPException) -> str:
    if isinstance(exc, http.client.IncompleteRead):
        expected = f", {exc.expected} more expected" if exc.expected else ""
        return f"truncated response body ({len(exc.partial)} bytes read{expected})"
    return f"{type(exc).__name__}: {exc}"


@contextlib.contextmanager
def normalize_httplib_errors() -> Iterator[None]:
    """Re-raise ``http.client`` protocol failures as ``ConnectionError``.

    ``http.client.HTTPException`` is NOT an ``OSError``: a server closing
    the socket mid-body — the routine failure mode for a 100MB podcast
    enclosure — raises ``IncompleteRead`` from ``response.read()``, which
    every caller's ``except OSError`` connection guard misses, so a
    truncated download lands as an engine bug with an issue filed instead
    of a retryable ``blocked``. Normalizing at the seam classifies it as
    the connection failure it is, for every transport caller at once.
    (``RemoteDisconnected`` already inherits ``ConnectionResetError`` and
    needs no help; it passes through this guard unchanged in meaning.)
    """
    try:
        yield
    except http.client.HTTPException as e:
        raise ConnectionError(_httplib_reason(e)) from e


def _media_type(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def _content_length(raw: str | None) -> int | None:
    if raw is None or not raw.strip().isdigit():
        return None
    return int(raw.strip())


def urllib_transport(url: str, *, method: str = "GET") -> HttpResponse:
    """Fetch ``url`` over urllib with the browser UA.

    Args:
        url: An absolute http(s) URL.
        method: ``GET`` (default) or ``HEAD``.

    Returns:
        The response — 4xx/5xx included, never raised.

    Raises:
        ValueError: ``url`` is not http(s).
        OSError: Connection-level failure (DNS, refused, reset, timeout, a
            truncated body); ``urllib.error.URLError`` is an ``OSError``
            subclass and ``http.client``'s family is normalized into one.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"transport fetches http(s) URLs only, got {url!r}")
    request = urllib.request.Request(  # noqa: S310 — scheme checked above
        url, headers={"User-Agent": BROWSER_UA}, method=method
    )
    with normalize_httplib_errors():
        try:
            with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:  # noqa: S310
                body = b"" if method == "HEAD" else response.read()
                return HttpResponse(
                    status=response.status,
                    content_type=_media_type(response.headers.get("Content-Type")),
                    body=body,
                    content_length=_content_length(response.headers.get("Content-Length")),
                )
        except urllib.error.HTTPError as e:
            body = b""
            # A truncated error page still classifies by its status: the
            # partial body is only detail, so the read failure is dropped.
            with contextlib.suppress(OSError, ValueError, http.client.HTTPException):
                body = e.read()
            return HttpResponse(
                status=e.code,
                content_type=_media_type(e.headers.get("Content-Type") if e.headers else None),
                body=body,
                content_length=_content_length(
                    e.headers.get("Content-Length") if e.headers else None
                ),
            )
