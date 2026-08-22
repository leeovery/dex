"""Tests for the HTTP seam: what it returns, and what shape it raises.

The truncated-body cases run against a real localhost socket — the failure
lives inside ``http.client``'s read path and no in-process double reproduces
it faithfully.
"""

import http.client

import pytest

from dex_engine.capabilities import Capabilities
from dex_engine.drivers.file import FileDriver
from dex_engine.drivers.transport import normalize_httplib_errors, urllib_transport
from dex_engine.pipeline.classify import classify_connection
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

    def test_an_oserror_passes_through_untouched(self):
        # RemoteDisconnected already IS a ConnectionResetError; the guard
        # must not re-wrap what classify_connection already reads.
        original = ConnectionResetError("reset by peer")
        with pytest.raises(ConnectionResetError) as caught, normalize_httplib_errors():
            raise original
        assert caught.value is original


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
