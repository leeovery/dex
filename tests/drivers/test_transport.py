"""Tests for the HTTP seam: what it returns, and what shape it raises.

The truncated-body cases run against a real localhost socket — the failure
lives inside ``http.client``'s read path and no in-process double reproduces
it faithfully. The non-ASCII cases use one for the same reason: what a URL
becomes on the wire is only visible from the other end of the socket.
"""

import contextlib
import http.client
import socket
import threading
from collections.abc import Iterator

import pytest

from dex_engine.capabilities import Capabilities
from dex_engine.capabilities.transcribe.whisper_api import WhisperApi, urllib_multipart_post
from dex_engine.drivers.file import FileDriver
from dex_engine.drivers.transport import (
    _REQUEST_LINE_FORBIDDEN,
    _ascii_url,
    normalize_httplib_errors,
    urllib_transport,
)
from dex_engine.pipeline.classify import ProviderInputError, classify_connection, classify_http
from dex_engine.pipeline.types import Kind, Status
from tests.drivers.conftest import make_unit, truncating_server


@contextlib.contextmanager
def recording_server() -> Iterator[tuple[str, list[str]]]:
    """Serve 200 OK, recording each request line. Yields the base URL and the log."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    host, port = listener.getsockname()
    seen: list[str] = []

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
                seen.append(data.split(b"\r\n")[0].decode("ascii", "replace"))
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
        assert result.status is Status.BLOCKED
        assert "truncated response body" in (result.reason or "")


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
        assert seen == [f"GET {wire} HTTP/1.1"]

    def test_a_space_in_the_path_reaches_the_wire_encoded(self):
        # http.client rejects [\x00-\x20\x7f] in the request line, not
        # merely non-ASCII: an unencoded space in an href — everyday in a
        # corpus — raised InvalidURL and parked the item blocked, spending
        # five attempts and five wayback lookups on a condition no retry
        # can change. The URL was fetchable all along.
        with recording_server() as (base, seen):
            response = urllib_transport(base + "/reports/annual report.pdf")
        assert response.status == 200
        assert seen == ["GET /reports/annual%20report.pdf HTTP/1.1"]

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
