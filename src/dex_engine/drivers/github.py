"""The github driver: repos, profiles, gists, issues, files and directories via the gh CLI.

Every route goes through :mod:`dex_engine.drivers.gh`, the authenticated
seam this driver shares with the file driver. That module owns the ``gh``
invocation, the failure classification, and the whole path round trip
including where a blob, raw or tree URL's ref stops and its path starts;
this driver owns only what a github URL's *shape* means and what to do
with what comes back.

A shape is read only by a route written for it. A repo link that is not
the repo itself, a path in it, an issue or a download — a commit, a
release page, a wiki — parks for judgment naming its shape, rather than
reading the repo's root README, which is not what the link points at.

File bytes are sniffed *by name* before they are fenced: a document
committed to a repo re-detects to ``file`` work — the file driver reads the
bytes back through the same seam, so extraction runs on the committed file
itself. Any other binary parks ``manual`` naming what it is, never a code
fence full of replacement characters. The name matters because two
extractable shapes carry no byte signature: a CSV, and an unsmudged Git-LFS
pointer, whose 130 bytes of stand-in text would otherwise be fenced as
though they were the document they point at.
"""

import re
import urllib.parse
from collections.abc import Callable

from dex_engine.pipeline.classify import Classification
from dex_engine.pipeline.detect import format_of_name, sniff_format
from dex_engine.pipeline.types import (
    Content,
    Job,
    Kind,
    Outcome,
    Redetected,
    Unusable,
    WorkUnit,
)
from dex_engine.pipeline.urls import base_canonical, host_of

from .gh import (
    Blob,
    Gh,
    RefPath,
    Tree,
    fetch_path,
    fetch_readme,
    gh_api,
    gh_api_list,
    ref_path,
    run_gh,
)

__all__ = ["GitHubDriver"]

_HOSTS = frozenset({"github.com", "gist.github.com"})

_ATTACHMENTS = "user-attachments"

# First path segments github.com reserves for its own product surfaces.
# None of them can be a user or an org, so none of them is repo or profile
# work: the API 404s them while a browser renders them fine, which turned
# a live marketing or topic page into a `dead` ledger line. Declining them
# hands the URL to the web driver, which extracts the page like any other —
# and a user-attachments link, which serves a file rather than a page, is
# rerouted on from there: a document by detection's HEAD sniff, a picture
# by the web driver's own look at the body.
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
        _ATTACHMENTS,
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
_TRUNCATED = "**Truncated:** cut at {kept:,} of {whole:,} characters; the rest is at {url}"
_TOP_REPOS = 15
_TOPIC_LIMIT = 8


class GitHubDriver:
    """Fetch GitHub content by URL shape: gist, profile, repo, file, directory, issue/PR."""

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

    def fetch(self, unit: WorkUnit) -> Outcome:
        """Dispatch on the URL shape."""
        parts = urllib.parse.urlsplit(unit.url)
        segments = [segment for segment in parts.path.split("/") if segment]
        if host_of(unit.url) == "gist.github.com":
            return self._fetch_gist(segments, unit.url)
        if not segments:
            # The root addresses no content unit, and no judgment could
            # pull one out of it either.
            return Unusable(evidence="github root url — nothing to fetch", rescuable=False)
        if segments[0].lower() == _ATTACHMENTS:
            # `matches` declines these now, but a unit ledgered github before
            # it did never re-detects: a requeue drives it by its stored kind.
            return _attachment(segments[1:])
        if len(segments) == 1:
            return self._fetch_profile(segments[0])
        address = ref_path(unit.url)
        if address is not None:
            return self._fetch_path(address, unit.url)
        return self._fetch_repo_page(segments[0], segments[1], segments[2:], unit.url)

    # -- routes ----------------------------------------------------------

    def _fetch_gist(self, segments: list[str], url: str) -> Outcome:
        gist_id = _gist_id(segments)
        if gist_id is None:
            return Unusable(evidence="gist index page — no single gist to fetch", rescuable=False)
        payload = self._api(f"gists/{gist_id}")
        if isinstance(payload, Classification):
            return payload.to_outcome()
        files = payload.get("files") or {}
        body = "\n\n".join(
            f"### {name}\n"
            + _capped((file or {}).get("content", ""), _MAX_GIST_FILE_CHARS, url=url, wrap=_fence)
            for name, file in files.items()
        )
        if not body:
            return Unusable(evidence="gist has no files")
        return Content(meta={"title": payload.get("description") or "gist"}, body=body)

    def _fetch_profile(self, user: str) -> Outcome:
        payload = self._api(f"users/{user}")
        if isinstance(payload, Classification):
            return payload.to_outcome()
        repos = gh_api_list(self._gh, f"users/{user}/repos?sort=pushed&per_page=100")
        listing = _repo_listing(repos)
        meta = {"title": payload.get("name") or user, "followers": payload.get("followers")}
        body = (
            f"## Profile\n\n{payload.get('bio') or '(no bio)'}\n\n## Top repos\n\n"
            f"{listing or '(repo listing unavailable)'}"
        )
        return Content(meta=meta, body=body)

    def _fetch_repo_page(self, owner: str, repo: str, rest: list[str], url: str) -> Outcome:
        """Everything a repo URL addresses beyond a path at a ref; no route, no read."""
        match rest:
            case []:
                return self._fetch_repo(owner, repo, url)
            case ["issues" | "pull", number, *_] if number.isdigit():
                return self._fetch_issue(owner, repo, number)
            case ["releases", "download", _, *_, asset] | ["releases", "latest", "download", asset]:
                return _download(asset)
            case ["archive", *_, name]:
                return _download(name)
            case _:
                return _unrouted(rest[0])

    def _fetch_path(self, address: RefPath, url: str) -> Outcome:
        found = fetch_path(self._gh, address)
        if isinstance(found, Classification):
            return found.to_outcome()
        if isinstance(found, Blob):
            return _file_outcome(found, url)
        if not found.path:
            return self._fetch_repo(address.owner, address.repo, url, ref=found.ref)
        return self._fetch_directory(address, found, url)

    def _fetch_directory(self, address: RefPath, tree: Tree, url: str) -> Outcome:
        readme = fetch_readme(self._gh, address.owner, address.repo, ref=tree.ref, path=tree.path)
        if isinstance(readme, Classification):
            return readme.to_outcome()
        listing = "\n".join(f"- `{name}`" for name in tree.entries)
        return Content(
            meta={"title": f"{address.owner}/{address.repo}/{tree.path}"},
            body=f"## README\n\n{_readme_body(readme, url)}\n\n## Contents\n\n{listing}",
        )

    def _fetch_issue(self, owner: str, repo: str, number: str) -> Outcome:
        payload = self._api(f"repos/{owner}/{repo}/issues/{number}")
        if isinstance(payload, Classification):
            return payload.to_outcome()
        return Content(
            meta={"title": payload.get("title")},
            body=payload.get("body") or "(no body)",
        )

    def _fetch_repo(self, owner: str, repo: str, url: str, *, ref: str | None = None) -> Outcome:
        payload = self._api(f"repos/{owner}/{repo}")
        if isinstance(payload, Classification):
            return payload.to_outcome()
        readme = fetch_readme(self._gh, owner, repo, ref=ref)
        if isinstance(readme, Classification):
            return readme.to_outcome()
        meta: dict[str, str | int | None] = {
            "title": payload.get("full_name"),
            "description": payload.get("description") or None,
            "stars": payload.get("stargazers_count"),
            "archived": "true" if payload.get("archived") else None,
            "topics": ", ".join((payload.get("topics") or [])[:_TOPIC_LIMIT]) or None,
            # Which ref's README this is, when a link named one. Migration 15
            # reads its absence to tell the default branch's root README the
            # old dispatch stored for every repo link from a landing of this
            # route, so it must never be dropped from a ref'd reading.
            "ref": ref,
        }
        return Content(meta=meta, body=_readme_body(readme, url))

    def _api(self, endpoint: str) -> dict | Classification:
        return gh_api(self._gh, endpoint)


def _file_outcome(blob: Blob, url: str) -> Outcome:
    """A file fenced as source, re-detected as a document, or parked as any other binary."""
    # Named, because a signature is not always there to find: an
    # unsmudged Git-LFS pointer is 130 bytes of honest UTF-8 standing in
    # for a document, and a CSV has no signature at all. Unnamed, both
    # decoded cleanly and fenced — the pointer text presented as the
    # document it stands for.
    fmt = sniff_format(blob.data, name=blob.path)
    if fmt is not None:
        # A document committed to a repo IS extractable work: the file
        # driver reaches these bytes back through the shared seam, so
        # the refusal that used to park this manual ("no other driver
        # can re-fetch a blob URL") no longer holds. An LFS pointer that
        # sniffs by name is caught there, before any extractor sees it.
        return Redetected(kind=Kind.FILE, format=fmt)
    try:
        text = blob.data.decode("utf-8")
    except UnicodeDecodeError:
        # Decoding with errors="replace" fenced 40k characters of
        # replacement-character soup and ledgered it done.
        return Unusable(
            evidence=f"{blob.path} is binary, not UTF-8 text — there is nothing to fence"
        )
    return Content(
        meta={"file": blob.path}, body=_capped(text, _MAX_BLOB_CHARS, url=url, wrap=_fence)
    )


def _attachment(rest: list[str]) -> Outcome:
    """What a user-attachments link serves: a file to extract, or a picture to keep."""
    match rest:
        case ["files", _, name]:
            return _download(name)
        case ["assets", _]:
            # Media work, named directly: correcting to web would have the
            # web driver correct it again once it saw the picture, and a
            # second correction of one unit in one run parks as a loop.
            return Redetected(kind=Kind.WEB, job=Job.MEDIA)
        case _:
            return _unrouted(_ATTACHMENTS)


def _download(name: str) -> Outcome:
    """File work for a download named as a document; a park for any other."""
    # Screened by name before a byte moves: the file driver reads a URL's
    # whole body before it can sniff it, and a release asset is as often
    # a 200MB installer as a PDF.
    name = urllib.parse.unquote(name)
    fmt = format_of_name(name)
    if fmt is None:
        return Unusable(evidence=f"{name} is a download, not a document the file driver extracts")
    return Redetected(kind=Kind.FILE, format=fmt)


def _unrouted(shape: str) -> Unusable:
    """The park for a github link no route reads: a commit, a release page, a wiki."""
    return Unusable(
        evidence=(
            f"github /{shape}/ link — the driver reads repos, directories, files and issues, "
            "and has no route for this one"
        )
    )


def _readme_body(readme: str | None, url: str) -> str:
    return _capped(readme or "", _MAX_README_CHARS, url=url, wrap=str.rstrip) or "(no README)"


def _capped(text: str, cap: int, *, url: str, wrap: Callable[[str], str]) -> str:
    """``text`` wrapped whole when it fits, else its lines up to ``cap`` and a line saying so.

    A silent prefix is read as the whole file: the digest written from it
    cannot know the rest exists. So a cut is followed, outside whatever
    ``wrap`` puts round the text, by one line naming how much of how much
    was kept and where the rest is. The cut falls at the last line end the
    cap allows, where that keeps at least half of it; text whose last line
    end inside the cap falls earlier — a minified file under a one-line
    header, a README with one huge line of HTML — is cut mid-line at the
    cap, rather than kept to a few characters.
    """
    if len(text) <= cap:
        return wrap(text)
    end = text.rfind("\n", 0, cap + 1)
    kept = text[:end] if end >= cap // 2 else text[:cap]
    return f"{wrap(kept)}\n\n{_TRUNCATED.format(kept=len(kept), whole=len(text), url=url)}"


def _fence(text: str) -> str:
    return f"```\n{text}\n```"


def _gist_id(segments: list[str]) -> str | None:
    """The gist id a gist.github.com path addresses, or None for index pages."""
    if len(segments) >= 2:  # noqa: PLR2004 — /user/<gist-id>
        return segments[1]
    if len(segments) == 1 and _GIST_ID_RE.fullmatch(segments[0]):
        return segments[0]
    return None


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
