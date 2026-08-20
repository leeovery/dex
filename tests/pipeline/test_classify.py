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

    @pytest.mark.parametrize("code", [101, 301, 302, 304, 399])
    def test_surfaced_1xx_3xx_is_blocked_never_a_crash(self, code):
        # Totality pin: a redirect-loop HTTPError surfaces as a non-2xx
        # response; classification must map it, not raise — a raise here
        # became a fake `error` in drivers and crashed the media stage.
        classification = classify_http(code)
        assert classification.status is Status.BLOCKED
        assert classification.reason == f"unexpected HTTP {code}"

    @pytest.mark.parametrize("code", [200, 204, 299])
    def test_success_codes_are_rejected(self, code):
        with pytest.raises(ValueError, match="non-success"):
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

    def test_home_paths_are_redacted_to_end_of_token(self):
        # The tail under the home dir is owner data too: repo names, item
        # slugs, media filenames.
        out = scrub(
            "open /Users/lee/Code/dex/dex-health/corpus/2026/"
            "2026-08-18-private-note-abc123.md failed"
        )
        assert out == "open <home> failed"

    @pytest.mark.parametrize(
        "token",
        [
            "corpus/2026/2026-08-19-secret-note-9a1b2c.md",
            "enrichment/2026-08-19-private-dispute-9a1b2c/x-112233.md",
            "media/1ba269/owner-chosen-name.jpg",
            "inbox/20260818-101530.md",
            "cache/audio/abc.mp3",
            "raw/discord/general/messages.json",
            "state/config.json",
            "dex-health/inbox/therapy.md",
            "dex-health",
        ],
    )
    def test_instance_content_tokens_are_redacted_whole(self, token):
        assert scrub(f"cannot write {token} today") == "cannot write <path> today"

    def test_bare_item_ids_are_redacted(self):
        # Item-id slugs derive from owner notes — they are content, not ids.
        assert scrub("unknown item 2026-08-19-secret-plan-ab12cd") == "unknown item <path>"

    def test_lookalike_words_pass_through(self):
        # "index-" contains "dex-" only mid-word; "estate/" is not "state/".
        assert scrub("index-page estate/plan mediation") == "index-page estate/plan mediation"

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
