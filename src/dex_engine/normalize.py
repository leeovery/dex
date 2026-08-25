"""dex-normalize: raw chat exports → corpus items.

``raw/discord/<channel>/messages.json`` (DiscordChatExporter JSON)
→ ``corpus/YYYY/YYYY-MM-DD-<slug>-<id>.md``

One corpus item per message cluster. Deterministic and idempotent:
re-running regenerates the same files; fix bugs here and re-run, never
hand-edit corpus. Kind detection is the shared pipeline module's job:
kinds are stamped pattern-only here and are provisional forever — the ledger
is authoritative. Regeneration preserves the two enricher-owned frontmatter
fields (``status``, ``enrichment``) and emits through ``corpus.py``, the one
frontmatter write point.

Out-of-scope clusters are excluded via ``state/exclusions.tsv`` (id + reason
per line, tab-separated), curated by the scope-filter pass and matched on
the item id's trailing shortid so exclusions survive slug changes. See
``exclude.py``.
"""

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import corpus
from .pipeline.detect import detect_kind
from .pipeline.registry import DRIVERS
from .pipeline.types import Config, Instance

__all__ = [
    "DISCORD_GAP_MIN",
    "build_parser",
    "kind_of",
    "load_exclusions",
    "main",
    "normalize_discord",
    "run_normalize",
    "slugify",
]

URL_RE = re.compile(r"https?://[^\s>\)\]]+")

# Same-author messages this close together are one post split across messages.
DISCORD_GAP_MIN = 15

# A cluster with no external URL and no attachment needs this much prose to
# be knowledge rather than chatter.
_MIN_SUBSTANCE_CHARS = 200


def load_exclusions(path: Path) -> set[str]:
    """The excluded shortids from ``state/exclusions.tsv`` (trailing-id match).

    Args:
        path: The exclusions file; missing means none.

    Returns:
        The shortids whose clusters are never regenerated.
    """
    if not path.exists():
        return set()
    return {
        line.split("\t")[0].rsplit("-", 1)[-1]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def kind_of(url: str) -> str:
    """The provisional kind for a captured URL, via the shared detect module.

    The private ``kind_of`` copy — and its divergence from the enricher's —
    is deleted. Pattern-only here; the ledger is authoritative.
    """
    return str(detect_kind(url, DRIVERS))


def slugify(text: str, max_len: int = 40) -> str:
    """A filename slug: lowercase, hyphenated, bounded, never empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def _is_internal(url: str, internal_domains: list[str]) -> bool:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    return any(host == d or host.endswith("." + d) for d in internal_domains)


# ---------------------------------------------------------------------------
# Discord (DiscordChatExporter JSON)
# ---------------------------------------------------------------------------


def _clusters(messages: list[dict]) -> list[list[dict]]:
    """Group messages: replies join their target; same-author runs merge (gap rule)."""
    clusters: list[list[dict]] = []
    by_msg_id: dict[str, list[dict]] = {}
    for message in messages:
        ref = (message.get("reference") or {}).get("messageId")
        target = None
        if message["type"] == "Reply" and ref in by_msg_id:
            target = by_msg_id[ref]
        elif clusters:
            prev = clusters[-1][-1]
            gap = (
                datetime.fromisoformat(message["timestamp"])
                - datetime.fromisoformat(prev["timestamp"])
            ).total_seconds()
            if prev["author"]["id"] == message["author"]["id"] and gap <= DISCORD_GAP_MIN * 60:
                target = clusters[-1]
        if target is None:
            target = []
            clusters.append(target)
        target.append(message)
        by_msg_id[message["id"]] = target
    return clusters


def _display_name(author: dict) -> str:
    return author.get("nickname") or author["name"]


def _cluster_body(messages: list[dict], embed_titles: dict[str, str]) -> str:
    lines: list[str] = []
    first = messages[0]
    for message in messages:
        who = _display_name(message["author"])
        stamp = datetime.fromisoformat(message["timestamp"])
        lines.append(f"**{who}** ({stamp:%Y-%m-%d %H:%M}):")
        if message.get("content"):
            lines.append(message["content"])
        lines.extend(
            f"*[attached: {attachment['fileName']}]*"
            for attachment in message.get("attachments", [])
        )
        if message is first:
            lines.extend(f"> unfurl: {title}" for title in embed_titles.values())
        lines.append("")
    return "\n" + "\n".join(lines).rstrip() + "\n"


def _emit_cluster(
    messages: list[dict],
    channel: str,
    instance: Instance,
    config: Config,
    warn: Callable[[str], None],
) -> bool:
    """Emit one cluster as a corpus item; False when it is too thin to keep."""
    all_text = "\n".join(message.get("content") or "" for message in messages)
    urls = [url.rstrip(".,") for url in URL_RE.findall(all_text)]
    external = [
        url for url in dict.fromkeys(urls) if not _is_internal(url, config.internal_domains)
    ]
    attachments = [
        attachment["url"]
        for message in messages
        for attachment in message.get("attachments", [])
        if not attachment["url"].startswith("http")
    ]
    substance = len(re.sub(r"\s+", " ", URL_RE.sub("", all_text)).strip())
    if not external and not attachments and substance < _MIN_SUBSTANCE_CHARS:
        return False

    first = messages[0]
    shortid = hashlib.sha1(f"{channel}/{first['id']}".encode()).hexdigest()[:6]  # noqa: S324 — content key, not a security context
    date = datetime.fromisoformat(first["timestamp"])
    embed_titles = {
        embed["url"]: embed["title"]
        for message in messages
        for embed in message.get("embeds", [])
        if embed.get("url") and embed.get("title")
    }
    if external and embed_titles.get(external[0]):
        slug = slugify(embed_titles[external[0]])
    else:
        slug = slugify(URL_RE.sub("", all_text)[:80])
    reactions = sum(
        reaction.get("count", 0)
        for message in messages
        for reaction in message.get("reactions", [])
    )
    item = corpus.CorpusItem(
        id=f"{date:%Y-%m-%d}-{slug}-{shortid}",
        source="discord",
        channel=channel,
        shared_by=_display_name(first["author"]),
        date=date.date(),
        urls=external,
        kinds=sorted({kind_of(url) for url in external}) or ["text"],
        reactions=reactions or None,
        attachments=[f"raw/discord/{channel}/{a}" for a in attachments],
        body=_cluster_body(messages, embed_titles),
    )
    _write_preserving(instance, item, warn)
    return True


def _write_preserving(
    instance: Instance, item: corpus.CorpusItem, warn: Callable[[str], None]
) -> None:
    """Write the item, preserving enricher-owned fields across regeneration.

    ``status`` and ``enrichment`` are the two fields the enricher owns;
    regeneration must never reset them.
    """
    path = instance.corpus_dir / f"{item.date:%Y}" / f"{item.id}.md"
    if path.exists():
        try:
            existing = corpus.read_item(path)
        except corpus.CorpusSchemaError as e:
            warn(
                f"warn: {path} does not parse ({e}); regenerating fresh — "
                "status/enrichment reset"
            )
        else:
            item = corpus.CorpusItem(
                id=item.id,
                source=item.source,
                channel=item.channel,
                shared_by=item.shared_by,
                date=item.date,
                urls=item.urls,
                kinds=item.kinds,
                reactions=item.reactions,
                attachments=item.attachments,
                status=existing.status,
                enrichment=existing.enrichment,
                media=item.media,
                body=item.body,
            )
    corpus.write_item(path, item)


def normalize_discord(  # noqa: PLR0913 — one keyword per seam: paths, config, exclusions, warn
    chan_dir: Path,
    channel: str,
    *,
    instance: Instance,
    config: Config,
    excluded: set[str],
    warn: Callable[[str], None],
) -> tuple[int, int]:
    """Normalize one Discord channel export into corpus items.

    Args:
        chan_dir: ``raw/discord/<channel>/`` holding ``messages.json``.
        channel: The channel name (stamped into provenance).
        instance: The instance.
        config: Instance config — ``internal_domains`` filters link noise.
        excluded: Shortids from :func:`load_exclusions` — never regenerated.
        warn: Where non-fatal regeneration warnings go.

    Returns:
        ``(written, skipped)`` cluster counts.
    """
    data = json.loads((chan_dir / "messages.json").read_text(encoding="utf-8"))
    messages = [
        message
        for message in data["messages"]
        if message["type"] in ("Default", "Reply")
        and (message.get("content") or message.get("attachments"))
    ]
    written = skipped = 0
    for cluster in _clusters(messages):
        first = cluster[0]
        shortid = hashlib.sha1(f"{channel}/{first['id']}".encode()).hexdigest()[:6]  # noqa: S324 — content key, not a security context
        if shortid in excluded:
            skipped += 1
            continue
        if _emit_cluster(cluster, channel, instance, config, warn):
            written += 1
        else:
            skipped += 1
    return written, skipped


def run_normalize(instance: Instance, config: Config) -> list[str]:
    """Normalize every export under ``raw/``; one summary line per channel.

    Args:
        instance: The instance.
        config: The instance config.

    Returns:
        The summary lines.

    Raises:
        ValueError: No exports found under ``raw/``.
    """
    excluded = load_exclusions(instance.state_dir / "exclusions.tsv")
    raw_discord = instance.root / "raw" / "discord"
    lines: list[str] = []
    channel_dirs = sorted(raw_discord.iterdir()) if raw_discord.exists() else []
    for chan_dir in channel_dirs:
        if not (chan_dir / "messages.json").exists():
            continue
        written, skipped = normalize_discord(
            chan_dir,
            chan_dir.name,
            instance=instance,
            config=config,
            excluded=excluded,
            warn=lines.append,
        )
        lines.append(
            f"discord/{chan_dir.name}: {written} items written, {skipped} clusters skipped"
        )
    if not lines:
        raise ValueError("no exports found under raw/")
    return lines


# ---------------------------------------------------------------------------
# CLI — parse, build, call; zero business logic
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-normalize takes no flags."""
    return argparse.ArgumentParser(
        prog="dex-normalize",
        description="Normalize raw chat exports under raw/ into corpus items "
        "(deterministic; regeneration preserves enricher-owned fields).",
    )


def main(argv: list[str] | None = None) -> None:
    """Parse, build Instance/Config, normalize, print the summaries."""
    build_parser().parse_args(argv)
    instance = Instance(root=Path.cwd())
    try:
        config = Config.load(instance.config_path)
        lines = run_normalize(instance, config)
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(f"dex-normalize: {e}")
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
