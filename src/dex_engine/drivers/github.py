"""The github driver: repos / profiles / gists / issues / blobs via the gh CLI.

``gh`` is a declared instance dependency — an environment without it
is not a dex environment, so a missing binary propagates as an engine
error rather than being classified. API failures ARE classified: gh's
stderr names the HTTP status (``gh: Not Found (HTTP 404)``), which routes
through the central classifier; anything without a visible code is
``blocked``, never silently terminal.
"""

import base64
import binascii
import json
import re
import subprocess
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from dex_engine.pipeline.classify import (
    Classification,
    classify_http,
    scrub,
)
from dex_engine.pipeline.types import Kind, Result, Status, WorkUnit
from dex_engine.pipeline.urls import base_canonical, host_of

__all__ = ["GhResult", "GitHubDriver", "run_gh"]

_HOSTS = frozenset({"github.com", "gist.github.com"})

# First path segments github.com reserves for its own product surfaces.
# None of them can be a user or an org, so none of them is repo or profile
# work: the API 404s them while a browser renders them fine, which turned
# a live marketing or topic page into a `dead` ledger line. Declining them
# hands the URL to the web driver, which extracts the page like any other.
_RESERVED_SEGMENTS = frozenset(
    {
        "about",
        "account",
        "apps",
        "blog",
        "business",
        "codespaces",
        "collections",
        "contact",
        "copilot",
        "dashboard",
        "discussions",
        "education",
        "enterprise",
        "events",
        "explore",
        "features",
        "home",
        "issues",
        "join",
        "login",
        "logout",
        "marketplace",
        "mobile",
        "new",
        "notifications",
        "organizations",
        "orgs",
        "pricing",
        "pulls",
        "readme",
        "search",
        "security",
        "sessions",
        "settings",
        "signup",
        "site",
        "sitemap",
        "solutions",
        "sponsors",
        "stars",
        "team",
        "topics",
        "trending",
        "wiki",
    }
)

_GH_HTTP_RE = re.compile(r"HTTP (\d{3})")
_GH_TIMEOUT = 120.0

# Gist id shapes in the wild: 32-char hex today, 20-char hex from the 2013
# era, short sequential decimals before that. A bare gist.github.com/<id>
# link is a legacy share shape that still resolves; a one-segment path that
# cannot be a gist id is a username's index page.
_GIST_ID_RE = re.compile(r"[0-9a-f]{32}|[0-9a-f]{20}|\d{1,8}")

# Body size ceilings, ported from the proven enricher.
_MAX_GIST_FILE_CHARS = 20_000
_MAX_BLOB_CHARS = 40_000
_MAX_README_CHARS = 60_000
_TOP_REPOS = 15
_TOPIC_LIMIT = 8


@dataclass(frozen=True, slots=True, kw_only=True)
class GhResult:
    """One ``gh`` invocation's outcome."""

    returncode: int
    stdout: str
    stderr: str


def run_gh(args: Sequence[str]) -> GhResult:
    """Run ``gh`` with ``args``; a missing binary raises (engine error).

    A hung invocation is the world misbehaving, not an engine bug: the
    timeout comes back as a failed GhResult so classification makes it
    ``blocked`` (no HTTP code in the stderr), never ``error``.
    """
    try:
        completed = subprocess.run(  # noqa: S603 — gh is a declared dependency; args are code-built, no shell
            ["gh", *args],  # noqa: S607 — resolved from PATH by design (instances install gh, not a path)
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return GhResult(returncode=124, stdout="", stderr=f"gh timed out after {_GH_TIMEOUT:g}s")
    return GhResult(
        returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )


class GitHubDriver:
    """Fetch GitHub content by URL shape: gist, profile, blob, issue/PR, repo."""

    kind: Kind = Kind.GITHUB
    sleep: float = 0.3

    def __init__(self, *, gh: Callable[[Sequence[str]], GhResult] = run_gh) -> None:
        """Wire the gh-CLI seam.

        Args:
            gh: Runs a ``gh`` invocation; injected so tests are hermetic.
        """
        self._gh = gh

    def matches(self, url: str) -> bool:
        """True for github.com and gist.github.com, minus reserved namespaces."""
        host = host_of(url)
        if host not in _HOSTS:
            return False
        if host == "gist.github.com":
            return True
        segments = [segment for segment in urllib.parse.urlsplit(url).path.split("/") if segment]
        return not segments or segments[0].lower() not in _RESERVED_SEGMENTS

    def canonical(self, url: str) -> str:
        """The generic canonical form."""
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Result:
        """Dispatch on the URL shape."""
        parts = urllib.parse.urlsplit(unit.url)
        segments = [segment for segment in parts.path.split("/") if segment]
        if host_of(unit.url) == "gist.github.com":
            return self._fetch_gist(segments)
        if not segments:
            return Result(
                status=Status.SKIPPED, meta={}, reason="github root url — nothing to fetch"
            )
        if len(segments) == 1:
            return self._fetch_profile(segments[0])
        owner, repo = segments[0], segments[1]
        if len(segments) >= 4 and segments[2] == "blob":  # noqa: PLR2004 — /owner/repo/blob/<ref>/…
            return self._fetch_blob(owner, repo, segments[3:])
        if len(segments) >= 4 and segments[2] in ("issues", "pull"):  # noqa: PLR2004
            return self._fetch_issue(owner, repo, segments[3])
        return self._fetch_repo(owner, repo)

    # -- routes ----------------------------------------------------------

    def _fetch_gist(self, segments: list[str]) -> Result:
        gist_id = _gist_id(segments)
        if gist_id is None:
            return Result(
                status=Status.SKIPPED, meta={}, reason="gist index page — no single gist to fetch"
            )
        payload = self._api(f"gists/{gist_id}")
        if isinstance(payload, Classification):
            return _classified(payload)
        files = payload.get("files") or {}
        body = "\n\n".join(
            f"### {name}\n```\n{(file or {}).get('content', '')[:_MAX_GIST_FILE_CHARS]}\n```"
            for name, file in files.items()
        )
        meta = {"title": payload.get("description") or "gist"}
        if not body:
            return Result(status=Status.MANUAL, meta=meta, reason="gist has no files")
        return Result(status=Status.DONE, meta=meta, body=body)

    def _fetch_profile(self, user: str) -> Result:
        payload = self._api(f"users/{user}")
        if isinstance(payload, Classification):
            return _classified(payload)
        repos = self._api_list(f"users/{user}/repos?sort=pushed&per_page=100")
        listing = _repo_listing(repos)
        meta = {"title": payload.get("name") or user, "followers": payload.get("followers")}
        body = (
            f"## Profile\n\n{payload.get('bio') or '(no bio)'}\n\n## Top repos\n\n"
            f"{listing or '(repo listing unavailable)'}"
        )
        return Result(status=Status.DONE, meta=meta, body=body)

    def _fetch_blob(self, owner: str, repo: str, tail: list[str]) -> Result:
        ref, file_path = tail[0], "/".join(tail[1:])
        payload = self._api(
            f"repos/{owner}/{repo}/contents/{_requote(file_path, safe='/')}"
            f"?ref={_requote(ref, safe='')}"
        )
        if isinstance(payload, Classification):
            return _classified(payload)
        data = _blob_bytes(payload)
        meta: dict[str, str | int | None] = {"file": file_path}
        if data is None:
            # Over 1MB the contents API serves `encoding: "none"` and an
            # empty body — the file exists, we just cannot read it here.
            return Result(
                status=Status.MANUAL,
                meta=meta,
                reason=(
                    f"{file_path} is larger than the contents API serves inline — "
                    "read it from a clone"
                ),
            )
        body = f"```\n{data.decode('utf-8', 'replace')[:_MAX_BLOB_CHARS]}\n```"
        return Result(status=Status.DONE, meta=meta, body=body)

    def _fetch_issue(self, owner: str, repo: str, number: str) -> Result:
        payload = self._api(f"repos/{owner}/{repo}/issues/{number}")
        if isinstance(payload, Classification):
            return _classified(payload)
        return Result(
            status=Status.DONE,
            meta={"title": payload.get("title")},
            body=payload.get("body") or "(no body)",
        )

    def _fetch_repo(self, owner: str, repo: str) -> Result:
        payload = self._api(f"repos/{owner}/{repo}")
        if isinstance(payload, Classification):
            return _classified(payload)
        meta: dict[str, str | int | None] = {
            "title": payload.get("full_name"),
            "description": payload.get("description") or None,
            "stars": payload.get("stargazers_count"),
            "archived": "true" if payload.get("archived") else None,
            "topics": ", ".join((payload.get("topics") or [])[:_TOPIC_LIMIT]) or None,
        }
        readme = self._gh(
            ["api", f"repos/{owner}/{repo}/readme", "-H", "Accept: application/vnd.github.raw+json"]
        )
        body = readme.stdout if readme.returncode == 0 else "(no README)"
        return Result(status=Status.DONE, meta=meta, body=body[:_MAX_README_CHARS])

    # -- gh plumbing -----------------------------------------------------

    def _api(self, endpoint: str) -> dict | Classification:
        """``gh api <endpoint>`` parsed as a JSON object, or the classified failure."""
        result = self._gh(["api", endpoint])
        if result.returncode != 0:
            return _classify_gh_failure(result.stderr)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return Classification(status=Status.BLOCKED, reason="gh api returned unparseable JSON")
        if not isinstance(payload, dict):
            return Classification(
                status=Status.BLOCKED, reason="gh api returned an unexpected shape"
            )
        return payload

    def _api_list(self, endpoint: str) -> list:
        """``gh api <endpoint>`` parsed as a JSON array; failures yield [].

        Only the non-essential profile repo listing uses this — the profile
        itself already classified successfully, and a missing listing is
        noted in the body rather than failing the unit.
        """
        result = self._gh(["api", endpoint])
        if result.returncode != 0:
            return []
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []


def _requote(value: str, *, safe: str) -> str:
    """Encode a URL piece for the API endpoint without double-encoding it."""
    return urllib.parse.quote(urllib.parse.unquote(value), safe=safe)


def _blob_bytes(payload: dict) -> bytes | None:
    """The blob's bytes, or None when the API served no inline content."""
    if payload.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(payload.get("content") or "", validate=False)
    except (binascii.Error, ValueError):
        return None


def _gist_id(segments: list[str]) -> str | None:
    """The gist id a gist.github.com path addresses, or None for index pages."""
    if len(segments) >= 2:  # noqa: PLR2004 — /user/<gist-id>
        return segments[1]
    if len(segments) == 1 and _GIST_ID_RE.fullmatch(segments[0]):
        return segments[0]
    return None


def _classify_gh_failure(stderr: str) -> Classification:
    match = _GH_HTTP_RE.search(stderr)
    if match:
        return classify_http(int(match.group(1)))
    return Classification(status=Status.BLOCKED, reason=f"gh api failed: {scrub(stderr)}")


def _classified(failure: Classification) -> Result:
    return Result(status=failure.status, meta={}, reason=failure.reason)


def _repo_listing(repos: list) -> str:
    top = sorted(
        (repo for repo in repos if isinstance(repo, dict)),
        key=lambda repo: -(repo.get("stargazers_count") or 0),
    )[:_TOP_REPOS]
    return "\n".join(
        f"- **{repo.get('name')}** ({repo.get('stargazers_count', 0)}★): "
        f"{repo.get('description') or ''}"
        for repo in top
    )
