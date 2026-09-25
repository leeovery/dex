"""The drivers' authenticated GitHub seam: ``gh`` calls, and what a repo path holds.

Two drivers need the same authenticated route into a repo, and neither
owns it: the github driver reads a path to fence a file as source or list
a directory, and the file driver re-reads a file when its bytes turn out
to be a document to extract. Everything that route involves lives here —
the ``gh`` invocation, the JSON parse and its failure classification, and
the whole path round trip: where a ``/blob/``, ``/raw/`` or ``/tree/``
URL's ref stops and its path starts, the contents-API endpoint, the base64
decode, a directory's listing and README, and the park for a file too
large to be served inline.

It sits beside :mod:`dex_engine.drivers.transport`, for that module's
reason: a seam several drivers share is not itself a driver, and a driver
must never import another driver. Like transport it reaches only for
pipeline vocabulary — nothing here knows about work units or results.

``gh`` is a declared instance dependency — an environment without it is not
a dex environment, so a missing binary propagates as an engine error rather
than being classified. API failures ARE classified: gh's stderr names the
HTTP status (``gh: Not Found (HTTP 404)``), which routes through the
central classifier; anything without a visible code is ``blocked``, never
silently terminal.

``raw.githubusercontent.com`` is not an option for the bytes: it is
unauthenticated, so it 404s every private-repo blob however the machine is
signed in, and that 404 classified live content ``dead``. Losing that host
costs the ref/path boundary it used to resolve server-side, which is why
:func:`fetch_path` has to settle the boundary itself — and why it answers
with the ref and path the winning split left, since that name is what the
bytes have to be sniffed under and where a directory's README is read.
"""

import base64
import binascii
import json
import re
import subprocess
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from dex_engine.pipeline.classify import Classification, classify_http, scrub
from dex_engine.pipeline.types import Status
from dex_engine.pipeline.urls import host_of

__all__ = [
    "Blob",
    "Gh",
    "GhResult",
    "RefPath",
    "Tree",
    "fetch_blob",
    "fetch_path",
    "fetch_readme",
    "gh_api",
    "gh_api_list",
    "ref_path",
    "run_gh",
]

_GH_HTTP_RE = re.compile(r"HTTP (\d{3})")
_GH_TIMEOUT = 120.0
_RAW_ACCEPT = "Accept: application/vnd.github.raw+json"
_UNEXPECTED_SHAPE = Classification(
    status=Status.BLOCKED, reason="gh api returned an unexpected shape"
)

# A path URL hides its ref/path boundary: `blob/automation/bors/auto/README.md`
# is branch `automation/bors/auto` holding `README.md`, and nothing in the
# string says where the ref stops. raw.githubusercontent.com resolved that
# boundary server-side; the contents API wants the two halves separately, so
# the seam has to settle it. The `refs/heads/`, `refs/tags/` permalink form
# names its own namespace, and one segment is right for nearly every other
# link — so guess that first (no extra request), and only when the guess 404s
# ask the repo which of its refs the path actually starts with.
_REF_NAMESPACES = ("heads", "tags")
_QUALIFIED_REF_SEGMENTS = 3  # refs/<namespace>/<name>

# The three views of one path at one ref: GitHub itself redirects each to
# whichever of blob and tree the path holds, and raw serves the same file.
_PATH_VIEWS = frozenset({"blob", "raw", "tree"})
_PATH_SEGMENTS = 4  # /owner/repo/<view>/<ref>/…


@dataclass(frozen=True, slots=True, kw_only=True)
class GhResult:
    """One ``gh`` invocation's outcome."""

    returncode: int
    stdout: str
    stderr: str


# The seam every caller injects so tests are hermetic; `run_gh` is the one
# real implementation.
Gh = Callable[[Sequence[str]], GhResult]


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


def gh_api(gh: Gh, endpoint: str) -> dict | Classification:
    """``gh api <endpoint>`` parsed as a JSON object, or the classified failure."""
    payload = _gh_json(gh, endpoint)
    if isinstance(payload, dict | Classification):
        return payload
    return _UNEXPECTED_SHAPE


def gh_api_list(gh: Gh, endpoint: str) -> list:
    """``gh api <endpoint>`` parsed as a JSON array; every failure yields [].

    Both callers treat an empty answer as "no extra information": a profile
    notes a missing repo listing in its body rather than failing the unit,
    and a path whose ref lookup came back empty keeps the classification its
    contents call already earned.
    """
    payload = _gh_json(gh, endpoint)
    return payload if isinstance(payload, list) else []


def fetch_readme(
    gh: Gh, owner: str, repo: str, *, ref: str | None = None, path: str = ""
) -> str | Classification | None:
    """A directory's README as text — the repo root's by default — or None for none.

    Asked only of a directory already found, so a 404 can mean nothing but
    "this directory has no README"; every other failure is the fetch
    failing, and classifies like any other.
    """
    endpoint = f"repos/{owner}/{repo}/readme"
    if path:
        endpoint += f"/{_requote(path, safe='/')}"
    if ref is not None:
        endpoint += f"?ref={_requote(ref, safe='')}"
    result = gh(["api", endpoint, "-H", _RAW_ACCEPT])
    if result.returncode == 0:
        return result.stdout
    failure = _classify_gh_failure(result.stderr)
    return None if failure.status is Status.DEAD else failure


@dataclass(frozen=True, slots=True, kw_only=True)
class RefPath:
    """A repo, and the ref/path tail a blob, raw or tree URL has not yet split.

    The tail is kept whole on purpose: which segments are the ref is not
    knowable from the URL, and :func:`fetch_path` may have to ask the repo.
    ``min_path`` is how many segments the ref must leave as the path: a
    blob or raw link names a file, so one; a tree link may name a ref's
    root, so none — GitHub renders ``tree/automation/bors/auto`` and 404s
    the same link as a blob.
    """

    owner: str
    repo: str
    tail: tuple[str, ...]
    min_path: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Blob:
    """One file's bytes, under the path the resolved ref left behind."""

    path: str
    data: bytes


@dataclass(frozen=True, slots=True, kw_only=True)
class Tree:
    """One directory at the ref and path the split resolved; ``path`` is empty at the root.

    ``entries`` are the listing's names in the API's order, a directory's
    marked with a trailing slash.
    """

    ref: str
    path: str
    entries: tuple[str, ...]


def ref_path(url: str) -> RefPath | None:
    """The path a github.com blob, raw or tree URL addresses, or None for every other shape.

    Both drivers ask: the github driver to read what the path holds, the
    file driver both to re-fetch a committed document and to know that
    HTML from this seam is a committed file rather than a viewer page to
    re-route.
    """
    if host_of(url) != "github.com":
        return None
    segments = [segment for segment in urllib.parse.urlsplit(url).path.split("/") if segment]
    if len(segments) < _PATH_SEGMENTS or segments[2] not in _PATH_VIEWS:
        return None
    return RefPath(
        owner=segments[0],
        repo=segments[1],
        tail=tuple(segments[3:]),
        min_path=0 if segments[2] == "tree" else 1,
    )


def fetch_path(gh: Gh, ref: RefPath) -> Blob | Tree | Classification:
    """What a path holds — a file's bytes or a directory's listing — through the contents API.

    The ref/path split is guessed from the URL's shape first — free, and
    right for nearly every link. A 404 on that guess is the one failure a
    different boundary could fix, so it costs one extra request to ask the
    repo which of its own refs the path really starts with; when nothing
    re-splits, or the lookup itself fails, the guess's own classification
    stands and a genuinely missing path is still ``dead``.
    """
    split = _guessed_split(ref.tail)
    payload = _contents(gh, ref, split)
    if isinstance(payload, Classification) and payload.status is Status.DEAD:
        resplit = _resolve_split(gh, ref)
        if resplit is not None and resplit != split:
            split = resplit
            payload = _contents(gh, ref, split)
    if isinstance(payload, Classification):
        return payload
    ref_name, path = split
    if isinstance(payload, list):
        return Tree(ref=ref_name, path=path, entries=_entry_names(payload))
    data = _blob_bytes(payload)
    if data is None:
        # Over 1MB the contents API serves `encoding: "none"` and an empty
        # body — the file exists, we just cannot read it here.
        return Classification(
            status=Status.MANUAL,
            reason=f"{path} is larger than the contents API serves inline — read it from a clone",
        )
    return Blob(path=path, data=data)


def fetch_blob(gh: Gh, ref: RefPath) -> Blob | Classification:
    """A file's bytes, for a reader with nothing to do with a directory."""
    found = fetch_path(gh, ref)
    if isinstance(found, Tree):
        where = found.path or "the repo root"
        return Classification(status=Status.MANUAL, reason=f"{where} is a directory, not a file")
    return found


def _resolve_split(gh: Gh, ref: RefPath) -> tuple[str, str] | None:
    """The ref/path split the repo's own refs support, or None for no match.

    ``git/matching-refs`` is asked only about refs starting with the tail's
    first segment, so a repo with thousands of branches costs the same one
    page as a repo with three. A SHA ref matches nothing and answers ``[]``,
    which leaves the guess (and its classification) alone.
    """
    tail = ref.tail
    namespaces, start = _ref_search(tail)
    for namespace in namespaces:
        refs = gh_api_list(
            gh,
            f"repos/{ref.owner}/{ref.repo}/git/matching-refs/"
            f"{namespace}/{_requote(tail[start], safe='')}",
        )
        length = _longest_ref_match(_ref_names(refs, namespace), tail, start, min_path=ref.min_path)
        if length is not None:
            boundary = start + length
            return "/".join(tail[:boundary]), "/".join(tail[boundary:])
    return None


def _contents(gh: Gh, ref: RefPath, split: tuple[str, str]) -> dict | list | Classification:
    """The contents API's answer for one path at one ref: a file's object, a directory's array."""
    ref_name, path = split
    payload = _gh_json(
        gh,
        f"repos/{ref.owner}/{ref.repo}/contents/{_requote(path, safe='/')}"
        f"?ref={_requote(ref_name, safe='')}",
    )
    if isinstance(payload, dict | list | Classification):
        return payload
    return _UNEXPECTED_SHAPE


def _entry_names(listing: list) -> tuple[str, ...]:
    """A directory listing's names, each directory's marked with a trailing slash."""
    return tuple(
        f"{entry['name']}/" if entry.get("type") == "dir" else entry["name"]
        for entry in listing
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    )


def _is_qualified_ref(tail: tuple[str, ...]) -> bool:
    """True for the ``refs/heads/<name>/…`` permalink form."""
    return len(tail) >= _QUALIFIED_REF_SEGMENTS and tail[0] == "refs" and tail[1] in _REF_NAMESPACES


def _guessed_split(tail: tuple[str, ...]) -> tuple[str, str]:
    """The ref/path split assuming the shortest ref the tail's shape allows."""
    if _is_qualified_ref(tail):
        return "/".join(tail[:_QUALIFIED_REF_SEGMENTS]), "/".join(tail[_QUALIFIED_REF_SEGMENTS:])
    return tail[0], "/".join(tail[1:])


def _ref_search(tail: tuple[str, ...]) -> tuple[tuple[str, ...], int]:
    """Which ref namespaces to search, and where in the tail the ref starts."""
    if _is_qualified_ref(tail):
        return (tail[1],), 2
    return _REF_NAMESPACES, 0


def _ref_names(refs: list, namespace: str) -> list[str]:
    """``refs/heads/x/y`` entries reduced to the bare ref names (``x/y``)."""
    prefix = f"refs/{namespace}/"
    names = [ref.get("ref") for ref in refs if isinstance(ref, dict)]
    return [
        name.removeprefix(prefix)
        for name in names
        if isinstance(name, str) and name.startswith(prefix)
    ]


def _longest_ref_match(
    names: list[str], tail: tuple[str, ...], start: int, *, min_path: int
) -> int | None:
    """Segment count of the longest name matching ``tail[start:]``, or None.

    Compared segment by segment, never as a string prefix: ``automation/bors``
    must not claim a URL whose branch is ``automation/bors-next``. A name that
    leaves fewer than ``min_path`` segments of path is rejected — for a blob
    link, a ref that consumes the whole tail leaves no file to read.
    """
    best: int | None = None
    for name in names:
        segments = [segment for segment in name.split("/") if segment]
        length = len(segments)
        if start + length > len(tail) - min_path:
            continue
        if tail[start : start + length] == tuple(segments) and (best is None or length > best):
            best = length
    return best


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


def _gh_json(gh: Gh, endpoint: str) -> object:
    """``gh api <endpoint>`` parsed as whatever JSON it answered, or the classified failure."""
    result = gh(["api", endpoint])
    if result.returncode != 0:
        return _classify_gh_failure(result.stderr)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return Classification(status=Status.BLOCKED, reason="gh api returned unparseable JSON")


def _classify_gh_failure(stderr: str) -> Classification:
    match = _GH_HTTP_RE.search(stderr)
    if match:
        return classify_http(int(match.group(1)))
    return Classification(status=Status.BLOCKED, reason=f"gh api failed: {scrub(stderr)}")
