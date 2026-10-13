"""Tests for the HTTP seam: what it returns, and what shape it raises.

The truncated-body cases run against a real localhost socket — the failure
lives inside ``http.client``'s read path and no in-process double reproduces
it faithfully. The non-ASCII cases use one for the same reason: what a URL
becomes on the wire is only visible from the other end of the socket, and
so do the stall cases, whose drips only ``http.client``'s buffered reads
can be caught in.
"""

import contextlib
import gc
import http.client
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Self

import pytest

from dex_engine.capabilities import Capabilities
from dex_engine.capabilities.transcribe.whisper_api import WhisperApi, urllib_multipart_post
from dex_engine.drivers import transport
from dex_engine.drivers.fetch import FetchFailure, fetch_classified
from dex_engine.drivers.file import FileDriver
from dex_engine.drivers.transport import (
    _OPENER_THREAD,
    _REQUEST_LINE_FORBIDDEN,
    BODY_DEADLINE,
    BOT_UA,
    BROWSER_UA,
    STALL_MIN_BYTES,
    STALL_WINDOW,
    _Arrivals,
    _ascii_url,
    bot_transport,
    normalize_httplib_errors,
    urllib_transport,
)
from dex_engine.pipeline.classify import (
    ProviderInputError,
    StalledTransferError,
    classify_connection,
    classify_http,
)
from dex_engine.pipeline.types import Kind, Refused, Status
from tests.drivers.conftest import (
    chunked,
    drip,
    make_unit,
    paced_server,
    response_head,
    truncating_server,
)


@dataclass(frozen=True, slots=True)
class _RecordedRequest:
    """One request as the server saw it."""

    line: str
    headers: dict[str, str]  # field names lowercased, values stripped


def _record(head: bytes) -> _RecordedRequest:
    line, *fields = head.decode("ascii", "replace").split("\r\n")
    split = (field.partition(":") for field in fields)
    return _RecordedRequest(line=line, headers={name.lower(): v.strip() for name, _, v in split})


@contextlib.contextmanager
def recording_server(
    *, redirect: str | None = None
) -> Iterator[tuple[str, list[_RecordedRequest]]]:
    """Serve 200 OK, recording each request. Yields the base URL and the log.

    ``redirect`` answers the first request with a 302 to that path instead.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    host, port = listener.getsockname()
    seen: list[_RecordedRequest] = []

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return  # the listener closed: the context manager is done
            with conn, contextlib.suppress(OSError):
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                seen.append(_record(data.split(b"\r\n\r\n")[0]))
                if redirect is not None and len(seen) == 1:
                    conn.sendall(
                        b"HTTP/1.1 302 Found\r\nLocation: %s\r\n"
                        b"Content-Length: 0\r\nConnection: close\r\n\r\n" % redirect.encode()
                    )
                else:
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                        b"Content-Length: 2\r\nConnection: close\r\n\r\nhi"
                    )
                conn.shutdown(socket.SHUT_WR)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}", seen
    finally:
        listener.close()
        thread.join(timeout=5)


class TestUserAgent:
    """Which UA reaches the wire — the only difference between the transports.

    The bot UA is load-bearing, not cosmetic: the Instagram embed proxies
    serve their OpenGraph metadata to link-preview bots and redirect a
    browser UA to the JS app shell, which carries no metadata at all.
    """

    @pytest.mark.parametrize(
        ("transport", "expected"),
        [(urllib_transport, BROWSER_UA), (bot_transport, BOT_UA)],
    )
    def test_each_transport_identifies_as_its_own_agent(self, transport, expected):
        with recording_server() as (base, seen):
            response = transport(base + "/p/abc123/")
        assert response.status == 200
        assert [request.headers["user-agent"] for request in seen] == [expected]


class TestServedUrl:
    """Where a body came from: what every relative URL inside it resolves against."""

    def test_a_redirected_fetch_reports_the_url_it_landed_on(self):
        with recording_server(redirect="/docs/create/code") as (base, seen):
            response = urllib_transport(base + "/docs/code")
        assert response.url == base + "/docs/create/code"
        assert [request.line for request in seen] == [
            "GET /docs/code HTTP/1.1",
            "GET /docs/create/code HTTP/1.1",
        ]

    def test_a_direct_fetch_reports_its_own_url(self):
        with recording_server() as (base, _):
            response = urllib_transport(base + "/p/abc123/")
        assert response.url == base + "/p/abc123/"


class TestHttplibNormalization:
    def test_incomplete_read_becomes_a_classified_connection_failure(self):
        # A server that closes mid-body — the routine failure mode for a
        # 100MB enclosure — must not escape as an engine bug: IncompleteRead
        # is an HTTPException, which no caller's `except OSError` catches.
        with truncating_server() as url, pytest.raises(OSError) as caught:  # noqa: PT011 — the OSError shape IS the assertion
            urllib_transport(url)
        assert "truncated response body" in str(caught.value)
        assert classify_connection(caught.value).status is Status.BLOCKED

    def test_the_family_normalizes_not_just_incomplete_read(self):
        with pytest.raises(OSError, match="BadStatusLine"), normalize_httplib_errors():
            raise http.client.BadStatusLine("garbage")

    def test_a_truncated_error_body_still_classifies_by_its_status(self):
        # The other half: the body of a 4xx is only detail, and losing the
        # read must not lose the STATUS. Without the guard the truncated
        # read escapes as a connection failure and a 404's `dead` — the
        # item is gone — arrives as `blocked`, retried forever.
        with truncating_server(status=b"404 Not Found") as url:
            response = urllib_transport(url)
        assert response.status == 404
        assert classify_http(response.status).status is Status.DEAD

    def test_an_oserror_passes_through_untouched(self):
        # RemoteDisconnected already IS a ConnectionResetError; the guard
        # must not re-wrap what classify_connection already reads.
        original = ConnectionResetError("reset by peer")
        with pytest.raises(ConnectionResetError) as caught, normalize_httplib_errors():
            raise original
        assert caught.value is original


class TestThroughTheWhisperApiPost:
    def test_a_truncated_response_reaches_the_provider_as_an_oserror(self):
        # The second urllib seam: whisper-api's multipart POST guards on
        # `except OSError` too, and maps it to ProviderUnavailableError —
        # the job waits. Only if the httplib failure arrives as an OSError.
        with truncating_server() as url, pytest.raises(OSError) as caught:  # noqa: PT011 — the OSError shape IS the assertion
            urllib_multipart_post(url, api_key=None, fields={}, filename="a.mp3", file_bytes=b"x")
        assert "truncated response body" in str(caught.value)

    def test_a_truncated_error_body_keeps_its_status(self):
        with truncating_server(status=b"400 Bad Request", body=b"unsupported forma") as url:
            status, body = urllib_multipart_post(
                url, api_key=None, fields={}, filename="a.mp3", file_bytes=b"x"
            )
        assert status == 400
        assert body == b""  # the detail goes, as at the transport seam; the status stays

    def test_a_truncated_400_still_parks_the_episode_manual(self, tmp_path):
        # The verdict the status carries: 400 is the AUDIO's fault — manual,
        # under the escalation clock. A read failure escaping instead reads
        # as the endpoint being down: waiting, retried forever, no clock.
        chunk = tmp_path / "chunk-000.mp3"
        chunk.write_bytes(b"audio")
        with truncating_server(status=b"400 Bad Request", body=b"unsupported forma") as url:
            provider = WhisperApi(base_url=url, api_key="k", post=urllib_multipart_post)
            with pytest.raises(ProviderInputError, match="rejected the audio"):
                provider._transcribe_chunk(chunk, "")  # noqa: SLF001 — the seam under test


class TestThroughADriverFetch:
    def test_truncated_download_is_blocked_never_an_engine_error(self):
        driver = FileDriver(
            capabilities=Capabilities(transcribers=(), extractors=()),
            transport=urllib_transport,
        )
        with truncating_server() as url:
            result = driver.fetch(make_unit(url, Kind.FILE))
        assert isinstance(result, Refused)
        assert not result.permanent
        assert "truncated response body" in result.evidence


class TestNonAsciiUrls:
    """A URL with an accent — or a space — in it is a URL to fetch, not an error."""

    @pytest.mark.parametrize(
        ("path", "wire"),
        [
            ("/wiki/Café", "/wiki/Caf%C3%A9"),  # an accented Wikipedia path
            ("/记/分布式", "/%E8%AE%B0/%E5%88%86%E5%B8%83%E5%BC%8F"),  # a CJK slug
            ("/search?q=café", "/search?q=caf%C3%A9"),  # the query too
        ],
    )
    def test_the_url_reaches_the_wire_percent_encoded(self, path, wire):
        # http.client ascii-encodes the request line, so every one of these
        # raised UnicodeEncodeError before a byte left the machine — and a
        # UnicodeEncodeError IS a ValueError, so it walked past the drivers'
        # `except OSError` guards and parked the item manual, unfetched,
        # with a raw codec message as the operator's stated reason.
        with recording_server() as (base, seen):
            response = urllib_transport(base + path)
        assert response.status == 200
        assert [request.line for request in seen] == [f"GET {wire} HTTP/1.1"]

    def test_a_space_in_the_path_reaches_the_wire_encoded(self):
        # http.client rejects [\x00-\x20\x7f] in the request line, not
        # merely non-ASCII: an unencoded space in an href — everyday in a
        # corpus — raised InvalidURL and parked the item blocked, spending
        # every attempt, and a wayback lookup with each, on a condition no
        # retry can change. The URL was fetchable all along.
        with recording_server() as (base, seen):
            response = urllib_transport(base + "/reports/annual report.pdf")
        assert response.status == 200
        assert [request.line for request in seen] == ["GET /reports/annual%20report.pdf HTTP/1.1"]

    @pytest.mark.parametrize("char", ["\x00", " ", "\x1f", "\x7f"])
    def test_no_character_the_request_line_forbids_survives_the_encoder(self, char):
        assert _REQUEST_LINE_FORBIDDEN.search(_ascii_url(f"https://example.com/a{char}b")) is None

    def test_an_idn_host_is_punycoded(self):
        assert _ascii_url("https://münchen.example/rathaus") == (
            "https://xn--mnchen-3ya.example/rathaus"
        )

    def test_an_idn_host_keeps_its_port_and_encodes_its_path(self):
        assert _ascii_url("https://例え.テスト:8443/パス?q=検索") == (
            "https://xn--r8jz45g.xn--zckzah:8443/%E3%83%91%E3%82%B9?q=%E6%A4%9C%E7%B4%A2"
        )

    def test_an_already_encoded_url_passes_through_untouched(self):
        # The encoding is idempotent or it corrupts the canonical URL the
        # ledger keys on: %C3%A9 must never become %25C3%25A9. The pin has
        # to be a URL that REACHES the encoder — one already-encoded escape
        # beside a character that still needs encoding — because a URL the
        # encoder returns at the guard pins nothing about what quote does.
        assert _ascii_url("https://fr.wikipedia.org/wiki/Caf%C3%A9?q=thé#frag") == (
            "https://fr.wikipedia.org/wiki/Caf%C3%A9?q=th%C3%A9#frag"
        )
        url = "https://fr.wikipedia.org/wiki/Caf%C3%A9?a=b&c=d#frag"
        assert _ascii_url(url) == url
        assert _ascii_url(_ascii_url("https://fr.wikipedia.org/wiki/Café")) == (
            "https://fr.wikipedia.org/wiki/Caf%C3%A9"
        )

    def test_an_unencodable_host_fails_with_a_stated_reason(self):
        # The one genuinely unfetchable shape: an IDN label DNS cannot
        # carry. It still fails as a stated ValueError the bad-seed
        # containment can park on, never the codec's own words.
        with pytest.raises(ValueError, match="is not encodable for DNS"):
            urllib_transport("https://" + "ü" * 200 + ".example/")


class _ChunkedResponse:
    """A 200 body served in chunks, counting every byte the caller draws.

    ``read1(n)`` answers short (at most one chunk), the way a socket does,
    so every byte drawn past a ceiling shows in ``served``.
    """

    status = 200
    url = "https://cdn.example.test/served.mp3"

    def __init__(self, total: int, chunk: int = 8192) -> None:
        self.total = total
        self.chunk = chunk
        self.served = 0
        self.headers = {"Content-Type": "audio/mpeg", "Content-Length": str(total)}

    @property
    def length(self) -> int:
        return self.total - self.served

    def read1(self, n: int) -> bytes:
        take = min(n, self.chunk, self.length)
        self.served += take
        return b"m" * take

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class TestBoundedBodyRead:
    """A caller's ceiling bounds what the transport ever draws or holds."""

    LIMIT = 1_000

    def _serve(self, monkeypatch, total: int, chunk: int = 8192) -> _ChunkedResponse:
        response = _ChunkedResponse(total, chunk)
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda request, timeout: response,  # noqa: ARG005 — one canned response
        )
        return response

    def test_an_over_ceiling_body_stops_one_byte_past_the_limit(self, monkeypatch):
        response = self._serve(monkeypatch, total=self.LIMIT * 40)
        fetched = urllib_transport("https://cdn.example.test/huge.mp3", limit=self.LIMIT)
        # One byte past the ceiling: len(body) > limit still fires for the
        # caller's check, and the other 39,000 bytes were never requested —
        # the memory cost of an oversize body is the ceiling, not the body.
        assert len(fetched.body) == self.LIMIT + 1
        assert response.served == self.LIMIT + 1

    def test_the_extra_byte_is_asked_for_on_its_own(self, monkeypatch):
        # Pieces that reach the ceiling exactly leave the one extra byte
        # outstanding: unless the read asks for it alone, an over-ceiling
        # body arrives exactly at the limit and passes the caller's check.
        response = self._serve(monkeypatch, total=self.LIMIT * 40, chunk=self.LIMIT // 2)
        fetched = urllib_transport("https://cdn.example.test/huge.mp3", limit=self.LIMIT)
        assert len(fetched.body) == self.LIMIT + 1
        assert response.served == self.LIMIT + 1

    def test_a_body_at_the_ceiling_arrives_byte_identical(self, monkeypatch):
        self._serve(monkeypatch, total=self.LIMIT)
        fetched = urllib_transport("https://cdn.example.test/ok.mp3", limit=self.LIMIT)
        assert fetched.body == b"m" * self.LIMIT

    def test_no_ceiling_still_reads_the_body_whole(self, monkeypatch):
        self._serve(monkeypatch, total=self.LIMIT * 3)
        fetched = urllib_transport("https://cdn.example.test/page")
        assert fetched.body == b"m" * (self.LIMIT * 3)


# Every stall below ends in a fraction of a second under `tight_bounds`,
# while the server behind it would hold an unbounded call seconds longer.
PROMPTLY = 2.0


@pytest.fixture
def tight_bounds(monkeypatch):
    """The transfer bounds at test scale: a stall shows in a third of a second."""
    monkeypatch.setattr(transport, "HEADERS_DEADLINE", 0.3)
    monkeypatch.setattr(transport, "STALL_WINDOW", 0.3)
    monkeypatch.setattr(transport, "STALL_MIN_BYTES", 1024)


def opener_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == _OPENER_THREAD]


def assert_openers_end() -> None:
    """Every abandoned worker ends once its call does — none outlives its server."""
    for thread in opener_threads():
        thread.join(timeout=5)
    assert opener_threads() == []


class TestHeadersDeadline:
    """Everything before the body under one wall clock, whatever it waits on."""

    @pytest.mark.usefixtures("tight_bounds")
    def test_a_dripping_status_line_fails_at_the_deadline(self):
        # A byte every 50ms never trips the per-operation socket timeout:
        # http.client's readline sits in its buffered reader for as long as
        # the server cares to drip, which here is four seconds.
        with paced_server(drip(response_head(length=2) + b"hi", every=0.05)) as served:
            started = time.monotonic()
            with pytest.raises(StalledTransferError, match=r"^no response within 0\.3s$"):
                urllib_transport(served.base + "/slow-head")
            assert time.monotonic() - started < PROMPTLY
        assert_openers_end()

    @pytest.mark.usefixtures("tight_bounds")
    def test_a_name_lookup_that_never_answers_fails_at_the_deadline(self, monkeypatch):
        # getaddrinfo has no timeout at all, and on macOS it waits on
        # mDNSResponder: no socket of the process's own exists to time out.
        release = threading.Event()

        def unanswered(*_args, **_kwargs):
            release.wait(timeout=10)
            raise socket.gaierror(socket.EAI_AGAIN, "temporary failure in name resolution")

        monkeypatch.setattr(socket, "getaddrinfo", unanswered)
        try:
            started = time.monotonic()
            with pytest.raises(StalledTransferError):
                urllib_transport("http://unanswered.example.test/")
            assert time.monotonic() - started < PROMPTLY
            # Abandoned, never awaited: a daemon cannot hold up the exit.
            stuck = opener_threads()
            assert stuck
            assert all(thread.daemon for thread in stuck)
        finally:
            release.set()
        assert_openers_end()

    @pytest.mark.usefixtures("tight_bounds")
    def test_an_abandoned_worker_hangs_up_on_the_response_it_gets(self):
        # The headers complete after the caller gave up: nobody will read
        # that response, so the worker closes it rather than leave the
        # server holding a connection open for a reader that never comes.
        with paced_server(drip(response_head(length=100), every=0.01)) as served:
            # Kept bound: the call's frames hold its handoff, so a response
            # handed over to it rather than closed would stay open.
            with pytest.raises(StalledTransferError) as abandoned:
                urllib_transport(served.base + "/late-head")
            assert served.hung_up.wait(timeout=5)
        assert abandoned.type is StalledTransferError
        assert_openers_end()

    def test_a_completed_call_leaves_no_worker_behind(self):
        with recording_server() as (base, _):
            assert urllib_transport(base + "/").status == 200
        assert opener_threads() == []


@pytest.mark.usefixtures("tight_bounds")
class TestBodyStall:
    """A body slower than the floor, or past its deadline, fails; one within both never does."""

    @pytest.mark.parametrize("limit", [None, 10_000_000])
    def test_a_dripping_body_stalls(self, limit):
        # Headers at once, then a byte every 10ms — inside every socket
        # timeout, so only the body's own floor ends it, ten seconds early.
        steps = [(0, response_head(length=100_000)), *drip(b"x" * 1000, every=0.01)]
        with paced_server(steps) as served:
            started = time.monotonic()
            with pytest.raises(
                StalledTransferError,
                match=r"^transfer stalled: body slower than 1024 bytes per 0\.3s$",
            ):
                urllib_transport(served.base + "/drip.jpg", limit=limit)
            assert time.monotonic() - started < PROMPTLY

    def test_a_dripping_chunked_body_stalls(self):
        steps = [(0, response_head(length=None) + b"3e8\r\n"), *drip(b"x" * 1000, every=0.01)]
        with paced_server(steps) as served, pytest.raises(StalledTransferError):
            urllib_transport(served.base + "/drip-chunked.jpg")

    def test_a_fast_start_does_not_cover_a_later_drip(self):
        # The floor reads the trailing window, never the running total: the
        # burst leaves the window and the drip behind it stalls on its own.
        steps = [
            (0, response_head(length=100_000) + b"x" * 50_000),
            *drip(b"x" * 1000, every=0.01),
        ]
        with paced_server(steps) as served:
            started = time.monotonic()
            with pytest.raises(StalledTransferError):
                urllib_transport(served.base + "/burst-then-drip.mp3")
            assert time.monotonic() - started < PROMPTLY

    def test_a_slow_body_above_the_floor_arrives_whole(self):
        # A podcast over a poor link must never trip the floor: 1KB every
        # 30ms is ten times the floor, sustained across four windows.
        pieces = [bytes([n]) * 1024 for n in range(40)]
        steps = [(0, response_head(length=len(pieces) * 1024)), *((0.03, p) for p in pieces)]
        with paced_server(steps) as served:
            fetched = urllib_transport(served.base + "/slow.mp3")
        assert fetched.body == b"".join(pieces)

    def test_a_steady_body_past_its_deadline_fails(self, monkeypatch):
        # Ten times the floor in every window, but the whole body is held
        # to a wall clock as well: a server clearing the floor by a hair
        # would hold a 10MB read for nine hours, and a scheduled run must
        # finish.
        monkeypatch.setattr(transport, "BODY_DEADLINE", 0.5)
        pieces = [bytes([n]) * 1024 for n in range(40)]
        steps = [(0, response_head(length=len(pieces) * 1024)), *((0.03, p) for p in pieces)]
        with paced_server(steps) as served:
            started = time.monotonic()
            with pytest.raises(
                StalledTransferError, match=r"^transfer stalled: body unfinished after 0\.5s$"
            ):
                urllib_transport(served.base + "/endless.mp3")
            assert 0.5 <= time.monotonic() - started < PROMPTLY

    def test_a_body_gone_silent_fails_at_the_socket_timeout(self, monkeypatch):
        # Silence is the socket timeout's to catch, as it always was, and
        # it reads as a timeout — never as a drip the server kept up.
        monkeypatch.setattr(transport, "DEFAULT_TIMEOUT", 0.3)
        steps = [(0, response_head(length=100_000) + b"x" * 50_000)]
        with paced_server(steps) as served, pytest.raises(TimeoutError) as caught:
            urllib_transport(served.base + "/silent.mp3")
        assert type(caught.value) is TimeoutError
        assert classify_connection(caught.value).reason == "connection timed out"

    def test_a_dripping_error_body_keeps_its_status(self):
        # The error page is only detail: its stall is dropped like any other
        # failure reading it, and the status still classifies the fetch.
        steps = [
            (0, response_head(b"404 Not Found", length=100_000)),
            *drip(b"x" * 1000, every=0.01),
        ]
        with paced_server(steps) as served:
            started = time.monotonic()
            response = urllib_transport(served.base + "/gone")
            assert time.monotonic() - started < PROMPTLY
        assert response.status == 404
        assert response.body == b""


class TestResponseShape:
    """What a response carries besides its body, whatever its status."""

    def test_a_response_states_its_media_type_and_declared_length(self):
        body = b"\x89PNG"
        with paced_server([(0, response_head(length=len(body)) + body)]) as served:
            response = urllib_transport(served.base + "/a.png")
        assert (response.status, response.content_type, response.content_length) == (
            200,
            "image/png",
            len(body),
        )

    def test_an_error_response_states_them_too_under_the_same_ceiling(self):
        body = b"e" * 200_000
        with paced_server(
            [(0, response_head(b"404 Not Found", length=len(body)) + body)]
        ) as served:
            response = urllib_transport(served.base + "/gone.png", limit=1000)
        assert (response.status, response.content_type, response.content_length) == (
            404,
            "image/png",
            len(body),
        )
        assert response.body == b"e" * 1001

    def test_a_head_goes_out_as_head_and_reads_no_body(self):
        with recording_server() as (base, seen):
            response = urllib_transport(base + "/probe.png", method="HEAD")
        assert [request.line for request in seen] == ["HEAD /probe.png HTTP/1.1"]
        assert response.body == b""


# Longer than the ceiling the release tests read it under: http.client
# closes a connection itself once a body is read to its declared end, so
# only a read stopped short leaves the socket for the transport to close.
NOT_FOUND = response_head(b"404 Not Found", length=200_000) + b"e" * 200_000


@contextlib.contextmanager
def collector_off() -> Iterator[None]:
    """No cyclic collection: what is still held after a call stays held."""
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


class TestErrorResponseRelease:
    """An error response is let go when the call returns, never left to the collector."""

    def test_its_connection_is_closed_when_the_call_returns(self):
        with collector_off(), paced_server([(0, NOT_FOUND)]) as served:
            response = urllib_transport(served.base + "/gone.png", limit=1000)
            assert served.hung_up.wait(timeout=2)
        assert response.status == 404

    def test_the_error_is_held_in_no_cycle(self):
        # Raised through the worker's handoff, the error's traceback holds
        # the frames that handed it over: if any of them still held the
        # error, the two would keep each other — and its socket — alive.
        with collector_off():
            with paced_server([(0, NOT_FOUND)]) as served:
                urllib_transport(served.base + "/gone.png", limit=1000)
            held = [obj for obj in gc.get_objects() if isinstance(obj, urllib.error.HTTPError)]
        assert held == []


class TestBodyOverTheWire:
    """The pieces ``read1`` hands back add up to the body ``read`` returned."""

    def test_a_body_arrives_whole(self):
        body = bytes(range(256)) * 1024
        with paced_server([(0, response_head(length=len(body)) + body)]) as served:
            assert urllib_transport(served.base + "/whole.png").body == body

    def test_a_chunked_body_arrives_whole(self):
        pieces = [b"a" * 70_000, b"b" * 3, b"c" * 100_000]
        with paced_server([(0, response_head(length=None) + chunked(*pieces))]) as served:
            assert urllib_transport(served.base + "/chunked.png").body == b"".join(pieces)

    def test_an_over_ceiling_body_stops_one_byte_past_the_limit(self):
        body = b"z" * 200_000
        with paced_server([(0, response_head(length=len(body)) + body)]) as served:
            assert urllib_transport(served.base + "/big.png", limit=1000).body == b"z" * 1001

    def test_a_body_at_the_ceiling_arrives_whole(self):
        body = b"q" * 1000
        with paced_server([(0, response_head(length=len(body)) + body)]) as served:
            assert urllib_transport(served.base + "/exact.png", limit=1000).body == body

    def test_a_truncated_body_under_a_ceiling_fails_as_truncated(self):
        # read1 — and read(n) before it — reports a server hanging up short
        # of its Content-Length as a quiet end of body, so a media download
        # cut off mid-way landed as a broken image under a done line.
        with truncating_server() as url, pytest.raises(OSError) as caught:  # noqa: PT011 — the OSError shape IS the assertion
            urllib_transport(url, limit=10_000_000)
        assert str(caught.value) == "truncated response body (20 bytes read, 4999980 more expected)"


class TestStallFloor:
    """The stall rule on an explicit clock: once a whole window has passed,
    fewer than the floor's bytes in the trailing window is a stall."""

    def test_no_verdict_before_a_whole_window(self):
        _Arrivals(0.0).add(1, STALL_WINDOW - 0.001)

    def test_a_window_short_of_the_floor_has_stalled(self):
        with pytest.raises(StalledTransferError):
            _Arrivals(0.0).add(STALL_MIN_BYTES - 1, STALL_WINDOW)

    def test_the_floor_itself_is_enough(self):
        arrivals = _Arrivals(0.0)
        arrivals.add(STALL_MIN_BYTES - 1, 1.0)
        arrivals.add(1, STALL_WINDOW)

    def test_bytes_leave_the_window_as_it_trails(self):
        arrivals = _Arrivals(0.0)
        arrivals.add(STALL_MIN_BYTES * 100, 1.0)
        arrivals.add(1, STALL_WINDOW + 1.0)  # the burst is exactly a window old: still in it
        with pytest.raises(StalledTransferError):
            arrivals.add(1, STALL_WINDOW + 1.5)


class TestBodyDeadline:
    """The whole body on an explicit clock: however fast it arrives, it ends at the deadline."""

    def test_a_body_well_above_the_floor_reads_up_to_the_deadline(self):
        arrivals = _Arrivals(0.0)
        arrivals.add(STALL_MIN_BYTES, BODY_DEADLINE - 1.0)
        arrivals.add(1, BODY_DEADLINE - 0.001)

    def test_the_deadline_ends_it(self):
        arrivals = _Arrivals(0.0)
        arrivals.add(STALL_MIN_BYTES, BODY_DEADLINE - 1.0)
        with pytest.raises(
            StalledTransferError, match=r"^transfer stalled: body unfinished after 1800s$"
        ):
            arrivals.add(1, BODY_DEADLINE)


@pytest.mark.usefixtures("tight_bounds")
class TestStallClassification:
    def test_a_stalled_fetch_is_blocked_and_names_the_stall(self):
        steps = [(0, response_head(length=100_000)), *drip(b"x" * 1000, every=0.01)]
        with paced_server(steps) as served:
            failure = fetch_classified(urllib_transport, served.base + "/drip.jpg", limit=10_000)
        assert isinstance(failure, FetchFailure)
        assert failure.http_status is None
        assert failure.classification.status is Status.BLOCKED
        assert failure.classification.reason == (
            "transfer stalled: body slower than 1024 bytes per 0.3s"
        )

    def test_a_fetch_with_no_response_by_the_deadline_is_never_called_a_stall(self):
        # A dripping status line and four unreachable addresses tried in
        # turn both run out the same clock: the reason says only that no
        # response came.
        with paced_server(drip(response_head(length=2) + b"hi", every=0.05)) as served:
            failure = fetch_classified(urllib_transport, served.base + "/slow-head")
        assert isinstance(failure, FetchFailure)
        assert failure.classification.status is Status.BLOCKED
        assert failure.classification.reason == "no response within 0.3s"
        assert_openers_end()
