"""Tests for pipeline/classify.py: the one classification point."""

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
        # THE regression pin: the motivating incident was a 403
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
        # 402: x.com answers it; retrying never resolves payment-required.
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
        exc = OSError("cannot reach https://example.test/x from /Users/owner/repo")
        reason = classify_connection(exc).reason
        assert "example.test" not in reason
        assert "/Users/owner" not in reason


class TestScrub:
    def test_urls_are_redacted(self):
        assert scrub("failed fetching https://example.test/a?b=c badly") == (
            "failed fetching <url> badly"
        )

    def test_emails_are_redacted(self):
        assert scrub("mail owner@example.com now") == "mail <email> now"

    def test_urls_are_redacted_whatever_the_case(self):
        assert scrub("fetch HTTPS://Example.TEST/A?b=C failed") == "fetch <url> failed"

    @pytest.mark.parametrize(
        "path",
        [
            "/Users/owner/Code/x",
            "/home/owner/code",
            "/root/code",
            "/var/home/owner/code",
            "C:\\Users\\owner\\code",
            "C:/Users/owner/code",
        ],
    )
    def test_home_paths_are_redacted(self, path):
        assert "owner" not in scrub(f"open {path} failed")

    @pytest.mark.parametrize(
        "message",
        [
            "[Errno 2] No such file: '/Users/owner/dex-notes/media/Some Podcast Episode.mp3'",
            "cannot read [/root/notes/My Big File.pdf] now",
            "ffmpeg: /var/home/owner/media/Some Podcast Episode.mp3: invalid data",
            "copy C:/Users/owner/Docs/My Notes.txt failed",
        ],
    )
    def test_a_space_in_the_filename_does_not_truncate_the_redaction(self, message):
        # The tail of an owner-chosen filename is owner data; stopping at
        # the first space leaked it ("<home> Podcast Episode.mp3").
        out = scrub(message)
        assert "owner" not in out
        assert "Podcast" not in out
        assert "Big" not in out
        assert "Notes" not in out

    def test_the_redaction_stops_at_the_path_and_keeps_the_prose(self):
        # Over-redaction hides the failure: everything past the filename is
        # the message the session has to read.
        assert scrub("open /home/owner/x and check the log") == "open <home> and check the log"
        assert scrub("open /Users/owner/Code/x failed") == "open <home> failed"

    def test_home_paths_are_redacted_to_end_of_token(self):
        # The tail under the home dir is owner data too: repo names, item
        # slugs, media filenames.
        out = scrub(
            "open /Users/owner/Code/dex/dex-notes/corpus/2026/"
            "2026-08-18-laravel-queue-retries-abc123.md failed"
        )
        assert out == "open <home> failed"

    @pytest.mark.parametrize(
        "token",
        [
            "corpus/2026/2026-08-19-agentic-coding-notes-9a1b2c.md",
            "enrichment/2026-08-19-claude-skills-review-9a1b2c/x-112233.md",
            "media/1ba269/owner-chosen-name.jpg",
            "inbox/20260818-101530.md",
            "cache/audio/abc.mp3",
            "raw/discord/general/messages.json",
            "state/config.json",
            "dex-notes/inbox/claude-skills-review.md",
            "dex-notes",
        ],
    )
    def test_instance_content_tokens_are_redacted_whole(self, token):
        assert scrub(f"cannot write {token} today") == "cannot write <path> today"

    def test_bare_item_ids_are_redacted(self):
        # Item-id slugs derive from owner notes — they are content, not ids.
        assert scrub("unknown item 2026-08-19-claude-agent-teardown-ab12cd") == (
            "unknown item <path>"
        )

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
