"""Normalize raw Google Chat exports (Takeout format) into corpus items.

raw/gspace/<space>/messages.json -> corpus/YYYY/YYYY-MM-DD-<slug>-<id>.md

One corpus item per thread (topic_id). Deterministic + idempotent: re-running
regenerates the same files; fix bugs here and re-run, never hand-edit corpus.
"""

import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path.cwd()  # engine runs from the brain repo root
RAW = ROOT / "raw" / "gspace"
CORPUS = ROOT / "corpus"

# Volume-specific mappings live in state/normalize-config.json at the volume
# root: {"name_map": {...}, "internal_domains": [...], "noise_prefixes": [...]}
import json as _json
_cfg = {}
_cfg_path = ROOT / "state" / "normalize-config.json"
if _cfg_path.exists():
    _cfg = _json.loads(_cfg_path.read_text())
NAME_MAP = _cfg.get("name_map", {})
INTERNAL_DOMAINS = set(_cfg.get("internal_domains", [])) | {
    "docs.google.com", "drive.google.com", "mail.google.com", "chat.google.com",
    "calendar.google.com", "meet.google.com", "sites.google.com",
}
NOISE_PREFIXES = tuple(_cfg.get("noise_prefixes", ["Updated room membership"]))

# Out-of-scope threads (id + reason per line, tab-separated), curated by the
# scope-filter pass. Matched on the item id's trailing shortid so exclusions
# survive slug changes. See bin/exclude.py.
EXCLUSIONS = ROOT / "state" / "exclusions.tsv"


def load_exclusions() -> set[str]:
    if not EXCLUSIONS.exists():
        return set()
    return {
        line.split("\t")[0].rsplit("-", 1)[-1]
        for line in EXCLUSIONS.read_text().splitlines()
        if line.strip()
    }

URL_RE = re.compile(r"https?://[^\s>\)\]]+")


def is_internal(url: str) -> bool:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return any(host == d or host.endswith("." + d) for d in INTERNAL_DOMAINS)


def parse_date(s: str) -> datetime:
    # "Thursday, 11 January 2024 at 21:37:56 UTC"
    return datetime.strptime(s, "%A, %d %B %Y at %H:%M:%S %Z")


def kind_of(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower().removeprefix("www.")
    if host in ("youtube.com", "youtu.be", "m.youtube.com"):
        return "youtube"
    if host == "github.com":
        return "github"
    if host in ("arxiv.org", "openreview.net") or "huggingface.co/papers" in url:
        return "paper"
    if host in ("x.com", "twitter.com"):
        return "tweet"
    return "blog"


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "untitled"


def unfurl_titles(msg: dict) -> dict:
    """url -> title from Google's link annotations."""
    out = {}
    for ann in msg.get("annotations", []):
        meta = ann.get("url_metadata")
        if not meta:
            continue
        url = (meta.get("url") or {}).get(
            "private_do_not_access_or_else_safe_url_wrapped_value"
        )
        if url and meta.get("title"):
            out[url] = meta["title"]
    return out


def normalize_space(space_dir: Path, channel: str) -> tuple[int, int]:
    msgs = json.loads((space_dir / "messages.json").read_text())["messages"]

    threads: dict[str, list[dict]] = {}
    for m in msgs:
        text = m.get("text", "")
        if any(text.startswith(p) for p in NOISE_PREFIXES):
            continue
        if not text and not m.get("attached_files"):
            continue
        threads.setdefault(m["topic_id"], []).append(m)

    written = skipped = 0
    for topic_id, tmsgs in threads.items():
        tmsgs.sort(key=lambda m: parse_date(m["created_date"]))
        all_text = "\n".join(m.get("text", "") for m in tmsgs)
        urls = [u.rstrip(".,") for u in URL_RE.findall(all_text)]
        external = [u for u in dict.fromkeys(urls) if not is_internal(u)]
        attachments = [
            f["export_name"]
            for m in tmsgs
            for f in m.get("attached_files", [])
        ]
        substance = len(re.sub(r"\s+", " ", URL_RE.sub("", all_text)).strip())

        # A thread earns an item via an external resource, a shared file, or
        # enough prose to stand alone as knowledge.
        if not external and not attachments and substance < 200:
            skipped += 1
            continue

        first = tmsgs[0]
        date = parse_date(first["created_date"])
        titles = {}
        for m in tmsgs:
            titles.update(unfurl_titles(m))

        if external and titles.get(external[0]):
            slug = slugify(titles[external[0]])
        else:
            slug = slugify(URL_RE.sub("", all_text)[:80])
        shortid = hashlib.sha1(f"{channel}/{topic_id}".encode()).hexdigest()[:6]
        if shortid in EXCLUDED:
            skipped += 1
            continue
        item_id = f"{date:%Y-%m-%d}-{slug}-{shortid}"

        kinds = sorted({kind_of(u) for u in external}) or ["text"]
        reactions = sum(
            len(r.get("reactor_emails", [])) for m in tmsgs for r in m.get("reactions", [])
        )

        shared_by = NAME_MAP.get(first["creator"]["name"], first["creator"]["name"])
        body = []
        for m in tmsgs:
            who = NAME_MAP.get(m["creator"]["name"], m["creator"]["name"])
            d = parse_date(m["created_date"])
            body.append(f"**{who}** ({d:%Y-%m-%d %H:%M}):")
            if m.get("text"):
                body.append(m["text"])
            for f in m.get("attached_files", []):
                body.append(f"*[attached: {f['original_name']}]*")
            for url, title in unfurl_titles(m).items():
                body.append(f"> unfurl: {title}")
            body.append("")

        emit_item("gspace", "foundations", channel, item_id, date, shared_by,
                  external, kinds,
                  [f"raw/gspace/{channel}/{a}" for a in attachments],
                  reactions, body)
        written += 1

    return written, skipped


def emit_item(source, tier, channel, item_id, date, shared_by, urls, kinds,
              attachment_paths, reactions, body_lines) -> None:
    fm = [
        "---",
        f"id: {item_id}",
        f"source: {source}",
        f"channel: {channel}",
        f"shared_by: {shared_by}",
        f"date: {date:%Y-%m-%d}",
    ]
    if urls:
        fm.append("urls:")
        fm += [f"  - {u}" for u in urls]
    fm.append(f"kinds: [{', '.join(kinds)}]")
    if attachment_paths:
        fm.append("attachments:")
        fm += [f"  - {p}" for p in attachment_paths]
    if reactions:
        fm.append(f"reactions: {reactions}")
    fm += [f"tier: {tier}", "status: raw", "enrichment: []", "---", ""]

    out = CORPUS / f"{date:%Y}" / f"{item_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(fm + body_lines).rstrip() + "\n"
    # Preserve enricher-owned frontmatter across regeneration.
    if out.exists():
        old = out.read_text()
        for field in ("status", "enrichment"):
            kept = re.search(rf"^{field}: .*$", old, re.M)
            if kept:
                content = re.sub(rf"^{field}: .*$", kept.group(0), content,
                                 count=1, flags=re.M)
    out.write_text(content)


RAW_DISCORD = ROOT / "raw" / "discord"

# Same-author messages this close together are one post split across messages.
DISCORD_GAP_MIN = 15


def normalize_discord(chan_dir: Path, channel: str) -> tuple[int, int]:
    from datetime import datetime as dt

    data = json.loads((chan_dir / "messages.json").read_text())
    msgs = [m for m in data["messages"]
            if m["type"] in ("Default", "Reply")
            and (m.get("content") or m.get("attachments"))]

    clusters: list[list[dict]] = []
    by_msg_id: dict[str, list[dict]] = {}
    for m in msgs:
        ref = (m.get("reference") or {}).get("messageId")
        target = None
        if m["type"] == "Reply" and ref in by_msg_id:
            target = by_msg_id[ref]
        elif clusters:
            prev = clusters[-1][-1]
            gap = (dt.fromisoformat(m["timestamp"])
                   - dt.fromisoformat(prev["timestamp"])).total_seconds()
            if prev["author"]["id"] == m["author"]["id"] and gap <= DISCORD_GAP_MIN * 60:
                target = clusters[-1]
        if target is None:
            target = []
            clusters.append(target)
        target.append(m)
        by_msg_id[m["id"]] = target

    written = skipped = 0
    for tmsgs in clusters:
        all_text = "\n".join(m.get("content") or "" for m in tmsgs)
        urls = [u.rstrip(".,") for u in URL_RE.findall(all_text)]
        external = [u for u in dict.fromkeys(urls) if not is_internal(u)]
        attachments = [a["url"] for m in tmsgs for a in m.get("attachments", [])
                       if not a["url"].startswith("http")]
        substance = len(re.sub(r"\s+", " ", URL_RE.sub("", all_text)).strip())
        if not external and not attachments and substance < 200:
            skipped += 1
            continue

        first = tmsgs[0]
        shortid = hashlib.sha1(f"{channel}/{first['id']}".encode()).hexdigest()[:6]
        if shortid in EXCLUDED:
            skipped += 1
            continue
        date = dt.fromisoformat(first["timestamp"])
        embed_titles = {e["url"]: e["title"] for m in tmsgs for e in m.get("embeds", [])
                        if e.get("url") and e.get("title")}
        if external and embed_titles.get(external[0]):
            slug = slugify(embed_titles[external[0]])
        else:
            slug = slugify(URL_RE.sub("", all_text)[:80])
        item_id = f"{date:%Y-%m-%d}-{slug}-{shortid}"
        kinds = sorted({kind_of(u) for u in external}) or ["text"]
        reactions = sum(r.get("count", 0) for m in tmsgs for r in m.get("reactions", []))
        shared_by = first["author"].get("nickname") or first["author"]["name"]

        body = []
        for m in tmsgs:
            who = m["author"].get("nickname") or m["author"]["name"]
            d = dt.fromisoformat(m["timestamp"])
            body.append(f"**{who}** ({d:%Y-%m-%d %H:%M}):")
            if m.get("content"):
                body.append(m["content"])
            for a in m.get("attachments", []):
                body.append(f"*[attached: {a['fileName']}]*")
            for url, title in embed_titles.items():
                if m is first:
                    body.append(f"> unfurl: {title}")
            body.append("")

        emit_item("discord", "frontier", channel, item_id, date, shared_by,
                  external, kinds,
                  [f"raw/discord/{channel}/{a}" for a in attachments],
                  reactions, body)
        written += 1

    return written, skipped


EXCLUDED: set[str] = set()


def main() -> None:
    EXCLUDED.update(load_exclusions())
    found = False
    for d in sorted(RAW.iterdir()) if RAW.exists() else []:
        if (d / "messages.json").exists():
            found = True
            written, skipped = normalize_space(d, d.name)
            print(f"gspace/{d.name}: {written} items written, {skipped} threads skipped")
    for d in sorted(RAW_DISCORD.iterdir()) if RAW_DISCORD.exists() else []:
        if (d / "messages.json").exists():
            found = True
            written, skipped = normalize_discord(d, d.name)
            print(f"discord/{d.name}: {written} items written, {skipped} clusters skipped")
    if not found:
        sys.exit("no exports found under raw/")


if __name__ == "__main__":
    main()
