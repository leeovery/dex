"""dex-inbox: reconcile the capture inbox — materialize staged binary captures.

Every capture is one ``.md`` file in ``inbox/``. Text captures carry a URL
and/or note in the body. A capture that arrived with a binary (image, PDF,
any file) additionally carries frontmatter naming a release asset staged on
the instance's standing ``inbox`` release::

    ---
    asset: https://api.github.com/repos/OWNER/REPO/releases/assets/123
    name: 20260818-101530.jpg
    ---
    why this caught my eye

Run from the instance root (``bin/dex inbox``), reconcile:

  1. downloads each staged asset to ``media/<item-id>/<name>``
     (item-id = sha1("media/<name>")[:6] — the id ``enrich item new`` uses),
  2. git-adds it so the LFS clean filter applies (and verifies it did),
  3. deletes the release asset,
  4. rewrites the capture frontmatter to ``media: media/<item-id>/<name>``.

The materialization sequence is device-tested — every step's order is
load-bearing: the LFS check happens BEFORE the asset delete (a file that
failed to stage as an LFS pointer must not lose its only remote copy), and
the pointer rewrite happens after, so an interrupted run re-enters cleanly.
Once anything materialized, the repo copy is the ONLY copy — the output says
to commit and push immediately.

Text captures pass through untouched. Idempotent; run it at the top of every
session. ``bin/dex inbox ensure`` creates the standing release if missing
(one-time per instance; reconcile runs also self-heal it).

Auth: GITHUB_TOKEN env var, else ``gh auth token``. Only required when there
are staged assets to move (or for ``ensure``).
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from email.message import Message as HTTPMessage

from . import atomic
from .drivers.transport import normalize_httplib_errors
from .pipeline.capture import parse_capture
from .pipeline.detect import sniff_format
from .pipeline.types import Instance

__all__ = [
    "GithubSeams",
    "build_parser",
    "default_seams",
    "ensure",
    "main",
    "parse_capture",
    "reconcile",
]

API = "https://api.github.com"
TAG = "inbox"
UA = "dex-engine-inbox"

_ASSET_URL_RE = re.compile(r"https://api\.github\.com/repos/[^/]+/[^/]+/releases/assets/\d+")
_REMOTE_RE = re.compile(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?$")


class ApiCall(Protocol):
    """One GitHub API request; HTTP errors return their status, never raise."""

    def __call__(
        self, url: str, token: str, method: str = "GET", data: dict[str, str | bool] | None = None
    ) -> tuple[int, dict[str, object] | None]:
        """Request ``url``; relative paths resolve against the API root."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class GithubSeams:
    """The seams between inbox and the world: all injected, all fakeable.

    ``api`` speaks the GitHub REST API; ``download`` fetches one asset's
    bytes; ``git`` runs a git subcommand returning ``(returncode, stdout)``;
    ``token`` resolves auth (env, then gh); ``echo`` is where progress goes.
    """

    api: ApiCall
    download: Callable[[str, str], bytes]
    git: Callable[[list[str]], tuple[int, str]]
    token: Callable[[], str | None]
    echo: Callable[[str], None] = print


# ---------------------------------------------------------------------------
# Production seams
# ---------------------------------------------------------------------------


def _resolve_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        completed = subprocess.run(
            ["gh", "auth", "token"],  # noqa: S607 — gh resolves via PATH like every dev tool
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    return None


def _run_git(args: list[str]) -> tuple[int, str]:
    completed = subprocess.run(  # noqa: S603 — engine-built args, no shell
        ["git", *args],  # noqa: S607 — git resolves via PATH like every dev tool
        capture_output=True,
        check=False,
    )
    # Decoded tolerantly, never text=True: `cat-file -p` on a raw binary
    # staged blob — the very case the LFS pointer check exists to catch —
    # must yield a comparable string, not a UnicodeDecodeError.
    return completed.returncode, completed.stdout.decode("utf-8", "replace")


def _api_request(
    url: str, token: str, method: str = "GET", data: dict[str, str | bool] | None = None
) -> tuple[int, dict[str, object] | None]:
    if not url.startswith("http"):
        url = API + url
    body = json.dumps(data).encode() if data is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": UA,
    }
    if body:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310 — https-only API root
    # The same seam guard the drivers' transport uses: `http.client`'s
    # protocol failures are not OSErrors, and main()'s wrapper catches
    # OSError — an unguarded IncompleteRead reaches the operator as a
    # traceback instead of a `dex-inbox: ...` line.
    with normalize_httplib_errors():
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A handler that refuses redirects — the caller re-follows without auth."""

    def redirect_request(  # noqa: PLR0913, PLR0917 — urllib's fixed override signature
        self,
        req: urllib.request.Request,  # noqa: ARG002 — the refusal ignores the redirect
        fp: "IO[bytes]",  # noqa: ARG002
        code: int,  # noqa: ARG002
        msg: str,  # noqa: ARG002
        headers: "HTTPMessage",  # noqa: ARG002
        newurl: str,  # noqa: ARG002
    ) -> urllib.request.Request | None:
        """Refuse the redirect so it surfaces as the HTTPError handled above."""
        return None


def _download_asset(asset_url: str, token: str) -> bytes:
    # GitHub 302-redirects asset downloads to a signed storage URL that
    # rejects requests carrying an Authorization header — follow without it.
    request = urllib.request.Request(  # noqa: S310 — asset URLs are validated https API URLs
        asset_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/octet-stream",
            "User-Agent": UA,
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    # An asset read is the large-download shape: the server hanging up
    # mid-body raises IncompleteRead, which is not an OSError and so would
    # escape main()'s wrapper as a traceback rather than a stated failure.
    with normalize_httplib_errors():
        try:
            with opener.open(request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            target = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and target:
                location = urllib.request.Request(  # noqa: S310 — the API's signed redirect target
                    target, headers={"User-Agent": UA}
                )
                with urllib.request.urlopen(location, timeout=600) as response:  # noqa: S310
                    return response.read()
            raise


def default_seams() -> GithubSeams:
    """The production seams: real API, real git, env/gh token, print."""
    return GithubSeams(
        api=_api_request,
        download=_download_asset,
        git=_run_git,
        token=_resolve_token,
    )


# ---------------------------------------------------------------------------
# Capture files
# ---------------------------------------------------------------------------


def _rewrite_pointer(path: Path, frontmatter: dict[str, str], rel: str, body: str) -> None:
    keep = {k: v for k, v in frontmatter.items() if k not in ("asset", "name")}
    lines = ["---", f"media: {rel}", *(f"{k}: {v}" for k, v in keep.items()), "---"]
    text = "\n".join(lines) + "\n"
    if body.strip():
        text += "\n" + body.rstrip("\n") + "\n"
    # Atomic: the release asset is already deleted by the time this runs — a
    # crash mid-write must never leave a truncated capture as the only copy.
    atomic.write_text(path, text)


# ---------------------------------------------------------------------------
# Materialization — the device-tested sequence; order is load-bearing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class _Reconcile:
    """One reconcile run over the instance's inbox."""

    instance: Instance
    seams: GithubSeams

    def materialize(self, path: Path, frontmatter: dict[str, str], body: str, token: str) -> bool:
        echo = self.seams.echo
        asset_url = frontmatter["asset"]
        if not _ASSET_URL_RE.fullmatch(asset_url):
            echo(
                f"  FAIL {path.name}: malformed asset URL {asset_url!r} — the capture "
                f"client's upload step likely failed; the binary may be lost"
            )
            return False
        status, meta = self.seams.api(asset_url, token)
        name = frontmatter.get("name") or (str(meta["name"]) if meta else "")
        if not name:
            echo(f"  FAIL {path.name}: pointer has no name and asset lookup returned {status}")
            return False
        item_id = hashlib.sha1(f"media/{name}".encode()).hexdigest()[:6]  # noqa: S324 — content key, not a security context
        rel = f"media/{item_id}/{name}"
        dest = self.instance.root / rel

        if status == 404:  # noqa: PLR2004 — HTTP status codes are self-naming
            return self._already_gone(path, frontmatter, rel, dest, body)
        if status != 200 or meta is None:  # noqa: PLR2004 — HTTP status codes are self-naming
            echo(f"  FAIL {path.name}: asset lookup returned {status}")
            return False
        if not self._fetched(path, asset_url, token, dest, int(str(meta["size"]))):
            return False
        return self._promote(path, frontmatter, rel, dest, body, asset_url, token)

    def _already_gone(
        self, path: Path, frontmatter: dict[str, str], rel: str, dest: Path, body: str
    ) -> bool:
        """A 404'd asset: fine if the file already landed, loud otherwise."""
        if dest.exists():
            _rewrite_pointer(path, frontmatter, rel, body)
            self.seams.echo(f"  ok {path.name}: asset already ingested; pointer updated")
            return True
        self.seams.echo(
            f"  FAIL {path.name}: asset is gone and {rel} doesn't exist locally "
            f"— did a prior ingest on another machine push this? git pull first."
        )
        return False

    def _fetched(self, path: Path, asset_url: str, token: str, dest: Path, size: int) -> bool:
        """Ensure the asset's bytes are on disk, size-verified."""
        if dest.exists() and dest.stat().st_size == size:
            return True
        data = self.seams.download(asset_url, token)
        if len(data) != size:
            self.seams.echo(f"  FAIL {path.name}: downloaded {len(data)} bytes, expected {size}")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_bytes(dest, data)
        return True

    def _promote(  # noqa: PLR0913, PLR0917 — the tail of the sequence needs the whole capture context
        self,
        path: Path,
        frontmatter: dict[str, str],
        rel: str,
        dest: Path,
        body: str,
        asset_url: str,
        token: str,
    ) -> bool:
        """LFS-verify, delete the asset, rewrite the pointer — in that order.

        LFS verification comes BEFORE the asset delete: a binary that did not
        stage as an LFS pointer must keep its remote copy.
        """
        echo = self.seams.echo
        if not self._lfs_staged(rel):
            echo(
                f"  FAIL {path.name}: {rel} did not stage as an LFS pointer — check "
                f".gitattributes covers media/** and `git lfs install` has run. "
                f"Asset NOT deleted."
            )
            return False
        status, _ = self.seams.api(asset_url, token, method="DELETE")
        if status not in (204, 404):
            echo(
                f"  warn {path.name}: asset delete returned {status} — remove it from "
                f"the inbox release by hand"
            )
        _rewrite_pointer(path, frontmatter, rel, body)
        echo(f"  ok {path.name} -> {rel}")
        _note_document_format(dest, echo=echo)
        return True

    def _lfs_staged(self, rel: str) -> bool:
        code, attr = self.seams.git(["check-attr", "filter", "--", rel])
        if code != 0 or not attr.strip().endswith("lfs"):
            return False
        code, _ = self.seams.git(["add", "--", rel])
        if code != 0:
            return False
        _, staged = self.seams.git(["cat-file", "-p", f":{rel}"])
        return staged.startswith("version https://git-lfs")

    def repo(self) -> str | None:
        code, out = self.seams.git(["remote", "get-url", "origin"])
        if code != 0:
            return None
        match = _REMOTE_RE.search(out.strip())
        return match.group(1) if match else None


def _note_document_format(dest: Path, *, echo: Callable[[str], None]) -> None:
    """Announce a document-format capture's routing at inbox time.

    Materialized files feed the pipeline: a document capture becomes a
    ``file:`` work unit when its corpus item is created — ``enrich run``
    seeds it from the item's ``media:`` list (format detect → extract
    queue). The item id does not exist at materialization time, so the
    ledger write happens there; this hook is the detection half, so the
    operator sees the routing decision now. Never fatal — but the net is
    the ONE expected failure (an unreadable file), not a broad except: a
    ``sniff_format`` crash here is an engine bug that must stay loud.
    """
    try:
        data = dest.read_bytes()
    except OSError:
        return
    fmt = sniff_format(data, name=dest.name)
    if fmt is not None:
        echo(
            f"  note {dest.name}: {fmt.value} document — queues for extraction "
            f"at the next enrich run (via the item's media list)"
        )


# ---------------------------------------------------------------------------
# The standing inbox release
# ---------------------------------------------------------------------------


def _ensure_release(
    reconciler: _Reconcile, repo: str, token: str, *, quiet_if_present: bool = False
) -> dict[str, object] | None:
    echo = reconciler.seams.echo
    status, release = reconciler.seams.api(f"/repos/{repo}/releases/tags/{TAG}", token)
    if status == 200:  # noqa: PLR2004 — HTTP status codes are self-naming
        if not quiet_if_present:
            echo(f"inbox release present on {repo}")
        return release
    if status != 404:  # noqa: PLR2004 — HTTP status codes are self-naming
        echo(f"FAIL: release lookup on {repo} returned {status}")
        return None
    status, release = reconciler.seams.api(
        f"/repos/{repo}/releases",
        token,
        method="POST",
        data={
            "tag_name": TAG,
            "name": "Capture inbox",
            "prerelease": True,
            "body": "Staging area for Send To Dex binary captures. Assets are "
            "moved into media/ (LFS) at ingest and deleted.",
        },
    )
    if status == 201:  # noqa: PLR2004 — HTTP status codes are self-naming
        echo(f"created inbox release on {repo}")
        return release
    echo(f"FAIL: creating inbox release on {repo} returned {status}")
    return None


def _report_orphans(reconciler: _Reconcile, repo: str, token: str) -> None:
    """Assets on the release with no capture pointer: a failed or in-flight capture."""
    echo = reconciler.seams.echo
    status, release = reconciler.seams.api(f"/repos/{repo}/releases/tags/{TAG}", token)
    if status != 200 or release is None:  # noqa: PLR2004 — HTTP status codes are self-naming
        return
    assets = release.get("assets")
    if not isinstance(assets, list) or not assets:
        return
    referenced: set[int] = set()
    inbox_dir = reconciler.instance.root / "inbox"
    for path in inbox_dir.glob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # already reported as FAIL by the reconcile loop
        frontmatter, _ = parse_capture(text)
        match = re.search(r"/assets/(\d+)$", frontmatter.get("asset", ""))
        if match:
            referenced.add(int(match.group(1)))
    stray = [a for a in assets if isinstance(a, dict) and a.get("id") not in referenced]
    if stray:
        echo(
            f"note: {len(stray)} asset(s) on the inbox release have no capture "
            f"pointer (a capture may have failed after upload, or is in flight):"
        )
        for asset in stray:
            echo(f"  {asset.get('name')} ({asset.get('size')} bytes) — {asset.get('url')}")


# ---------------------------------------------------------------------------
# The verbs
# ---------------------------------------------------------------------------


def ensure(instance: Instance, seams: GithubSeams) -> int:
    """Create the standing inbox release if missing (one-time per instance).

    Args:
        instance: The instance.
        seams: The GitHub/git seams.

    Returns:
        The process exit code.
    """
    reconciler = _Reconcile(instance=instance, seams=seams)
    token = seams.token()
    repo = reconciler.repo()
    if not token or not repo:
        seams.echo(
            "need GitHub auth (gh auth login, or GITHUB_TOKEN) and an origin remote on github.com"
        )
        return 1
    return 0 if _ensure_release(reconciler, repo, token) else 1


def reconcile(instance: Instance, seams: GithubSeams) -> int:
    """Reconcile the inbox: materialize every staged capture (the sequence above).

    Args:
        instance: The instance.
        seams: The GitHub/git seams.

    Returns:
        The process exit code (1 when anything failed).
    """
    echo = seams.echo
    inbox_dir = instance.root / "inbox"
    if not inbox_dir.is_dir():
        echo("no inbox/ here — run from an instance root.")
        return 0
    reconciler = _Reconcile(instance=instance, seams=seams)
    captures = sorted(inbox_dir.glob("*.md"))
    staged: list[tuple[Path, dict[str, str], str]] = []
    unreadable = 0
    for path in captures:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # A capture that cannot even be read (raw bytes saved as .md,
            # a permissions accident) fails alone — never the reconcile.
            echo(f"  FAIL {path.name}: unreadable capture ({e})")
            unreadable += 1
            continue
        frontmatter, body = parse_capture(text)
        if "asset" in frontmatter:
            staged.append((path, frontmatter, body))
    token = seams.token()
    repo = reconciler.repo()

    if staged and (not token or not repo):
        echo("staged assets need GitHub auth: install gh and `gh auth login`, or set GITHUB_TOKEN")
        return 1

    materialized, failures = _materialize_all(reconciler, staged, token or "")
    failures += unreadable
    if staged:
        echo(
            f"inbox: {materialized}/{len(staged)} staged asset(s) materialized; "
            f"{len(captures) - len(staged) - unreadable} text capture(s) untouched."
        )
    else:
        echo(f"inbox: {len(captures)} capture(s), none with staged assets.")
    if materialized:
        # Order-critical, stated with its reason: the release assets
        # are already deleted, so until this lands on the remote the repo
        # copy is the ONLY copy of those binaries.
        echo(
            "commit and push the materialized media now — the release assets are "
            "deleted, so the repo copy is the only copy until it is pushed."
        )
    failures += _release_checks(reconciler, repo, token)
    return 1 if failures else 0


def _materialize_all(
    reconciler: _Reconcile,
    staged: list[tuple[Path, dict[str, str], str]],
    token: str,
) -> tuple[int, int]:
    """Materialize every staged capture; one bad capture never aborts the loop."""
    materialized = failures = 0
    for path, frontmatter, body in staged:
        try:
            ok = reconciler.materialize(path, frontmatter, body, token)
        except (OSError, ValueError, KeyError) as e:
            reconciler.seams.echo(f"  FAIL {path.name}: {e}")
            ok = False
        if ok:
            materialized += 1
        else:
            failures += 1
    return materialized, failures


def _release_checks(reconciler: _Reconcile, repo: str | None, token: str | None) -> int:
    """Self-heal the standing release and report orphaned assets, when possible."""
    if token and repo:
        if _ensure_release(reconciler, repo, token, quiet_if_present=True) is None:
            return 1
        _report_orphans(reconciler, repo, token)
        return 0
    # Two distinct causes, named apart — a misdiagnosis here sends the owner
    # to `gh auth login` when the fix is `git remote add origin`.
    if not token:
        why = "no GitHub auth here"
    else:
        why = "no origin remote on github.com — the release checks need one"
    reconciler.seams.echo(
        f"note: {why} — the inbox-release and orphaned-asset checks were skipped, not passed."
    )
    return 0


# ---------------------------------------------------------------------------
# CLI — parse, build, call; zero business logic
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-inbox [ensure]."""
    parser = argparse.ArgumentParser(
        prog="dex-inbox",
        description="Reconcile the capture inbox at cwd: materialize staged binary "
        "captures into media/ (LFS), delete the release assets. "
        "`ensure` creates the standing inbox release.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["ensure"],
        default=None,
        help="ensure: create the standing inbox release if missing",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse, build the Instance and seams, reconcile (or ensure)."""
    args = build_parser().parse_args(argv)
    instance = Instance(root=Path.cwd())
    seams = default_seams()
    try:
        code = ensure(instance, seams) if args.command == "ensure" else reconcile(instance, seams)
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(f"dex-inbox: {e}")
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
