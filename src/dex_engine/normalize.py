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


def _is_text(value: object) -> bool:
    """Whether a field arrived as the non-empty string the code reads it as."""
    return isinstance(value, str) and bool(value)


def _unreadable_reason(message: object) -> str | None:
    """Why this message cannot be read, or ``None`` when it can.

    Every field the clustering and emit passes index directly is checked
    here once, so those passes may index — and checked in the shape those
    passes use it, not merely for presence: a container they index into, a
    timestamp they parse, a display name the corpus takes as a scalar.
    DiscordChatExporter writes all of them, in those shapes, at every
    version that exports JSON. An export converted from another source is
    where a field goes missing or arrives in another shape — Discord's own
    data package holds a message's attachment as a single URL string, and
    a conversion that copies the field across emits that verbatim — and
    such a message is skipped and counted rather than aborting its channel.
    """
    if not isinstance(message, dict):
        return "message is not an object"
    return (
        _unreadable_scalar_reason(message)
        or _unreadable_reference_reason(message.get("reference"))
        or _unreadable_list_reason(message)
        or _unreadable_author_reason(message.get("author"))
    )


def _unreadable_scalar_reason(message: dict) -> str | None:
    """Why the message's own fields cannot be read, or ``None`` when they can."""
    for field in ("id", "type", "timestamp"):
        if not message.get(field):
            return f"no {field}"
    if not isinstance(message["id"], str):
        # The read pass keys reply targets by it and the emit pass hashes
        # it into the shortid — an unhashable id dies at the key lookup.
        return "id is not text"
    try:
        datetime.fromisoformat(message["timestamp"])
    except (TypeError, ValueError):
        return "timestamp is not ISO 8601"
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        return "content is not text"
    return None


def _unreadable_reference_reason(reference: object) -> str | None:
    """Why the message's reply reference cannot be read, or ``None`` when it can."""
    if not reference:
        # `or {}`-shaped like the read pass: a conversion writes null, and
        # the read pass reads that as no reference.
        return None
    if not isinstance(reference, dict):
        return "reference is not an object"
    target = reference.get("messageId")
    if target is not None and not isinstance(target, str):
        # The read pass looks the reply target up by it, so it must be the
        # key shape the message ids are — an unhashable one dies in the
        # lookup itself.
        return "reference messageId is not text"
    return None


def _unreadable_list_reason(message: dict) -> str | None:
    """Why the message's lists cannot be read, or ``None`` when they can."""
    for field in ("attachments", "embeds", "reactions"):
        entries = message.get(field) or []
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            return f"{field} is not a list of objects"
    if any(
        not _is_text(attachment.get("url")) or not _is_text(attachment.get("fileName"))
        for attachment in message.get("attachments") or []
    ):
        return "attachment has no url or fileName"
    for embed in message.get("embeds") or []:
        for key in ("url", "title"):
            value = embed.get(key)
            # The emit pass keys unfurl titles by url and slugifies title —
            # both taken as text where present, like content above.
            if value is not None and not isinstance(value, str):
                return f"embed {key} is not text"
    if any(
        # `or 0`-shaped like the emit pass's sum, so the check and the use
        # read the field identically.
        not isinstance(reaction.get("count") or 0, int)
        for reaction in message.get("reactions") or []
    ):
        return "reaction count is not a number"
    return None


def _unreadable_author_reason(author: object) -> str | None:
    """Why the message's author cannot be read, or ``None`` when it can."""
    if not author:
        return "no author"
    if not isinstance(author, dict):
        return "author is not an object"
    if not author.get("id"):
        return "author has no id"
    name = author.get("nickname") or author.get("name")
    if not name or not isinstance(name, str):
        return "author has no name"
    if name != name.strip() or "\n" in name:
        # It becomes shared_by, a corpus scalar: one line, no edge space.
        return "author name is not a single trimmed line"
    return None


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
            # `or []`, not a default: a conversion writes the empty list
            # as null, and the read pass reads that as no attachments.
            for attachment in message.get("attachments") or []
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
        for attachment in message.get("attachments") or []
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
        for embed in message.get("embeds") or []
        if embed.get("url") and embed.get("title")
    }
    if external and embed_titles.get(external[0]):
        slug = slugify(embed_titles[external[0]])
    else:
        slug = slugify(URL_RE.sub("", all_text)[:80])
    reactions = sum(
        reaction.get("count") or 0
        for message in messages
        for reaction in message.get("reactions") or []
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
    regeneration must never reset them. An unchanged item is not rewritten
    at all — idempotent re-runs must not churn every corpus file on disk.
    """
    path = instance.corpus_dir / f"{item.date:%Y}" / f"{item.id}.md"
    on_disk: str | None = None
    if path.exists():
        on_disk = path.read_text(encoding="utf-8")
        try:
            existing = corpus.parse(on_disk)
        except corpus.CorpusSchemaError as e:
            warn(f"warn: {path} does not parse ({e}); regenerating fresh — status/enrichment reset")
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
    if corpus.serialize(item) == on_disk:
        return
    corpus.write_item(path, item)


def _read_channel(chan_dir: Path, channel: str, warn: Callable[[str], None]) -> list[list[dict]]:
    """The clusters of one channel export — the whole read, before any write.

    Every fault a channel's own export can raise belongs here: the file is
    read, the messages that survive :func:`_unreadable_reason` are the ones
    the emit pass can index and the corpus will take, and clustering is
    finished. So a channel that raises has written nothing, and the caller
    may say it was skipped.

    Args:
        chan_dir: ``raw/discord/<channel>/`` holding ``messages.json``.
        channel: The channel name (named in the unreadable-message warnings).
        warn: Where non-fatal warnings go.

    Returns:
        The clusters, in export order.

    Raises:
        ValueError: The file is not readable as an exporter JSON export.
    """
    data = json.loads((chan_dir / "messages.json").read_text(encoding="utf-8"))
    export = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(export, list):
        # Not this exporter's shape: a Discord data-package dump (a bare
        # array, and named messages.json too), or a conversion that never
        # landed. The caller names the channel and moves on.
        raise ValueError("no messages array — not a DiscordChatExporter JSON export")
    messages: list[dict] = []
    unreadable: dict[str, int] = {}
    for message in export:
        reason = _unreadable_reason(message)
        if reason is not None:
            unreadable[reason] = unreadable.get(reason, 0) + 1
        elif message["type"] in ("Default", "Reply") and (
            message.get("content") or message.get("attachments")
        ):
            messages.append(message)
    for reason, count in sorted(unreadable.items()):
        # Counted per reason, not per message: a conversion that drops a
        # field drops it on every message, and one line says so.
        warn(f"warn: raw/discord/{channel}: {count} message(s) unreadable ({reason}) — skipped")
    return _clusters(messages)


def _emit_clusters(  # noqa: PLR0913 — one keyword per seam: clusters, config, exclusions, warn
    clusters: list[list[dict]],
    channel: str,
    *,
    instance: Instance,
    config: Config,
    excluded: set[str],
    warn: Callable[[str], None],
) -> tuple[int, int, Exception | None]:
    """Write a channel's clusters as corpus items.

    A fault here is never the export's doing — the read pass has already
    taken every unreadable message out — but a full disk or a read-only
    tree still lands one mid-channel, with items on disk. The channel
    stops there and the fault comes back rather than up, so the caller
    names it beside the count that did land: a channel that wrote is never
    reported as skipped, and no channel's fault costs the run its report.

    The shape classes (TypeError/AttributeError/KeyError) are caught as
    the backstop for a field access :func:`_unreadable_reason` missed —
    an engine bug, twice shipped before this catch existed, that killed
    the whole run with a raw traceback AFTER items were on disk and took
    every channel's summary with it. Contained it is still loud: the
    summary line carries the fault and calls the channel incomplete, the
    same bargain the drain's per-unit broad catch makes.

    Args:
        clusters: The clusters from :func:`_read_channel`.
        channel: The channel name (stamped into provenance).
        instance: The instance.
        config: Instance config — ``internal_domains`` filters link noise.
        excluded: Shortids from :func:`load_exclusions` — never regenerated.
        warn: Where non-fatal regeneration warnings go.

    Returns:
        ``(written, skipped, fault)`` — the cluster counts up to the fault
        that stopped the channel, or ``None`` when it wrote them all.
    """
    written = skipped = 0
    for cluster in clusters:
        first = cluster[0]
        shortid = hashlib.sha1(f"{channel}/{first['id']}".encode()).hexdigest()[:6]  # noqa: S324 — content key, not a security context
        if shortid in excluded:
            skipped += 1
            continue
        try:
            kept = _emit_cluster(cluster, channel, instance, config, warn)
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
            return written, skipped, e
        if kept:
            written += 1
        else:
            skipped += 1
    return written, skipped, None


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

    Raises:
        ValueError: The file is not readable as an exporter JSON export.
            Nothing has been written when it raises: the read finishes
            before the first item is emitted.
        OSError: A corpus item could not be written. Items written before
            it are on disk — this channel is part-normalized.
    """
    clusters = _read_channel(chan_dir, channel, warn)
    written, skipped, fault = _emit_clusters(
        clusters,
        channel,
        instance=instance,
        config=config,
        excluded=excluded,
        warn=warn,
    )
    if fault is not None:
        raise fault
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
        try:
            clusters = _read_channel(chan_dir, chan_dir.name, lines.append)
        except (OSError, ValueError) as e:
            # An export that cannot be read at all — truncated mid-write,
            # non-UTF-8, the wrong tool's JSON — fails alone: it is named
            # here and every other channel still normalizes. Only the read
            # is contained, because only the read leaves the corpus
            # untouched: "skipped" has to be the whole truth about the
            # channel, never a calm word over a part-written one.
            detail = " ".join(str(e).split())
            lines.append(f"discord/{chan_dir.name}: export unreadable ({detail}) — skipped")
            continue
        written, skipped, fault = _emit_clusters(
            clusters,
            chan_dir.name,
            instance=instance,
            config=config,
            excluded=excluded,
            warn=lines.append,
        )
        if fault is not None:
            # Not the export's fault and not a skip: this channel has items
            # on disk and the rest of it does not. Named as that, with the
            # count, and the run finishes so its report still reaches the
            # operator — who has a part-normalized channel to re-run.
            detail = " ".join(str(fault).split())
            lines.append(
                f"discord/{chan_dir.name}: {written} items written, then write failed "
                f"({detail}) — channel incomplete"
            )
            continue
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
