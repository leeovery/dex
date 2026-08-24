"""Shared driver-test plumbing: fixture loading, fake transports, work units."""

import base64
import contextlib
import dataclasses
import json
import re
import socket
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from dex_engine.drivers.gh import GhResult
from dex_engine.drivers.transport import HttpResponse
from dex_engine.pipeline.types import (
    Content,
    Format,
    Kind,
    Missing,
    NeedsCapability,
    Refused,
    Result,
    Unusable,
    WorkUnit,
)
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


def content_of(outcome: object) -> Content:
    """The outcome as ``Content`` — the fetch found something, or the test fails."""
    assert isinstance(outcome, Content), f"expected Content, got {outcome!r}"
    return outcome


def needs_of(outcome: object) -> NeedsCapability:
    """The outcome as ``NeedsCapability`` — a capability park, or the test fails."""
    assert isinstance(outcome, NeedsCapability), f"expected NeedsCapability, got {outcome!r}"
    return outcome


def evidence_of(outcome: object) -> str:
    """The stated evidence of a failure outcome, or the test fails."""
    assert isinstance(outcome, Missing | Refused | Unusable), (
        f"expected a failure outcome, got {outcome!r}"
    )
    return outcome.evidence


def body_of(outcome: object) -> str:
    assert isinstance(outcome, Result | Content | NeedsCapability), (
        f"expected an outcome carrying a body, got {outcome!r}"
    )
    assert outcome.body is not None
    return outcome.body


def html_response(html: str, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, content_type="text/html", body=html.encode("utf-8"))


def json_response(payload: object, *, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status, content_type="application/json", body=json.dumps(payload).encode("utf-8")
    )


class FakeTransport:
    """URL -> canned response (or exception to raise). Unknown URLs are loud.

    A caller's ``limit`` is honored the way the real transport honors it —
    the body truncated one byte past the ceiling — and recorded per call in
    ``limits`` so a test can pin which fetches carried one.
    """

    def __init__(self, responses: Mapping[str, HttpResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []
        self.limits: list[int | None] = []

    def __call__(self, url: str, *, method: str = "GET", limit: int | None = None) -> HttpResponse:
        self.calls.append((method, url))
        self.limits.append(limit)
        if url not in self.responses:
            if method == "HEAD":
                # Detection's sniff HEADs unknown catch-all URLs; an
                # unregistered one is simply inconclusive, not a test bug.
                return HttpResponse(status=200, content_type="text/html", body=b"")
            raise AssertionError(f"unexpected fetch of {url!r} — calls so far: {self.calls}")
        outcome = self.responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        if limit is not None and len(outcome.body) > limit:
            return dataclasses.replace(outcome, body=outcome.body[: limit + 1])
        return outcome


@pytest.fixture
def fake_transport():
    return FakeTransport


class FakeGh:
    """args tuple -> GhResult; unexpected invocations are loud.

    Shared because two drivers read the same gh seam: the github driver and
    the file driver both fetch blob bytes through it.
    """

    def __init__(self, responses: Mapping[tuple[str, ...], GhResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> GhResult:
        key = tuple(args)
        self.calls.append(key)
        if key not in self.responses:
            raise AssertionError(f"unexpected gh invocation {key!r}")
        return self.responses[key]


def gh_ok(stdout: str) -> GhResult:
    return GhResult(returncode=0, stdout=stdout, stderr="")


def gh_fail(stderr: str) -> GhResult:
    return GhResult(returncode=1, stdout="", stderr=stderr)


def gh_contents(data: bytes) -> GhResult:
    """The contents-API payload shape: base64 `content` plus its encoding."""
    return gh_ok(
        json.dumps({"encoding": "base64", "content": base64.b64encode(data).decode("ascii")})
    )


def gh_matching_refs(*names: str) -> GhResult:
    """A ``git/matching-refs`` page, in the API's own shape."""
    return gh_ok(json.dumps([{"ref": name, "object": {"sha": "0" * 40}} for name in names]))


def _drain_request(conn: socket.socket) -> None:
    """Read the whole request — headers AND body — before answering.

    Answering a POST while the client is still uploading closes the socket
    on unread bytes, and the client sees a reset instead of the truncated
    read the test is about.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    declared = re.search(rb"(?i)content-length:\s*(\d+)", head)
    outstanding = int(declared.group(1)) - len(body) if declared else 0
    while outstanding > 0:
        chunk = conn.recv(min(outstanding, 65536))
        if not chunk:
            return
        outstanding -= len(chunk)


@contextlib.contextmanager
def truncating_server(
    *,
    body: bytes = b"ID3\x04\x00\x00\x00partial audio",
    declared: int = 5_000_000,
    status: bytes = b"200 OK",
    redirect_first: bool = False,
) -> Iterator[str]:
    """Serve one response whose Content-Length far exceeds the bytes sent, then hang up.

    A real socket, deliberately: the truncated-body failure lives inside
    ``http.client``'s read path, and only a genuine short read raises the
    ``IncompleteRead`` the transport has to normalize. Yields the URL.

    ``status`` truncates an ERROR body instead — the case where the read
    failure must not cost the status code. ``redirect_first`` answers the
    first connection with a 302 to the same server, for the download paths
    that follow one by hand.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    host, port = listener.getsockname()
    served = 0

    def serve() -> None:
        nonlocal served
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return  # the listener closed: the context manager is done
            served += 1
            with conn, contextlib.suppress(OSError):
                _drain_request(conn)
                if redirect_first and served == 1:
                    conn.sendall(
                        b"HTTP/1.1 302 Found\r\nLocation: http://%s:%d/signed\r\n"
                        b"Content-Length: 0\r\nConnection: close\r\n\r\n" % (host.encode(), port)
                    )
                else:
                    conn.sendall(
                        b"HTTP/1.1 %s\r\nContent-Type: audio/mpeg\r\n"
                        b"Content-Length: %d\r\nConnection: close\r\n\r\n%s"
                        % (status, declared, body)
                    )
                conn.shutdown(socket.SHUT_WR)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}/ep42.mp3"
    finally:
        listener.close()
        thread.join(timeout=5)
