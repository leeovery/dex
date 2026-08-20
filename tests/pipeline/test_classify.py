"""Tests for pipeline/classify.py: the one classification point (§5)."""

import socket
import ssl
import urllib.error

import pytest

from dex_engine.pipeline.classify import (
    PAYWALL_REASON,
    Classification,
    ProviderInputError,
    classify_connection,
    classify_http,
    scrub,
)
from dex_engine.pipeline.types import Status


class TestClassifyHttp:
    def test_403_is_blocked_never_dead(self):
        # THE regression pin (§15): the motivating incident was a 403
        # challenge ledgered `dead` — terminal, never retried.
        classification = classify_http(403)
        assert classification.status is Status.BLOCKED
        assert classification.status is not Status.DEAD
        assert "403" in classification.reason

    @pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
    def test_rate_limits_and_5xx_are_blocked(self, code):
        assert classify_http(code).status is Status.BLOCKED

    @pytest.mark.parametrize("code", [404, 410])
    def test_confirmed_gone_is_dead(self, code):
        classification = classify_http(code)
        assert classification.status is Status.DEAD
        assert str(code) in classification.reason

    @pytest.mark.parametrize("code", [401, 402])
    def test_login_walls_and_paywalls_are_manual(self, code):
        # 402: x.com answers it; retrying never resolves payment-required (§5).
        classification = classify_http(code)
        assert classification.status is Status.MANUAL
        assert PAYWALL_REASON in classification.reason

    @pytest.mark.parametrize("code", [400, 405, 418, 451])
    def test_unlisted_failures_are_blocked_never_silently_terminal(self, code):
        assert classify_http(code).status is Status.BLOCKED

    @pytest.mark.parametrize("code", [200, 301, 399])
    def test_non_failing_codes_are_rejected(self, code):
        with pytest.raises(ValueError, match=">= 400"):
            classify_http(code)

    def test_every_classification_states_a_reason(self):
        for code in (401, 402, 403, 404, 410, 429, 500):
            assert classify_http(code).reason


class TestClassifyConnection:
    def test_nxdomain_is_dead(self):
        exc = socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")
        classification = classify_connection(exc)
        assert classification.status is Status.DEAD
        assert "NXDOMAIN" in classification.reason

    def test_temporary_dns_failure_is_blocked(self):
        exc = socket.gaierror(socket.EAI_AGAIN, "temporary failure in name resolution")
        assert classify_connection(exc).status is Status.BLOCKED

    def test_urlerror_wrappers_are_unwrapped(self):
        wrapped = urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "no such host"))
        assert classify_connection(wrapped).status is Status.DEAD

    def test_timeout_is_blocked(self):
        assert classify_connection(TimeoutError("timed out")).status is Status.BLOCKED
        wrapped = urllib.error.URLError(TimeoutError("timed out"))
        assert classify_connection(wrapped).status is Status.BLOCKED

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionRefusedError("refused"),
            ConnectionResetError("reset"),
            ssl.SSLError("tls handshake"),
            OSError("network unreachable"),
        ],
    )
    def test_other_connection_trouble_is_blocked(self, exc):
        classification = classify_connection(exc)
        assert classification.status is Status.BLOCKED
        assert classification.reason

    def test_reasons_are_scrubbed(self):
        exc = OSError("cannot reach https://example.test/x from /Users/lee/repo")
        reason = classify_connection(exc).reason
        assert "example.test" not in reason
        assert "/Users/lee" not in reason


class TestScrub:
    def test_urls_are_redacted(self):
        assert scrub("failed fetching https://example.test/a?b=c badly") == (
            "failed fetching <url> badly"
        )

    def test_emails_are_redacted(self):
        assert scrub("mail me@leeovery.com now") == "mail <email> now"

    @pytest.mark.parametrize(
        "path",
        ["/Users/lee/Code/x", "/home/lee/code", "C:\\Users\\lee\\code"],
    )
    def test_home_paths_are_redacted(self, path):
        assert "lee" not in scrub(f"open {path} failed")

    def test_multiline_collapses_to_one_line(self):
        assert scrub("boom\n  traceback line\n") == "boom traceback line"

    def test_clean_text_passes_through(self):
        assert scrub("plain message, nothing sensitive") == "plain message, nothing sensitive"


class TestProviderInputError:
    def test_is_a_typed_exception(self):
        with pytest.raises(ProviderInputError, match="corrupt"):
            raise ProviderInputError("corrupt audio stream")


class TestClassification:
    def test_frozen_value_object(self):
        classification = Classification(status=Status.BLOCKED, reason="HTTP 403")
        with pytest.raises(AttributeError):
            classification.status = Status.DEAD  # ty: ignore[invalid-assignment]
