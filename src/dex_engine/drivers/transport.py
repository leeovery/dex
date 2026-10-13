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

Every call is bounded as a whole, not only per socket operation: a server
dripping a byte inside each socket timeout held a run on one open socket
for most of an hour, and a name lookup has no timeout at all. Everything
up to the response headers runs under one wall clock (:func:`_open`), and
the body is read in whatever pieces arrive, failing once it slows below a
floor or outlasts a wall clock of its own (:func:`_read_body`). Each raises
:class:`~dex_engine.pipeline.classify.StalledTransferError`, a
``TimeoutError``, so it classifies like every other connection failure.

The browser UA is deliberate: the motivating incident was Cloudflare
challenging trafilatura's own fetch client; urllib with a browser UA avoids
the block outright more often than not.
"""

import collections
import contextlib
import http.client
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from dex_engine.pipeline.classify import StalledTransferError

__all__ = [
    "BODY_DEADLINE",
    "BOT_UA",
    "BROWSER_UA",
    "DEFAULT_TIMEOUT",
    "HEADERS_DEADLINE",
    "STALL_MIN_BYTES",
    "STALL_WINDOW",
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

# Per socket operation: a peer silent this long fails the call.
DEFAULT_TIMEOUT = 30.0
# Up to the response headers, redirects included: DNS and the proxy lookup
# have no timeout of their own, and a drip never trips DEFAULT_TIMEOUT.
HEADERS_DEADLINE = 90.0
# The stall floor, about 270 B/s: a 200MB enclosure at 100KB/s clears it
# hundreds of times over, and the byte-per-30s drip that held a run never can.
STALL_WINDOW = 60.0
STALL_MIN_BYTES = 16 * 1024
# The whole body, however it trickles in: a 300MB enclosure at 200KB/s fits,
# and a scheduled run always finishes.
BODY_DEADLINE = 30 * 60


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
    url: str | None = None  # where the body came from, redirects followed, when known

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


# The most of a body one read call asks for.
_READ_CHUNK = 64 * 1024

# Named, so a stack dump or a test can tell the worker from any other thread.
_OPENER_THREAD = "dex-transport-open"


class _Opening:
    """One ``urlopen`` handed from its worker thread to the caller, or abandoned.

    The worker can finish in the instant the caller gives up, so the handoff
    is decided under a lock: exactly one side owns what the call returned,
    and the worker closes a response the caller will never read.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outcome: http.client.HTTPResponse | Exception | None = None
        self._abandoned = False

    def run(self, request: urllib.request.Request) -> None:
        """Open ``request`` — the worker's whole job — and hand over what came of it.

        An exception is handed over inside its own ``except`` clause, which
        unbinds it on the way out: its traceback holds this frame, so a name
        still bound here would hold the exception in a cycle.
        """
        try:
            response = urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT)  # noqa: S310 — the caller checked the scheme
        except Exception as e:  # noqa: BLE001 — not handled here: carried over and raised in the caller's thread
            self._hand_over(e)
        else:
            self._hand_over(response)

    def _hand_over(self, outcome: http.client.HTTPResponse | Exception) -> None:
        with self._lock:
            if not self._abandoned:
                self._outcome = outcome
                return
        if isinstance(outcome, http.client.HTTPResponse | urllib.error.HTTPError):
            outcome.close()

    def collect(self) -> http.client.HTTPResponse:
        """The response; the worker's exception, raised; or the call abandoned.

        Nothing here keeps what it hands over: a raised exception's traceback
        holds this frame and the opening, so either holding the exception
        would keep it — and the connection behind an ``HTTPError`` — alive
        in a cycle until the collector came round.

        Raises:
            StalledTransferError: The worker has not finished — it is
                abandoned, and closes whatever it opens from here on.
        """
        with self._lock:
            outcome, self._outcome = self._outcome, None
            self._abandoned = outcome is None
        if outcome is None:
            raise StalledTransferError(f"no response within {HEADERS_DEADLINE:g}s")
        if isinstance(outcome, Exception):
            try:
                raise outcome
            finally:
                del outcome
        return outcome


def _open(request: urllib.request.Request) -> http.client.HTTPResponse:
    """``urlopen`` ``request`` within :data:`HEADERS_DEADLINE`: the response, headers read.

    The call runs in a daemon worker, because nothing can interrupt what it
    blocks in — ``getaddrinfo``, the macOS proxy lookup, a header dripping
    in a byte per socket timeout. On expiry the worker is abandoned to end
    whenever its call does, never holding up the run or the process's exit.

    Raises:
        StalledTransferError: No response within the deadline.
        Exception: Whatever ``urlopen`` raised, as it raised it — ``HTTPError``
            and the rest of urllib's ``OSError`` family, ``http.client``'s
            protocol failures, a ``ValueError`` for a URL it refuses.
    """
    opening = _Opening()
    worker = threading.Thread(target=opening.run, args=(request,), name=_OPENER_THREAD, daemon=True)
    worker.start()
    worker.join(HEADERS_DEADLINE)
    return opening.collect()


class _Arrivals:
    """A body's arrivals, held to the stall floor and the body deadline."""

    def __init__(self, started: float) -> None:
        self._started = started
        self._recent: collections.deque[tuple[float, int]] = collections.deque()
        self._recent_bytes = 0

    def add(self, size: int, now: float) -> None:
        """Count ``size`` bytes arrived at ``now``.

        Raises:
            StalledTransferError: The body has been read for a whole window
                and fewer than :data:`STALL_MIN_BYTES` arrived in the last
                one, or it has been read for :data:`BODY_DEADLINE`.
        """
        self._recent.append((now, size))
        self._recent_bytes += size
        while self._recent[0][0] < now - STALL_WINDOW:
            self._recent_bytes -= self._recent.popleft()[1]
        reading = now - self._started
        if reading >= STALL_WINDOW and self._recent_bytes < STALL_MIN_BYTES:
            raise StalledTransferError(
                f"transfer stalled: body slower than {STALL_MIN_BYTES} bytes per {STALL_WINDOW:g}s"
            )
        if reading >= BODY_DEADLINE:
            raise StalledTransferError(
                f"transfer stalled: body unfinished after {BODY_DEADLINE:g}s"
            )


def _read_body(
    stream: http.client.HTTPResponse | urllib.error.HTTPError, limit: int | None
) -> bytes:
    """Read ``stream`` whole, or under a ``limit`` to at most ``limit + 1`` bytes.

    ``stream`` is a response, or the error urllib raised in its place, which
    reads through to the same ``http.client`` response.

    ``read1`` answers with whatever has arrived, so the loop regains control
    at least once per socket timeout and the body's bounds
    (:class:`_Arrivals`) are checked as it comes in. ``read(n)`` sits inside
    the buffered reader until all ``n`` bytes are there, and a body dripping
    in a byte at a time never handed control back at all.

    The single extra byte is what keeps the caller's ceiling test honest:
    ``len(body) > limit`` still fires for an over-ceiling body, while the
    bytes past it are never requested — a 2GB enclosure behind a lying or
    absent Content-Length costs ``limit + 1`` bytes of memory, not 2GB.

    Raises:
        StalledTransferError: The body stalled, or outlasted its deadline.
        http.client.IncompleteRead: The body ended short of its declared
            length — which ``read1`` itself reports as a quiet end of body.
    """
    remaining = sys.maxsize if limit is None else limit + 1
    arrivals = _Arrivals(time.monotonic())
    chunks: list[bytes] = []
    while remaining > 0:
        chunk = stream.read1(min(_READ_CHUNK, remaining))
        if not chunk:
            if stream.length:
                raise http.client.IncompleteRead(b"".join(chunks), stream.length)
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        arrivals.add(len(chunk), time.monotonic())
    return b"".join(chunks)


def _urllib_fetch(url: str, *, user_agent: str, method: str, limit: int | None) -> HttpResponse:
    """Fetch ``url`` over urllib, identifying as ``user_agent``.

    Args:
        url: An absolute http(s) URL.
        user_agent: The User-Agent the request carries.
        method: ``GET`` or ``HEAD``.
        limit: The caller's body ceiling, when it has one — the body is
            read to at most ``limit + 1`` bytes (:func:`_read_body`).

    Returns:
        The response — 4xx/5xx included, never raised.

    Raises:
        ValueError: ``url`` is not http(s), or its host is not encodable
            for DNS (:func:`_ascii_url`).
        OSError: Connection-level failure (DNS, refused, reset, timeout, a
            stalled transfer, a truncated body); ``urllib.error.URLError`` is
            an ``OSError`` subclass and ``http.client``'s family is
            normalized into one.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"transport fetches http(s) URLs only, got {url!r}")
    request = urllib.request.Request(  # noqa: S310 — scheme checked above
        _ascii_url(url), headers={"User-Agent": user_agent}, method=method
    )
    with normalize_httplib_errors():
        try:
            with _open(request) as response:
                body = b"" if method == "HEAD" else _read_body(response, limit)
                return HttpResponse(
                    status=response.status,
                    content_type=_media_type(response.headers.get("Content-Type")),
                    body=body,
                    content_length=_content_length(response.headers.get("Content-Length")),
                    url=response.url,
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
                body = _read_body(e, limit)
            e.close()
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
