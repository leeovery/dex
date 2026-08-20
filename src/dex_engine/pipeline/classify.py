"""Centralized failure classification and the shared scrubber (§5).

Failure classification is never per-driver: the motivating incident was a
classification bug in one fetcher, and seven drivers classifying
independently would be seven chances to reintroduce it. Every driver's HTTP
path routes its outcome through :func:`classify_http` /
:func:`classify_connection`; the §15 regression pin tests this module once
and holds for all drivers.

A classification carries the status *and* the stated reason together — a
``manual`` outcome requires a reason (§5), and letting each driver invent
one would put classification judgment back in seven places.
"""

import re
import socket
import ssl
import urllib.error
from dataclasses import dataclass

from .types import Status

__all__ = [
    "MIN_SUBSTANTIAL_CHARS",
    "PAYWALL_REASON",
    "THIN_EXTRACTION_REASON",
    "Classification",
    "ProviderInputError",
    "ProviderUnavailableError",
    "ScannedDocumentError",
    "classify_connection",
    "classify_http",
    "scrub",
]


# A successful fetch whose extraction comes back with fewer characters than
# this is "thin" — our tooling can't read it (JS shells, consent walls). It
# parks `manual` with THIN_EXTRACTION_REASON, never `dead` (§5).
MIN_SUBSTANTIAL_CHARS = 300
THIN_EXTRACTION_REASON = "thin-extraction"

# Login-walls and paywalls: retrying never resolves payment-required, so
# burning blocked attempts on it teaches nothing (§5).
PAYWALL_REASON = "payment/login required"


# 2xx is the callers' success check (HttpResponse.ok); everything else —
# including a surfaced 1xx/3xx — classifies, so the contract is total over
# every non-success status a transport can hand back.
_SUCCESS_FLOOR = 200
_FAILURE_FLOOR = 400


class ProviderInputError(Exception):
    """A capability provider was handed input it cannot process (§5).

    Providers raise this for bad *inputs* (corrupt audio, unparseable file);
    the run loop maps it to ``manual``. Anything uncaught is an engine bug
    and takes the ``error`` path instead.
    """


class ProviderUnavailableError(Exception):
    """A provider that reported available() failed at call time anyway (§6).

    The capability-level failure modes ``available()`` cannot see up front:
    a rejected API key, a rate limit, a model download that failed mid-way.
    The transcribe drain maps this to *the job stays ``waiting``* with the
    stated reason — §6 semantics discovered late: the mechanical provider
    is, in truth, not available, and waiting has no escalation clock.
    """


class ScannedDocumentError(Exception):
    """A document with no extractable text — image-only or scanned (§6).

    Extract providers raise this instead of returning empty markdown; the
    file driver maps it to the OCR path (``waiting`` + ``needs: ocr``),
    where the cognitive floor reads the pages with eyes.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class Classification:
    """A classified fetch failure: the status plus the stated reason."""

    status: Status
    reason: str


def classify_http(status_code: int) -> Classification:
    """Classify a non-success HTTP status code (§5) — total over non-2xx.

    403/429/5xx → ``blocked`` (the world misbehaved; retried every run),
    404/410 → ``dead`` (confirmed gone), 401/402 → ``manual`` with
    ``payment/login required`` (x.com answers 402; retrying never resolves
    it). Any other failing code is ``blocked``, and so is a surfaced
    1xx/3xx (a redirect loop's final 30x, an out-of-place informational
    response): the floor is that nothing is ever silently mislabeled into a
    terminal state, and no non-success status may crash a run.

    Args:
        status_code: The HTTP response status; anything outside 2xx.

    Returns:
        The classification, reason included.

    Raises:
        ValueError: ``status_code`` is a success (2xx) — that is the
            caller's branch, not a classification.
    """
    if _SUCCESS_FLOOR <= status_code < 300:  # noqa: PLR2004 — the 2xx band is self-naming
        raise ValueError(f"classify_http is for non-success statuses, got {status_code}")
    if status_code < _FAILURE_FLOOR:
        # A 1xx/3xx that reached a caller unresolved: the world is
        # misbehaving (redirect loops, broken upgrade dances) — blocked,
        # never an engine error, never terminal.
        return Classification(status=Status.BLOCKED, reason=f"unexpected HTTP {status_code}")
    if status_code in (401, 402):
        return Classification(status=Status.MANUAL, reason=f"{PAYWALL_REASON} (HTTP {status_code})")
    if status_code in (404, 410):
        return Classification(status=Status.DEAD, reason=f"HTTP {status_code}")
    # 403/429/5xx and every unlisted failure: blocked, never dead — a
    # transient challenge heals on retry; a persistent one escalates to
    # manual after 5 attempts with the truth recorded.
    return Classification(status=Status.BLOCKED, reason=f"HTTP {status_code}")


def classify_connection(exc: OSError) -> Classification:
    """Classify a connection-level failure (DNS, refused, reset, timeout).

    NXDOMAIN (``EAI_NONAME``) is the one connection failure that means
    *confirmed gone* → ``dead``. Everything else — temporary DNS failure,
    refused/reset connections, timeouts, TLS trouble — is the world
    misbehaving → ``blocked``.

    Args:
        exc: The raised failure; ``urllib.error.URLError`` wrappers are
            unwrapped to the underlying cause.

    Returns:
        The classification, reason included.
    """
    cause: BaseException = exc
    while isinstance(cause, urllib.error.URLError) and isinstance(cause.reason, BaseException):
        cause = cause.reason
    if isinstance(cause, socket.gaierror):
        if cause.errno == socket.EAI_NONAME:
            return Classification(status=Status.DEAD, reason="DNS: no such domain (NXDOMAIN)")
        return Classification(
            status=Status.BLOCKED, reason=f"temporary DNS failure ({scrub(str(cause))})"
        )
    if isinstance(cause, TimeoutError):
        return Classification(status=Status.BLOCKED, reason="connection timed out")
    if isinstance(cause, ssl.SSLError):
        return Classification(status=Status.BLOCKED, reason=f"TLS failure ({scrub(str(cause))})")
    return Classification(status=Status.BLOCKED, reason=f"connection failed ({scrub(str(cause))})")


# One scrubber feeds both the ledger `error` field and the issue filer's
# bodies (§5/§13). Code, not judgment, so it can't leak by judgment lapse.
_URL_RE = re.compile(r"https?://\S+")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# A home-anchored path leaks the username AND everything under it (the
# instance repo name, item slugs, media filenames) — redact to end-of-token,
# never just the user segment.
_HOME_RE = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)\S*")
_TOKEN_RE = re.compile(r"\S+")
# Instance content is owner data even in RELATIVE paths (they never touch
# the home redaction): item ids embed note-derived slugs, media names are
# owner-chosen, and dex-* names the owner's instance repo. Any token
# carrying an instance-directory segment, a dex-* segment, or an
# item-id-shaped substring is redacted whole.
_INSTANCE_TOKEN_RE = re.compile(
    r"(?:^|[^\w])(?:corpus|enrichment|media|inbox|cache|raw|state)[/\\]"
    r"|(?:^|[^\w])dex-\w"
    r"|\d{4}-\d{2}-\d{2}-[a-z0-9-]+-[0-9a-f]{6}"
)


def _redact_token(match: re.Match[str]) -> str:
    token = match.group(0)
    return "<path>" if _INSTANCE_TOKEN_RE.search(token) else token


def scrub(text: str) -> str:
    """Redact URLs, emails, home paths, and instance-content tokens; one line.

    Feeds every message that leaves the fetch path for the ledger ``error``
    field and the issue filer's bodies. Whole-token redaction is deliberate:
    a partially redacted path still leaks the owner-authored tail.

    Args:
        text: The raw message (an exception string, typically).

    Returns:
        The redacted, single-line message.
    """
    text = _URL_RE.sub("<url>", text)
    text = _EMAIL_RE.sub("<email>", text)
    text = _HOME_RE.sub("<home>", text)
    text = _TOKEN_RE.sub(_redact_token, text)
    return " ".join(text.split())
