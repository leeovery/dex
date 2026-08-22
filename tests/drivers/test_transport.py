"""Tests for the HTTP seam: what it returns, and what shape it raises.

The truncated-body cases run against a real localhost socket — the failure
lives inside ``http.client``'s read path and no in-process double reproduces
it faithfully.
"""

import http.client

import pytest

from dex_engine.capabilities import Capabilities
from dex_engine.capabilities.transcribe.whisper_api import WhisperApi, urllib_multipart_post
from dex_engine.drivers.file import FileDriver
from dex_engine.drivers.transport import normalize_httplib_errors, urllib_transport
from dex_engine.pipeline.classify import ProviderInputError, classify_connection, classify_http
from dex_engine.pipeline.types import Kind, Status
from tests.drivers.conftest import make_unit, truncating_server


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
