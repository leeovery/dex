"""The drivers' authenticated GitHub seam: ``gh`` calls, and a blob's bytes.

Two drivers need the same authenticated route into a repo, and neither
owns it: the github driver reads a blob to fence it as source, and the file
driver re-reads that same blob when its bytes turn out to be a document to
extract. Everything that route involves lives here — the ``gh`` invocation,
the JSON parse and its failure classification, and the whole blob round
trip: where a ``/blob/`` URL's ref stops and its path starts, the
contents-API endpoint, the base64 decode, and the park for a file too large
to be served inline.

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
:func:`fetch_blob` has to settle the boundary itself — and why it answers
with the path the winning split left, since that name is what the bytes
have to be sniffed under.
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
    "BlobRef",
    "Gh",
    "GhResult",
    "blob_ref",
    "fetch_blob",
    "gh_api",
    "gh_api_list",
    "run_gh",
]

_GH_HTTP_RE = re.compile(r"HTTP (\d{3})")
_GH_TIMEOUT = 120.0

# A blob URL hides its ref/path boundary: `blob/automation/bors/auto/README.md`
# is branch `automation/bors/auto` holding `README.md`, and nothing in the
# string says where the ref stops. raw.githubusercontent.com resolved that
# boundary server-side; the contents API wants the two halves separately, so
# the seam has to settle it. The `refs/heads/`, `refs/tags/` permalink form
# names its own namespace, and one segment is right for nearly every other
# link — so guess that first (no extra request), and only when the guess 404s
# ask the repo which of its refs the path actually starts with.
_REF_NAMESPACES = ("heads", "tags")
_QUALIFIED_REF_SEGMENTS = 4  # refs/<namespace>/<name>/<path…>, at minimum

_BLOB_SEGMENTS = 4  # /owner/repo/blob/<ref>/…


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
    result = gh(["api", endpoint])
    if result.returncode != 0:
        return _classify_gh_failure(result.stderr)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return Classification(status=Status.BLOCKED, reason="gh api returned unparseable JSON")
    if not isinstance(payload, dict):
        return Classification(status=Status.BLOCKED, reason="gh api returned an unexpected shape")
    return payload


def gh_api_list(gh: Gh, endpoint: str) -> list:
    """``gh api <endpoint>`` parsed as a JSON array; every failure yields [].

    Both callers treat an empty answer as "no extra information": a profile
    notes a missing repo listing in its body rather than failing the unit,
    and a blob whose ref lookup came back empty keeps the classification its
    contents call already earned.
    """
    result = gh(["api", endpoint])
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


@dataclass(frozen=True, slots=True, kw_only=True)
class BlobRef:
    """A repo, and the ref/path tail a ``/blob/`` URL has not yet split.

    The tail is kept whole on purpose: which segments are the ref is not
    knowable from the URL, and :func:`fetch_blob` may have to ask the repo.
    """

    owner: str
    repo: str
    tail: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Blob:
    """One blob's bytes, under the path the resolved ref left behind."""

    path: str
    data: bytes


def blob_ref(url: str) -> BlobRef | None:
    """The blob a github.com URL addresses, or None for every other shape.

    Both drivers ask: the github driver to fetch one, the file driver both
    to re-fetch a document blob and to know that HTML from this seam is a
    committed file rather than a viewer page to re-route.
    """
    if host_of(url) != "github.com":
        return None
    segments = [segment for segment in urllib.parse.urlsplit(url).path.split("/") if segment]
    if len(segments) < _BLOB_SEGMENTS or segments[2] != "blob":
        return None
    return BlobRef(owner=segments[0], repo=segments[1], tail=tuple(segments[3:]))


def fetch_blob(gh: Gh, ref: BlobRef) -> Blob | Classification:
    """A blob's bytes and its real path, through the authenticated contents API.

    The ref/path split is guessed from the URL's shape first — free, and
    right for nearly every link. A 404 on that guess is the one failure a
    different boundary could fix, so it costs one extra request to ask the
    repo which of its own refs the path really starts with; when nothing
    re-splits, or the lookup itself fails, the guess's own classification
    stands and a genuinely missing path is still ``dead``.
    """
    split = _guessed_split(ref.tail)
    payload = gh_api(gh, _contents_endpoint(ref, split))
    if isinstance(payload, Classification) and payload.status is Status.DEAD:
        resplit = _resolve_split(gh, ref)
        if resplit is not None and resplit != split:
            split = resplit
            payload = gh_api(gh, _contents_endpoint(ref, split))
    if isinstance(payload, Classification):
        return payload
    _, file_path = split
    data = _blob_bytes(payload)
    if data is None:
        # Over 1MB the contents API serves `encoding: "none"` and an empty
        # body — the file exists, we just cannot read it here.
        return Classification(
            status=Status.MANUAL,
            reason=(
                f"{file_path} is larger than the contents API serves inline — read it from a clone"
            ),
        )
    return Blob(path=file_path, data=data)


def _resolve_split(gh: Gh, ref: BlobRef) -> tuple[str, str] | None:
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
        length = _longest_ref_match(_ref_names(refs, namespace), tail, start)
        if length is not None:
            boundary = start + length
            return "/".join(tail[:boundary]), "/".join(tail[boundary:])
    return None


def _contents_endpoint(ref: BlobRef, split: tuple[str, str]) -> str:
    """The contents-API endpoint for one blob at one ref."""
    ref_name, file_path = split
    return (
        f"repos/{ref.owner}/{ref.repo}/contents/{_requote(file_path, safe='/')}"
        f"?ref={_requote(ref_name, safe='')}"
    )


def _is_qualified_ref(tail: tuple[str, ...]) -> bool:
    """True for the ``blob/refs/heads/<name>/…`` permalink form."""
    return len(tail) >= _QUALIFIED_REF_SEGMENTS and tail[0] == "refs" and tail[1] in _REF_NAMESPACES


def _guessed_split(tail: tuple[str, ...]) -> tuple[str, str]:
    """The ref/path split assuming the shortest ref the tail's shape allows."""
    if _is_qualified_ref(tail):
        return "/".join(tail[:3]), "/".join(tail[3:])
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


def _longest_ref_match(names: list[str], tail: tuple[str, ...], start: int) -> int | None:
    """Segment count of the longest name matching ``tail[start:]``, or None.

    Compared segment by segment, never as a string prefix: ``automation/bors``
    must not claim a URL whose branch is ``automation/bors-next``. A name that
    consumes the whole tail is rejected — that would leave no file path.
    """
    best: int | None = None
    for name in names:
        segments = [segment for segment in name.split("/") if segment]
        length = len(segments)
        if start + length >= len(tail):
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


def _classify_gh_failure(stderr: str) -> Classification:
    match = _GH_HTTP_RE.search(stderr)
    if match:
        return classify_http(int(match.group(1)))
    return Classification(status=Status.BLOCKED, reason=f"gh api failed: {scrub(stderr)}")
