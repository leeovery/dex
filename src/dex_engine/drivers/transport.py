"""The drivers' HTTP seam: one urllib GET/HEAD, under a browser or a bot UA.

Every driver takes a :data:`Transport` in its constructor so tests are
hermetic; :func:`urllib_transport` and :func:`bot_transport` are the real
implementations — one fetch (:func:`_urllib_fetch`) under two User-Agents.
It returns an :class:`HttpResponse` for *any* HTTP-level response — 4xx/5xx
included, so callers can route status codes through the central classifier —
and lets connection-level failures (DNS, refused, timeout) propagate as
``OSError`` for ``classify_connection``; the fetch-and-classify pairing
itself lives once, in :mod:`dex_engine.drivers.fetch`. ``http.client``'s
own protocol failures are normalized into that same ``OSError`` shape
here, once, rather than at every caller
(:func:`normalize_httplib_errors`).
URLs the request line cannot carry are encoded for the wire here too
(:func:`_ascii_url`): ``http.client`` ascii-encodes the request line and
rejects the controls, space and DEL within it, so an accented Wikipedia
path, a CJK slug or an unencoded space in an href failed before a byte
left the machine — these URLs are fetchable, and this is where they get
fetched.

The browser UA is deliberate: the motivating incident was Cloudflare
challenging trafilatura's own fetch client; urllib with a browser UA avoids
the block outright more often than not.
"""

import contextlib
import http.client
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "BOT_UA",
    "BROWSER_UA",
    "DEFAULT_TIMEOUT",
    "HttpResponse",
    "Transport",
    "bot_transport",
    "normalize_httplib_errors",
    "urllib_transport",
]

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# instagram.com and the Instagram embed proxies serve their OpenGraph
# metadata to link-preview bots only: a browser UA is redirected to the JS
# app shell, which carries no metadata at all, so a driver reading those
# endpoints has to identify as a preview bot deliberately (verified against
# the live endpoints 2026-08-27).
BOT_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpResponse:
    """One HTTP response: status, media type, raw body.

    Under a caller's ``limit`` the body holds at most ``limit + 1`` bytes —
    the read stops one byte past the ceiling, so ``len(body) > limit`` is
    still exactly the over-ceiling test while the memory cost is bounded.
    """

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

    def __call__(self, url: str, *, method: str = "GET", limit: int | None = None) -> HttpResponse:
        """Fetch ``url``; HTTP failures return, connection failures raise ``OSError``.

        ``limit`` is the caller's body ceiling: the body is read to at most
        ``limit + 1`` bytes and the rest never buffered, so an over-ceiling
        download is refused while it arrives rather than after.
        """
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


# What each component may legally carry unescaped, plus ``%`` itself: an
# already-encoded URL must pass through untouched, never have its "%C3%A9"
# doubly escaped into "%25C3%25A9".
_PATH_SAFE = "/%:@!$&'()*+,;="
_QUERY_SAFE = _PATH_SAFE + "?"

# What ``http.client`` refuses to carry in the request line, whatever the
# encoding: the C0 controls, space and DEL. Every one of them is ASCII, so
# "is it ASCII already" is not the question the encoder needs answered.
_REQUEST_LINE_FORBIDDEN = re.compile(r"[\x00-\x20\x7f]")


def _needs_encoding(component: str) -> bool:
    """True when ``component`` cannot go on the wire as it stands."""
    return not component.isascii() or _REQUEST_LINE_FORBIDDEN.search(component) is not None


def _ascii_netloc(netloc: str) -> str:
    """Punycode the host of ``netloc``; userinfo percent-encoded, port kept."""
    userinfo, at, hostport = netloc.rpartition("@")
    host, colon, port = hostport.rpartition(":")
    if not port.isdigit():  # no port, or an IPv6 literal's own colons
        host, colon, port = hostport, "", ""
    if not host.isascii():
        host = host.encode("idna").decode("ascii")
    if _needs_encoding(userinfo):
        userinfo = urllib.parse.quote(userinfo, safe=":%")
    return f"{userinfo}{at}{host}{colon}{port}"


def _ascii_url(url: str) -> str:
    """Encode ``url`` for the wire: punycoded host, percent-encoded path/query.

    ``http.client`` puts the request line through ``encode("ascii")``, so a
    URL carrying any non-ASCII character — an accented Wikipedia path, an
    IDN host, a CJK slug — raised ``UnicodeEncodeError`` before a byte left
    the machine, and being a ``ValueError`` it escaped every caller's
    connection guard as a raw codec message. Nothing is wrong with these
    URLs; what they need is the encoding a browser applies for them.

    Being ASCII is not enough to skip that: ``http.client`` also rejects
    the C0 controls, space and DEL in the request line outright
    (:data:`_REQUEST_LINE_FORBIDDEN`), so the unencoded space that hrefs
    carry all the time raised ``InvalidURL`` and parked the item
    ``blocked`` — retried, and looked up in the wayback, on a condition no
    retry can change. ``quote`` already maps it to ``%20``.

    Raises:
        ValueError: The host is not encodable for DNS (an over-long or
            empty IDN label) — stated, never the codec's own words.
    """
    if not _needs_encoding(url):
        return url  # the overwhelming majority, byte-for-byte as asked
    parts = urllib.parse.urlsplit(url)
    try:
        netloc = _ascii_netloc(parts.netloc)
    except UnicodeError as e:
        raise ValueError(f"host of {url!r} is not encodable for DNS: {e}") from e
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            netloc,
            urllib.parse.quote(parts.path, safe=_PATH_SAFE),
            urllib.parse.quote(parts.query, safe=_QUERY_SAFE),
            urllib.parse.quote(parts.fragment, safe=_QUERY_SAFE),
        )
    )


# How much of a body one bounded read call asks for. Only reads under a
# caller's ``limit`` chunk; an unlimited read stays one ``read()``.
_READ_CHUNK = 64 * 1024


class _BodyStream(Protocol):
    """What the bounded read needs of a response: ``read(n)``, short reads ok."""

    def read(self, n: int, /) -> bytes: ...


def _read_limited(stream: _BodyStream, limit: int) -> bytes:
    """Read ``stream`` to at most ``limit + 1`` bytes, in chunks, and stop.

    The single extra byte is what keeps the caller's ceiling test honest:
    ``len(body) > limit`` still fires for an over-ceiling body, while the
    bytes past it are never requested — a 2GB enclosure behind a lying or
    absent Content-Length costs ``limit + 1`` bytes of memory, not 2GB.
    """
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining > 0:
        chunk = stream.read(min(_READ_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _urllib_fetch(url: str, *, user_agent: str, method: str, limit: int | None) -> HttpResponse:
    """Fetch ``url`` over urllib, identifying as ``user_agent``.

    Args:
        url: An absolute http(s) URL.
        user_agent: The User-Agent the request carries.
        method: ``GET`` or ``HEAD``.
        limit: The caller's body ceiling, when it has one — the body is
            read to at most ``limit + 1`` bytes (:func:`_read_limited`).

    Returns:
        The response — 4xx/5xx included, never raised.

    Raises:
        ValueError: ``url`` is not http(s), or its host is not encodable
            for DNS (:func:`_ascii_url`).
        OSError: Connection-level failure (DNS, refused, reset, timeout, a
            truncated body); ``urllib.error.URLError`` is an ``OSError``
            subclass and ``http.client``'s family is normalized into one.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"transport fetches http(s) URLs only, got {url!r}")
    request = urllib.request.Request(  # noqa: S310 — scheme checked above
        _ascii_url(url), headers={"User-Agent": user_agent}, method=method
    )
    with normalize_httplib_errors():
        try:
            with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:  # noqa: S310
                if method == "HEAD":
                    body = b""
                elif limit is None:
                    body = response.read()
                else:
                    body = _read_limited(response, limit)
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
            # ``HTTPException`` is named because it is not an ``OSError`` —
            # an ``IncompleteRead`` here would otherwise leave through
            # ``normalize_httplib_errors`` as a connection failure, turning
            # a 404's `dead` into `blocked`.
            with contextlib.suppress(OSError, ValueError, http.client.HTTPException):
                body = e.read() if limit is None else _read_limited(e, limit)
            return HttpResponse(
                status=e.code,
                content_type=_media_type(e.headers.get("Content-Type") if e.headers else None),
                body=body,
                content_length=_content_length(
                    e.headers.get("Content-Length") if e.headers else None
                ),
            )


def urllib_transport(url: str, *, method: str = "GET", limit: int | None = None) -> HttpResponse:
    """Fetch ``url`` under the browser UA; the contract is :func:`_urllib_fetch`'s."""
    return _urllib_fetch(url, user_agent=BROWSER_UA, method=method, limit=limit)


def bot_transport(url: str, *, method: str = "GET", limit: int | None = None) -> HttpResponse:
    """Fetch ``url`` under :data:`BOT_UA`; the contract is :func:`_urllib_fetch`'s."""
    return _urllib_fetch(url, user_agent=BOT_UA, method=method, limit=limit)
