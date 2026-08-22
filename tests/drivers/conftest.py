"""Shared driver-test plumbing: fixture loading, fake transports, work units."""

import contextlib
import json
import socket
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from dex_engine.drivers.transport import HttpResponse
from dex_engine.pipeline.types import Format, Kind, Result, WorkUnit
from dex_engine.pipeline.urls import work_hash

FIXTURES = Path(__file__).parent.parent / "fixtures"


def fixture_text(driver: str, name: str) -> str:
    return (FIXTURES / driver / name).read_text(encoding="utf-8")


def make_unit(  # noqa: PLR0913 — a test-data builder mirrors the WorkUnit fields
    url: str,
    kind: Kind,
    *,
    item: str = "2026-08-19-example-55ad7b",
    depth: int = 0,
    parent: str | None = None,
    fmt: Format | None = None,
) -> WorkUnit:
    return WorkUnit(
        hash=work_hash(url), url=url, kind=kind, format=fmt, item=item, depth=depth, parent=parent
    )


def reason_of(result: Result) -> str:
    assert result.reason is not None
    return result.reason


def body_of(result: Result) -> str:
    assert result.body is not None
    return result.body


def html_response(html: str, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, content_type="text/html", body=html.encode("utf-8"))


def json_response(payload: object, *, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status, content_type="application/json", body=json.dumps(payload).encode("utf-8")
    )


class FakeTransport:
    """URL -> canned response (or exception to raise). Unknown URLs are loud."""

    def __init__(self, responses: Mapping[str, HttpResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, *, method: str = "GET") -> HttpResponse:
        self.calls.append((method, url))
        if url not in self.responses:
            if method == "HEAD":
                # Detection's sniff HEADs unknown catch-all URLs; an
                # unregistered one is simply inconclusive, not a test bug.
                return HttpResponse(status=200, content_type="text/html", body=b"")
            raise AssertionError(f"unexpected fetch of {url!r} — calls so far: {self.calls}")
        outcome = self.responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def fake_transport():
    return FakeTransport


@contextlib.contextmanager
def truncating_server(
    *, body: bytes = b"ID3\x04\x00\x00\x00partial audio", declared: int = 5_000_000
) -> Iterator[str]:
    """Serve one 200 whose Content-Length far exceeds the bytes sent, then hang up.

    A real socket, deliberately: the truncated-body failure lives inside
    ``http.client``'s read path, and only a genuine short read raises the
    ``IncompleteRead`` the transport has to normalize. Yields the URL.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    host, port = listener.getsockname()

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return  # the listener closed: the context manager is done
            with conn, contextlib.suppress(OSError):
                conn.recv(65536)  # drain the request so the close sends FIN, not RST
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\n"
                    b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (declared, body)
                )
                conn.shutdown(socket.SHUT_WR)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}/ep42.mp3"
    finally:
        listener.close()
        thread.join(timeout=5)
