"""URL canonicalization primitives and the work-key hash.

Canonicalization keys the ledger: ``work_hash(driver.canonical(url))`` is a
unit's identity, so these rules deliberately preserve the pre-rewrite
enricher's hashing semantics (scheme forced to https, ``www.``/``m.`` host
prefixes stripped, tracking params dropped, trailing slashes and fragments
removed) — changing them would orphan every existing ledger entry.

Idempotency is load-bearing: ``canonical(canonical(u)) == canonical(u)`` is
a hypothesis property, because a non-idempotent case IS a duplicate-entry bug.
"""

import hashlib
import urllib.parse

__all__ = [
    "STRIP_PARAMS",
    "base_canonical",
    "host_of",
    "work_hash",
]

# Tracking/share noise stripped from every query string.
STRIP_PARAMS = frozenset(
    {
        "si",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "feature",
        "ref",
        "ref_src",
        "lang",
        "app",
    }
)

_HOST_PREFIXES = ("www.", "m.")


def _strip_host(netloc: str) -> str:
    host = netloc.lower()
    # Loop until stable: a single removeprefix pass would leave "m.example"
    # inside "m.m.example" and break canonical idempotency.
    stripped = True
    while stripped:
        stripped = False
        for prefix in _HOST_PREFIXES:
            if host.startswith(prefix):
                host = host[len(prefix) :]
                stripped = True
    return host


def host_of(url: str) -> str:
    """The lowercased host (port kept) with ``www.``/``m.`` prefixes stripped.

    The one host form drivers match patterns against — the historical
    ``m.youtube.com`` divergence existed because two ``kind_of`` copies
    stripped different prefixes.
    """
    return _strip_host(urllib.parse.urlsplit(url).netloc)


def base_canonical(url: str, *, keep_params: frozenset[str] | None = None) -> str:
    """Canonicalize a URL: https scheme, stripped host, filtered params.

    Args:
        url: An absolute http(s) URL.
        keep_params: When given, only these query params survive (the
            podcast driver keeps the Apple episode param alone); otherwise
            everything except :data:`STRIP_PARAMS` survives.

    Returns:
        The canonical form — fragment dropped, trailing path slashes
        stripped, query re-encoded in original order.
    """
    parts = urllib.parse.urlsplit(url)
    host = _strip_host(parts.netloc)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query)
        if key not in STRIP_PARAMS and (keep_params is None or key in keep_params)
    ]
    return urllib.parse.urlunsplit(
        ("https", host, parts.path.rstrip("/"), urllib.parse.urlencode(query), "")
    )


def work_hash(work_key: str) -> str:
    """The ledger key for a unit of work: ``sha1(work key)[:10]``.

    The work key is a canonical URL, or ``file:<repo-path>`` for local files.
    """
    return hashlib.sha1(work_key.encode("utf-8")).hexdigest()[:10]  # noqa: S324 — content key, not a security context
