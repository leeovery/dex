"""Reconcile the capture inbox: materialize staged binary captures.

Every capture is one .md file in state/inbox/. Text captures carry a URL
and/or note in the body. A capture that arrived with a binary (image, PDF,
any file) additionally carries frontmatter naming a release asset staged on
the instance's standing "inbox" release:

    ---
    asset: https://api.github.com/repos/OWNER/REPO/releases/assets/123
    name: 20260818-101530.jpg
    ---
    why this caught my eye

Run from the instance root (bin/kb inbox), this command:

  1. downloads each staged asset to media/<item-id>/<name>
     (item-id = sha1("media/<name>")[:6] — the id the ingest skill uses),
  2. git-adds it so the LFS clean filter applies (and verifies it did),
  3. deletes the release asset,
  4. rewrites the capture frontmatter to `media: media/<item-id>/<name>`.

Text captures pass through untouched. Idempotent; run it at the top of
every ingest. `bin/kb inbox ensure` creates the standing release if
missing (one-time per instance; reconcile runs also self-heal it).

Auth: GITHUB_TOKEN env var, else `gh auth token`. Only required when
there are staged assets to move (or for `ensure`).
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path.cwd()
INBOX = ROOT / "state" / "inbox"
API = "https://api.github.com"
TAG = "inbox"
UA = "dex-engine-inbox"


def _token() -> str | None:
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except FileNotFoundError:
        pass
    return None


def _repo() -> str | None:
    r = subprocess.run(["git", "remote", "get-url", "origin"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    m = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?$", r.stdout.strip())
    return m.group(1) if m else None


def _api(url: str, token: str, method: str = "GET", data: dict | None = None):
    if not url.startswith("http"):
        url = API + url
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json", "User-Agent": UA}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download(asset_url: str, token: str) -> bytes:
    # GitHub 302-redirects asset downloads to a signed storage URL that
    # rejects requests carrying an Authorization header — follow without it.
    req = urllib.request.Request(asset_url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/octet-stream", "User-Agent": UA})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = urllib.request.Request(e.headers.get("Location"),
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(loc, timeout=600) as resp:
                return resp.read()
        raise


def _parse(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        return {}, text
    fm = {}
    for line in lines[1:end]:
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, "\n".join(lines[end + 1:]).lstrip("\n")


def _rewrite(path: Path, fm: dict, rel: str, body: str) -> None:
    keep = {k: v for k, v in fm.items() if k not in ("asset", "name")}
    lines = ["---", f"media: {rel}"] + [f"{k}: {v}" for k, v in keep.items()] + ["---"]
    text = "\n".join(lines) + "\n"
    if body.strip():
        text += "\n" + body.rstrip("\n") + "\n"
    path.write_text(text)


def _lfs_staged(rel: str) -> bool:
    attr = subprocess.run(["git", "check-attr", "filter", "--", rel],
                          capture_output=True, text=True).stdout
    if not attr.strip().endswith("lfs"):
        return False
    if subprocess.run(["git", "add", "--", rel]).returncode != 0:
        return False
    staged = subprocess.run(["git", "cat-file", "-p", f":{rel}"],
                            capture_output=True, text=True).stdout
    return staged.startswith("version https://git-lfs")


def _materialize(path: Path, fm: dict, body: str, token: str) -> bool:
    asset_url = fm["asset"]
    st, meta = _api(asset_url, token)
    name = fm.get("name") or (meta["name"] if meta else "")
    if not name:
        print(f"  FAIL {path.name}: pointer has no name and asset lookup returned {st}")
        return False
    item_id = hashlib.sha1(f"media/{name}".encode()).hexdigest()[:6]
    rel = f"media/{item_id}/{name}"
    dest = ROOT / rel

    if st == 404:
        if dest.exists():
            _rewrite(path, fm, rel, body)
            print(f"  ok {path.name}: asset already ingested; pointer updated")
            return True
        print(f"  FAIL {path.name}: asset is gone and {rel} doesn't exist locally "
              f"— did a prior ingest on another machine push this? git pull first.")
        return False
    if st != 200:
        print(f"  FAIL {path.name}: asset lookup returned {st}")
        return False

    if not dest.exists() or dest.stat().st_size != meta["size"]:
        data = _download(asset_url, token)
        if len(data) != meta["size"]:
            print(f"  FAIL {path.name}: downloaded {len(data)} bytes, expected {meta['size']}")
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    if not _lfs_staged(rel):
        print(f"  FAIL {path.name}: {rel} did not stage as an LFS pointer — check "
              f".gitattributes covers media/** and `git lfs install` has run. Asset NOT deleted.")
        return False

    st, _ = _api(asset_url, token, method="DELETE")
    if st not in (204, 404):
        print(f"  warn {path.name}: asset delete returned {st} — remove it from the "
              f"inbox release by hand")
    _rewrite(path, fm, rel, body)
    print(f"  ok {path.name} -> {rel}")
    return True


def _ensure_release(repo: str, token: str, quiet_if_present: bool = False):
    st, rel = _api(f"/repos/{repo}/releases/tags/{TAG}", token)
    if st == 200:
        if not quiet_if_present:
            print(f"inbox release present on {repo}")
        return rel
    if st != 404:
        print(f"FAIL: release lookup on {repo} returned {st}")
        return None
    st, rel = _api(f"/repos/{repo}/releases", token, method="POST", data={
        "tag_name": TAG, "name": "Capture inbox", "prerelease": True,
        "body": "Staging area for Send To Dex binary captures. Assets are "
                "moved into media/ (LFS) at ingest and deleted."})
    if st == 201:
        print(f"created inbox release on {repo}")
        return rel
    print(f"FAIL: creating inbox release on {repo} returned {st}")
    return None


def _report_orphans(repo: str, token: str) -> None:
    st, rel = _api(f"/repos/{repo}/releases/tags/{TAG}", token)
    if st != 200 or not rel.get("assets"):
        return
    referenced = set()
    for p in INBOX.glob("*.md"):
        m = re.search(r"/assets/(\d+)$", _parse(p.read_text())[0].get("asset", ""))
        if m:
            referenced.add(int(m.group(1)))
    stray = [a for a in rel["assets"] if a["id"] not in referenced]
    if stray:
        print(f"note: {len(stray)} asset(s) on the inbox release have no capture "
              f"pointer (a capture may have failed after upload, or is in flight):")
        for a in stray:
            print(f"  {a['name']} ({a['size']} bytes) — {a['url']}")


def main() -> None:
    if sys.argv[1:2] == ["ensure"]:
        token, repo = _token(), _repo()
        if not token or not repo:
            sys.exit("need GitHub auth (gh auth login, or GITHUB_TOKEN) and an "
                     "origin remote on github.com")
        sys.exit(0 if _ensure_release(repo, token) else 1)

    if not INBOX.is_dir():
        print("no state/inbox/ here — run from an instance root.")
        return

    captures = sorted(INBOX.glob("*.md"))
    staged = [(p, fm, body) for p in captures
              for fm, body in [_parse(p.read_text())] if "asset" in fm]
    token, repo = _token(), _repo()

    if staged and (not token or not repo):
        sys.exit("staged assets need GitHub auth: install gh and `gh auth login`, "
                 "or set GITHUB_TOKEN")

    failures = 0
    for p, fm, body in staged:
        if not _materialize(p, fm, body, token):
            failures += 1

    if staged:
        print(f"inbox: {len(staged) - failures}/{len(staged)} staged asset(s) "
              f"materialized; {len(captures) - len(staged)} text capture(s) untouched.")
    else:
        print(f"inbox: {len(captures)} capture(s), none with staged assets.")

    if token and repo:
        _ensure_release(repo, token, quiet_if_present=True)
        _report_orphans(repo, token)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
