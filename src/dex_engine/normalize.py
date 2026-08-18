"""Normalize raw chat exports into corpus items.

raw/discord/<channel>/messages.json (DiscordChatExporter JSON)
  -> corpus/YYYY/YYYY-MM-DD-<slug>-<id>.md

One corpus item per message cluster. Deterministic + idempotent: re-running
regenerates the same files; fix bugs here and re-run, never hand-edit corpus.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path.cwd()  # engine runs from the instance repo root
CORPUS = ROOT / "corpus"

# Instance configuration lives in state/normalize-config.json at the instance
# root: {"name_map": {...}, "internal_domains": [...]}
import json as _json
_cfg = {}
_cfg_path = ROOT / "state" / "normalize-config.json"
if _cfg_path.exists():
    _cfg = _json.loads(_cfg_path.read_text())
NAME_MAP = _cfg.get("name_map", {})
INTERNAL_DOMAINS = set(_cfg.get("internal_domains", []))

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


def emit_item(source, channel, item_id, date, shared_by, urls, kinds,
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
    fm += ["status: raw", "enrichment: []", "---", ""]

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

        emit_item("discord", channel, item_id, date, shared_by,
                  external, kinds,
                  [f"raw/discord/{channel}/{a}" for a in attachments],
                  reactions, body)
        written += 1

    return written, skipped


EXCLUDED: set[str] = set()


def main() -> None:
    EXCLUDED.update(load_exclusions())
    found = False
    for d in sorted(RAW_DISCORD.iterdir()) if RAW_DISCORD.exists() else []:
        if (d / "messages.json").exists():
            found = True
            written, skipped = normalize_discord(d, d.name)
            print(f"discord/{d.name}: {written} items written, {skipped} clusters skipped")
    if not found:
        sys.exit("no exports found under raw/")


if __name__ == "__main__":
    main()
