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
from pathlib import Path

__all__ = [
    "STRIP_PARAMS",
    "base_canonical",
    "ext_of",
    "host_of",
    "resolve_repo_path",
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


def ext_of(url: str, *, default: str) -> str:
    """The file extension the URL's path tail names, or ``default``.

    Names a downloaded body's file on disk (a media image, a cached audio
    enclosure) — the caller states the default its media family warrants.
    An "extension" longer than 4 characters or carrying non-alphanumerics
    is a slug's tail, not a format, and takes the default too.
    """
    tail = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else ""
    if not ext or len(ext) > 4 or not ext.isalnum():  # noqa: PLR2004 — 4-char extension heuristic
        return default
    return ext


def resolve_repo_path(root: Path, repo_path: str) -> Path | None:
    """Resolve a repo-relative path strictly under the instance root.

    The one containment check: a ``file:`` work key's path, and the paths
    ``dex-exclude`` deletes. Both come from data an owner or the scope
    filter wrote, so they are never trusted as paths: an absolute path
    (which ``root / path`` would silently substitute for the root), a ``..``
    climb, or a symlink pointing outside the root all resolve elsewhere.
    Both sides are resolved before the containment check so symlinks cannot
    smuggle a path out.

    Args:
        root: The instance root directory.
        repo_path: The repo-relative path to resolve.

    Returns:
        The resolved path, or ``None`` when it escapes the root or cannot
        be a path at all (an embedded NUL byte) — the drain parks the unit
        and never reads the bytes; ``exclude`` refuses the batch.
    """
    try:
        resolved = (root / repo_path).resolve()
    except ValueError:
        return None
    if not resolved.is_relative_to(root.resolve()):
        return None
    return resolved


def work_hash(work_key: str) -> str:
    """The ledger key for a unit of work: ``sha1(work key)[:10]``.

    The work key is a canonical URL, or ``file:<repo-path>`` for local files.
    """
    return hashlib.sha1(work_key.encode("utf-8")).hexdigest()[:10]  # noqa: S324 — content key, not a security context
