"""Enrich corpus items: fetch the full content behind every external URL.

Mechanical capture only — no LLM. Resumable via state/enrichment-ledger.jsonl.

  bin/enrich.py inventory          what would be fetched
  bin/enrich.py run [--limit N] [--kinds youtube,blog,...]
  bin/enrich.py whisper            transcribe ledgered no-caption videos (needs OPENAI_API_KEY)
  bin/enrich.py status             ledger summary
"""

import json
import hashlib
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path.cwd()  # engine runs from the brain repo root
CORPUS = ROOT / "corpus"
ENRICH = ROOT / "enrichment"
LEDGER = ROOT / "state" / "enrichment-ledger.jsonl"

def _instance_cfg() -> dict:
    p = ROOT / "state" / "normalize-config.json"
    try:
        import json as _j
        return _j.loads(p.read_text())
    except Exception:
        return {}

MEDIA_FETCH = _instance_cfg().get("media_fetch", "lead")  # none | lead

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) aikb-enricher"}
SLEEP = {"youtube": 2.0, "blog": 1.0, "github": 0.3, "paper": 3.0, "tweet": 4.0}

# ---------- URL handling ----------

STRIP_PARAMS = {"si", "utm_source", "utm_medium", "utm_campaign", "utm_term",
                "utm_content", "feature", "ref", "ref_src", "lang", "app"}


def canonical(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    path, query = p.path, p.query
    if host == "youtu.be":
        host, query, path = "youtube.com", f"v={p.path.lstrip('/')}", "/watch"
    q = [(k, v) for k, v in urllib.parse.parse_qsl(query) if k not in STRIP_PARAMS]
    if host == "youtube.com":
        q = [(k, v) for k, v in q if k == "v"]
    return urllib.parse.urlunsplit(
        ("https", host, path.rstrip("/"), urllib.parse.urlencode(q), "")
    )


def kind_of(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")
    if host in ("youtube.com", "youtu.be"):
        return "youtube"
    if host in ("github.com", "gist.github.com"):
        return "github"
    if host in ("arxiv.org", "openreview.net") or "huggingface.co/papers" in url:
        return "paper"
    if host in ("x.com", "twitter.com"):
        return "tweet"
    return "blog"


def uhash(url: str) -> str:
    return hashlib.sha1(canonical(url).encode()).hexdigest()[:10]


# ---------- ledger ----------

def load_ledger() -> dict:
    entries = {}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                entries[e["hash"]] = e
    return entries


def ledger_append(entry: dict) -> None:
    LEDGER.parent.mkdir(exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------- corpus ----------

def corpus_items() -> list[dict]:
    items = []
    for f in sorted(CORPUS.glob("*/*.md")):
        text = f.read_text()
        m = re.search(r"^id: (.+)$", text, re.M)
        urls = re.findall(r"^  - (https?://\S+)$", text, re.M)
        items.append({"path": f, "id": m.group(1), "urls": urls})
    return items


def update_item_frontmatter(item: dict) -> None:
    d = ENRICH / item["id"]
    files = sorted(p.name for p in d.glob("*.md")) if d.exists() else []
    text = item["path"].read_text()
    status = "enriched" if files else "raw"
    text = re.sub(r"^status: .*$", f"status: {status}", text, count=1, flags=re.M)
    listing = "[" + ", ".join(files) + "]"
    text = re.sub(r"^enrichment: .*$", f"enrichment: {listing}", text, count=1, flags=re.M)
    item["path"].write_text(text)


def write_enrichment(item_id: str, name: str, url: str, meta: dict, body: str) -> Path:
    out = ENRICH / item_id / name
    out.parent.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"url: {url}", f"fetched: {date.today()}"]
    fm += [f"{k}: {v}" for k, v in meta.items() if v not in (None, "") and not k.startswith("_")]
    fm += ["---", ""]
    out.write_text("\n".join(fm) + body.strip() + "\n")
    return out


# ---------- fetchers ----------

def clean_vtt(vtt: str) -> str:
    lines, prev = [], None
    for raw in vtt.splitlines():
        line = re.sub(r"<[^>]+>", "", raw).strip()
        if (not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE"))
                or re.fullmatch(r"\d+", line) or line.startswith("align:")):
            continue
        if line != prev:
            lines.append(line)
            prev = line
    return "\n".join(lines)


def fetch_youtube(url: str) -> tuple[str, dict, str]:
    """-> (status, meta, body)"""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(canonical(url), download=False)
    except yt_dlp.utils.DownloadError as e:
        return ("dead", {"error": str(e)[:120]}, "")
    meta = {
        "title": (info.get("title") or "").replace(":", " -"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration_min": round((info.get("duration") or 0) / 60),
        "upload_date": info.get("upload_date"),
    }
    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    track = None
    for src in (subs, autos):
        for lang in sorted(src):
            if lang.startswith("en"):
                track = next((t for t in src[lang] if t.get("ext") == "vtt"), None)
                if track:
                    break
        if track:
            break
    if not track:
        return ("nocaptions", meta, "")
    try:
        req = urllib.request.Request(track["url"], headers=UA)
        vtt = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as e:
        return ("error", {**meta, "error": str(e)[:120]}, "")
    body = clean_vtt(vtt)
    if len(body) < 200:
        return ("nocaptions", meta, "")
    return ("done", meta, body)


def _trafilatura_extract(html: str) -> str | None:
    import trafilatura

    return trafilatura.extract(html, output_format="markdown", include_links=False,
                               include_comments=False)


def fetch_blog(url: str) -> tuple[str, dict, str]:
    import trafilatura

    html = trafilatura.fetch_url(url)
    if html:
        body = _trafilatura_extract(html)
        if body and len(body) > 300:
            meta = {}
            og = re.search(r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)', html) or \
                 re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']', html)
            if og:
                meta["_media"] = [og.group(1)]
            return ("done", meta, body)
    # wayback fallback
    try:
        q = urllib.parse.quote(url, safe="")
        req = urllib.request.Request(
            f"https://archive.org/wayback/available?url={q}", headers=UA)
        snap = json.load(urllib.request.urlopen(req, timeout=20))
        closest = snap.get("archived_snapshots", {}).get("closest", {})
        if closest.get("available"):
            html = trafilatura.fetch_url(closest["url"])
            if html:
                body = _trafilatura_extract(html)
                if body and len(body) > 300:
                    return ("done", {"via": "wayback", "snapshot": closest["url"]}, body)
    except Exception:
        pass
    return ("dead", {}, "")


def gh(args: list[str]) -> str | None:
    r = subprocess.run(["gh", "api"] + args, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def fetch_github(url: str) -> tuple[str, dict, str]:
    p = urllib.parse.urlsplit(url)
    parts = [x for x in p.path.split("/") if x]
    if p.netloc.startswith("gist."):
        if len(parts) < 2:
            return ("skipped", {"reason": "gist index"}, "")
        raw = gh([f"gists/{parts[1]}"])
        if not raw:
            return ("dead", {}, "")
        g = json.loads(raw)
        body = "\n\n".join(
            f"### {n}\n```\n{f.get('content','')[:20000]}\n```"
            for n, f in g.get("files", {}).items())
        return ("done", {"title": (g.get("description") or "gist")}, body)
    if len(parts) == 1:
        raw = gh([f"users/{parts[0]}"])
        if not raw:
            return ("dead", {}, "")
        u = json.loads(raw)
        repos_raw = gh([f"users/{parts[0]}/repos?sort=pushed&per_page=100"])
        repos = json.loads(repos_raw) if repos_raw else []
        top = sorted(repos, key=lambda r: -r.get("stargazers_count", 0))[:15]
        listing = "\n".join(
            f"- **{r['name']}** ({r['stargazers_count']}★): {r.get('description') or ''}"
            for r in top)
        meta = {"title": u.get("name") or parts[0],
                "followers": u.get("followers")}
        return ("done", meta,
                f"## Profile\n\n{u.get('bio') or '(no bio)'}\n\n## Top repos\n\n{listing}")
    if len(parts) < 2:
        return ("skipped", {"reason": "not a repo url"}, "")
    owner, repo = parts[0], parts[1]
    if len(parts) >= 4 and parts[2] == "blob":
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{'/'.join(parts[3:])}"
        try:
            req = urllib.request.Request(raw_url, headers=UA)
            body = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
            return ("done", {"file": "/".join(parts[3:])}, f"```\n{body[:40000]}\n```")
        except Exception:
            return ("dead", {}, "")
    if len(parts) >= 4 and parts[2] in ("issues", "pull"):
        api = f"repos/{owner}/{repo}/issues/{parts[3]}"
        raw = gh([api])
        if not raw:
            return ("dead", {}, "")
        issue = json.loads(raw)
        return ("done", {"title": issue.get("title")},
                issue.get("body") or "(no body)")
    raw = gh([f"repos/{owner}/{repo}"])
    if not raw:
        return ("dead", {}, "")
    r = json.loads(raw)
    meta = {
        "title": r.get("full_name"),
        "description": (r.get("description") or "").replace(":", " -"),
        "stars": r.get("stargazers_count"),
        "archived": r.get("archived"),
        "topics": ", ".join(r.get("topics", [])[:8]),
    }
    readme = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/readme",
         "-H", "Accept: application/vnd.github.raw+json"],
        capture_output=True, text=True)
    body = readme.stdout if readme.returncode == 0 else "(no README)"
    return ("done", meta, body[:60000])


ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([\d.]+?)(?:v\d+)?(?:\.pdf)?/?$")


def fetch_paper(url: str) -> tuple[str, dict, str]:
    m = ARXIV_ID.search(canonical(url))
    if not m:
        return fetch_blog(url)  # openreview / hf papers read fine as articles
    aid = m.group(1)
    try:
        req = urllib.request.Request(
            f"https://export.arxiv.org/api/query?id_list={aid}", headers=UA)
        feed = urllib.request.urlopen(req, timeout=30).read().decode()
    except Exception as e:
        return ("error", {"error": str(e)[:120]}, "")
    title = re.findall(r"<title>(.*?)</title>", feed, re.S)
    abstract = re.findall(r"<summary>(.*?)</summary>", feed, re.S)
    published = re.findall(r"<published>(\d{4}-\d{2}-\d{2})", feed)
    meta = {
        "title": re.sub(r"\s+", " ", title[1]).strip().replace(":", " -") if len(title) > 1 else None,
        "published": published[0] if published else None,
        "arxiv_id": aid,
    }
    body = "## Abstract\n\n" + re.sub(r"\s+", " ", abstract[0]).strip() if abstract else ""
    import trafilatura
    html = trafilatura.fetch_url(f"https://arxiv.org/html/{aid}") or trafilatura.fetch_url(
        f"https://ar5iv.labs.arxiv.org/html/{aid}")
    if html:
        full = _trafilatura_extract(html)
        if full and len(full) > 2000:
            body += "\n\n## Full text\n\n" + full
            return ("done", meta, body)
    return ("done", {**meta, "note": "abstract only"}, body) if body else ("error", meta, "")


def fetch_tweet(url: str) -> tuple[str, dict, str]:
    path = urllib.parse.urlsplit(url).path.lstrip("/")
    try:
        req = urllib.request.Request(f"https://api.fxtwitter.com/{path}", headers=UA)
        t = json.load(urllib.request.urlopen(req, timeout=30))["tweet"]
    except Exception as e:
        return ("manual", {"error": str(e)[:120]}, "")
    meta = {"author": f"{t['author']['name']} (@{t['author']['screen_name']})",
            "tweeted": t.get("created_at"), "via": "fxtwitter"}
    body = t.get("text", "") or (t.get("raw_text") or {}).get("text", "")
    if t.get("quote"):
        q = t["quote"]
        body += f"\n\n> Quoting @{q['author']['screen_name']}: {q.get('text', '')}"
    photos = ((t.get("media") or {}).get("photos") or [])
    if photos:
        meta["_media"] = [p_.get("url") for p_ in photos if p_.get("url")]
    return ("done", meta, body) if body else ("manual", meta, "")


FETCHERS = {"youtube": fetch_youtube, "blog": fetch_blog,
            "github": fetch_github, "paper": fetch_paper, "tweet": fetch_tweet}


# ---------- work loop ----------

def build_worklist(kinds: set[str]) -> list[dict]:
    ledger = load_ledger()
    seen, work = set(), []
    for item in corpus_items():
        for url in item["urls"]:
            h = uhash(url)
            k = kind_of(url)
            if k not in kinds or h in seen:
                continue
            seen.add(h)
            done = ledger.get(h)
            if done and done["status"] in ("done", "dead", "skipped", "nocaptions", "manual"):
                continue
            work.append({"url": url, "hash": h, "kind": k, "item": item})
    return work


def cmd_run(limit: int | None, kinds: set[str]) -> None:
    work = build_worklist(kinds)
    if limit:
        work = work[:limit]
    print(f"{len(work)} urls to fetch", flush=True)
    counts: dict[str, int] = {}
    touched: dict[str, dict] = {}
    for i, w in enumerate(work, 1):
        kind = w["kind"]
        try:
            status, meta, body = FETCHERS[kind](w["url"])
        except Exception as e:
            status, meta, body = "error", {"error": str(e)[:150]}, ""
        entry = {"hash": w["hash"], "url": canonical(w["url"]), "kind": kind,
                 "status": status, "date": str(date.today()), **{
                     k: v for k, v in meta.items() if k in ("title", "error")}}
        if body:
            name = f"{kind}-{w['hash'][:6]}.md"
            path = write_enrichment(w["item"]["id"], name, w["url"], meta, body)
            entry["path"] = str(path.relative_to(ROOT))
            touched[w["item"]["id"]] = w["item"]
            if MEDIA_FETCH != "none":
                for n, murl in enumerate(meta.get("_media", [])[:4]):
                    try:
                        ext = (urllib.parse.urlsplit(murl).path.rsplit(".", 1)[-1] or "jpg").lower()
                        if len(ext) > 4 or "/" in ext:
                            ext = "jpg"
                        mreq = urllib.request.Request(murl, headers=UA)
                        data = urllib.request.urlopen(mreq, timeout=30).read()
                        mdest = ENRICH / w["item"]["id"] / f"media-{n}.{ext}"
                        mdest.parent.mkdir(parents=True, exist_ok=True)
                        mdest.write_bytes(data)
                    except Exception:
                        pass
        ledger_append(entry)
        counts[status] = counts.get(status, 0) + 1
        if i % 25 == 0 or i == len(work):
            print(f"  [{i}/{len(work)}] {counts}", flush=True)
        time.sleep(SLEEP.get(kind, 1.0))
    for item in touched.values():
        update_item_frontmatter(item)
    print("run complete:", counts, flush=True)


def cmd_refresh_frontmatter() -> None:
    for item in corpus_items():
        update_item_frontmatter(item)
    print("frontmatter refreshed")


def cmd_inventory() -> None:
    work = build_worklist({"youtube", "blog", "github", "paper", "tweet"})
    counts: dict[str, int] = {}
    for w in work:
        counts[w["kind"]] = counts.get(w["kind"], 0) + 1
    ledger = load_ledger()
    lcounts: dict[str, int] = {}
    for e in ledger.values():
        lcounts[e["status"]] = lcounts.get(e["status"], 0) + 1
    print("pending unique urls by kind:", counts)
    print("ledger so far:", lcounts or "(empty)")


def cmd_status() -> None:
    ledger = load_ledger()
    by: dict[str, dict[str, int]] = {}
    for e in ledger.values():
        by.setdefault(e["kind"], {}).setdefault(e["status"], 0)
        by[e["kind"]][e["status"]] += 1
    for k, v in sorted(by.items()):
        print(f"{k}: {v}")


def cmd_whisper() -> None:
    import os
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY not set (env or aikb/.env)")
    import yt_dlp

    ledger = load_ledger()
    todo = [e for e in ledger.values() if e["status"] == "nocaptions"]
    # map url hash -> owning item
    owners = {}
    for item in corpus_items():
        for url in item["urls"]:
            owners.setdefault(uhash(url), item)
    print(f"{len(todo)} videos to transcribe", flush=True)
    scratch = ROOT / "state" / "audio"
    scratch.mkdir(parents=True, exist_ok=True)
    for i, e in enumerate(todo, 1):
        item = owners.get(e["hash"])
        if not item:
            continue
        base = scratch / e["hash"]
        opts = {"quiet": True, "no_warnings": True,
                "format": "bestaudio/best", "outtmpl": str(base) + ".%(ext)s",
                "postprocessors": [{"key": "FFmpegExtractAudio",
                                    "preferredcodec": "mp3", "preferredquality": "32"}]}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(e["url"])
        except yt_dlp.utils.DownloadError as err:
            ledger_append({**e, "status": "error", "error": str(err)[:120]})
            continue
        mp3 = base.with_suffix(".mp3")
        if not mp3.exists() or mp3.stat().st_size > 24_000_000:
            status = "toolong" if mp3.exists() else "error"
            ledger_append({**e, "status": status})
            mp3.unlink(missing_ok=True)
            continue
        boundary = "----aikb"
        payload = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n"
            f"whisper-1\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"a.mp3\"\r\nContent-Type: audio/mpeg\r\n\r\n"
        ).encode() + mp3.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions", data=payload,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            resp = json.load(urllib.request.urlopen(req, timeout=600))
            text = resp.get("text", "")
        except Exception as err:
            ledger_append({**e, "status": "error", "error": str(err)[:120]})
            mp3.unlink(missing_ok=True)
            continue
        mp3.unlink(missing_ok=True)
        meta = {"title": (info.get("title") or "").replace(":", " -"),
                "channel": info.get("channel"), "via": "whisper"}
        name = f"youtube-{e['hash'][:6]}.md"
        path = write_enrichment(item["id"], name, e["url"], meta, text)
        ledger_append({**e, "status": "done", "path": str(path.relative_to(ROOT)),
                       "via": "whisper"})
        update_item_frontmatter(item)
        print(f"  [{i}/{len(todo)}] {meta['title'][:60]}", flush=True)


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "inventory"
    if cmd == "inventory":
        cmd_inventory()
    elif cmd == "status":
        cmd_status()
    elif cmd == "refresh-frontmatter":
        cmd_refresh_frontmatter()
    elif cmd == "whisper":
        cmd_whisper()
    elif cmd == "run":
        limit = None
        kinds = {"youtube", "blog", "github", "paper", "tweet"}
        if "--limit" in args:
            limit = int(args[args.index("--limit") + 1])
        if "--kinds" in args:
            kinds = set(args[args.index("--kinds") + 1].split(","))
        cmd_run(limit, kinds)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
