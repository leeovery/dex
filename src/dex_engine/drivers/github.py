"""The github driver: repos / profiles / gists / issues / blobs via the gh CLI.

Every route goes through :mod:`dex_engine.drivers.gh`, the authenticated
seam this driver shares with the file driver. That module owns the ``gh``
invocation, the failure classification, and the whole blob round trip
including where a blob URL's ref stops and its path starts; this driver
owns only what a github URL's *shape* means and what to do with the bytes
that come back.

Blob bytes are sniffed *by name* before they are fenced: a document or any
other binary committed to a repo parks ``manual`` naming what it is,
because a GitHub blob URL serves an HTML viewer rather than the bytes, so
no other driver can re-fetch it — the rescue is to capture the file
itself. The name matters because two extractable shapes carry no
signature: a CSV, and a Git-LFS pointer, whose 130 bytes of stand-in text
would otherwise be fenced as though they were the document.
"""

import re
import urllib.parse

from dex_engine.pipeline.classify import Classification
from dex_engine.pipeline.detect import sniff_format
from dex_engine.pipeline.types import Kind, Result, Status, WorkUnit
from dex_engine.pipeline.urls import base_canonical, host_of

from .gh import BlobRef, Gh, blob_ref, fetch_blob, gh_api, gh_api_list, run_gh

__all__ = ["GitHubDriver"]

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


class GitHubDriver:
    """Fetch GitHub content by URL shape: gist, profile, blob, issue/PR, repo."""

    kind: Kind = Kind.GITHUB
    sleep: float = 0.3

    def __init__(self, *, gh: Gh = run_gh) -> None:
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
        blob = blob_ref(unit.url)
        if blob is not None:
            return self._fetch_blob(blob)
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
        repos = gh_api_list(self._gh, f"users/{user}/repos?sort=pushed&per_page=100")
        listing = _repo_listing(repos)
        meta = {"title": payload.get("name") or user, "followers": payload.get("followers")}
        body = (
            f"## Profile\n\n{payload.get('bio') or '(no bio)'}\n\n## Top repos\n\n"
            f"{listing or '(repo listing unavailable)'}"
        )
        return Result(status=Status.DONE, meta=meta, body=body)

    def _fetch_blob(self, ref: BlobRef) -> Result:
        blob = fetch_blob(self._gh, ref)
        if isinstance(blob, Classification):
            return _classified(blob)
        meta: dict[str, str | int | None] = {"file": blob.path}
        # Named, because a signature is not always there to find: an
        # unsmudged Git-LFS pointer is 130 bytes of honest UTF-8 standing in
        # for a document, and a CSV has no signature at all. Unnamed, both
        # decoded cleanly and fenced — the pointer text presented as the
        # document it stands for.
        fmt = sniff_format(blob.data, name=blob.path)
        if fmt is not None:
            return Result(
                status=Status.MANUAL,
                meta=meta,
                reason=(
                    f"{blob.path} is a {fmt.value} document, not source — capture the file "
                    "itself so the extractors can read it"
                ),
            )
        try:
            text = blob.data.decode("utf-8")
        except UnicodeDecodeError:
            # Decoding with errors="replace" fenced 40k characters of
            # replacement-character soup and ledgered it done.
            return Result(
                status=Status.MANUAL,
                meta=meta,
                reason=f"{blob.path} is binary, not UTF-8 text — there is nothing to fence",
            )
        return Result(status=Status.DONE, meta=meta, body=f"```\n{text[:_MAX_BLOB_CHARS]}\n```")

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

    def _api(self, endpoint: str) -> dict | Classification:
        return gh_api(self._gh, endpoint)


def _gist_id(segments: list[str]) -> str | None:
    """The gist id a gist.github.com path addresses, or None for index pages."""
    if len(segments) >= 2:  # noqa: PLR2004 — /user/<gist-id>
        return segments[1]
    if len(segments) == 1 and _GIST_ID_RE.fullmatch(segments[0]):
        return segments[0]
    return None


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
