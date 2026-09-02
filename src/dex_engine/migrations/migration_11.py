"""Migration 11 — repair gspace items that attach a colliding export file.

Google Chat's Takeout export disambiguates on disk and not in its
manifest: files whose names collide land as ``File-image.png``,
``File-image(1).png``, ``File-image(2).png``…, while every one of their
manifest records carries the bare ``export_name: "File-image.png"``. The
hand normalizer that produced these corpus items copied ``export_name``
verbatim into ``attachments:``, migration 8 derived ``media:`` from that
record, and so every colliding item got a copy of the FIRST file's bytes
— an image nothing in that message ever attached, read as its own by
every digest and every extraction since.

The manifest is still enough to repair them, because the disk names are
a function of it: the k-th occurrence of an ``export_name``, counted in
message order across the whole channel and in list order within a
message, is on disk as that name with ``(k)`` inserted before the last
dot — bare for k=0. Verified against a real export, 2026-09-02.

So this reads ``raw/gspace/<channel>/messages.json`` as a manifest and
nothing more — no reader for the source is grown here, and nothing is
re-normalized. Per topic it computes both spellings of the attachment
list, the export's and the corrected one; a topic whose two spellings
agree has nothing wrong with it. A member's item is rewritten only when
its stored list is still exactly the export's spelling — anything else
is judgment this migration will not overwrite — and only when every
corrected source is on disk, because a repair that guesses at bytes is
worse than the defect it heals.

Idempotent by construction: a rewritten item's ``attachments:`` is the
corrected spelling, which is not the export's, so a second apply finds
it healed and writes nothing.
"""

import dataclasses
import datetime
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

from dex_engine import corpus
from dex_engine.normalize import materialize_attachments
from dex_engine.pipeline.types import Instance, MigrationReport, Skipped
from dex_engine.pipeline.urls import resolve_repo_path

__all__ = ["GspaceAttachmentCollisions", "build"]

NUMBER = 11
INTENT = (
    "repair gspace attachment collisions: the export recorded one export_name for every "
    "file it wrote to disk as name(1), name(2)…, so items attaching a later collision "
    "carry a copy of the first file's bytes"
)

_WHY = (
    "the export named every colliding attachment File-image.png in its manifest while "
    "writing them to disk as File-image(1).png, File-image(2).png… — each rewritten item "
    "now attaches the file its own message carried. A standing digest still lists the old "
    "media path, so every rewritten item reads as media drift until `enrich item digest` "
    "re-emits it, and any media description written against the old copy describes the "
    "wrong file — re-read those"
)


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — the shared build signature; nothing here is dated
    now: Callable[[], datetime.datetime],  # noqa: ARG001 — and nothing here is a ledger line
    engine_version: str,  # noqa: ARG001 — nothing here is version-stamped
) -> "GspaceAttachmentCollisions":
    """Build migration 11 (the shared build signature; no state is stamped)."""
    return GspaceAttachmentCollisions()


class GspaceAttachmentCollisions:
    """Migration 11: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def apply(self, root: Path) -> MigrationReport:
        """Re-spell every colliding gspace attachment list and recopy its files.

        Args:
            root: The instance root.

        Returns:
            The report: per-channel counts and the sentence naming what the
            export did wrong, a skip per topic this migration declined to
            touch, and an anomaly per unreadable manifest or absent source.
        """
        gspace = root / "raw" / "gspace"
        if not gspace.is_dir():
            return MigrationReport()
        report = MigrationReport()
        items = _items_by_shortid(root)
        instance = Instance(root=root)
        for manifest in sorted(gspace.glob("*/messages.json")):
            channel = manifest.parent.name
            try:
                collisions = _collisions(manifest, channel)
            except _ManifestError as e:
                report.anomalies.append(
                    f"raw/gspace/{channel}/messages.json {e} — the channel's attachment "
                    "lists are left exactly as they are"
                )
                continue
            repair = _ChannelRepair(channel=channel, instance=instance, items=items, report=report)
            for topic_id, collision in collisions.items():
                repair.topic(topic_id, collision)
            repair.finish()
        if report.actions:
            report.actions.append(_WHY)
        return report


class _Collision(NamedTuple):
    """One topic's attachment list in both spellings — the export's, and true."""

    stored: list[str]
    corrected: list[str]


class _ManifestError(ValueError):
    """A channel manifest is not the shape the ordinals are counted from."""


class _ChannelRepair:
    """One channel's repair: the decisions, the writes, and what they report."""

    def __init__(
        self,
        *,
        channel: str,
        instance: Instance,
        items: dict[str, Path],
        report: MigrationReport,
    ) -> None:
        """Repair one channel, reporting into the run's shared report."""
        self._channel = channel
        self._instance = instance
        self._items = items
        self._report = report
        self._rewritten = self._copied = self._removed = 0

    def topic(self, topic_id: str, collision: _Collision) -> None:
        """Rewrite the topic's item, or report why it was left alone."""
        shortid = _shortid(self._channel, topic_id)
        path = self._items.get(shortid)
        if path is None:
            self._report.skipped.append(
                Skipped(
                    what=f"raw/gspace/{self._channel} topic {topic_id}",
                    why=(
                        f"no corpus item id ends in -{shortid}, so nothing holds this "
                        "topic's attachment list — the export's colliding files stand"
                    ),
                )
            )
            return
        rel = path.relative_to(self._instance.root).as_posix()
        try:
            item = corpus.read_item(path)
        except (corpus.CorpusSchemaError, OSError, UnicodeDecodeError) as e:
            self._report.skipped.append(
                Skipped(what=rel, why=f"could not be read, so its attachments are unknown ({e})")
            )
            return
        if item.attachments == collision.corrected:
            return
        if item.attachments != collision.stored:
            self._report.skipped.append(
                Skipped(
                    what=rel,
                    why=(
                        "attachments: is neither the export's spelling nor the corrected "
                        "one — hand-written since, so nothing here overwrites it; the "
                        f"corrected list is {collision.corrected}"
                    ),
                )
            )
            return
        missing = [
            source for source in collision.corrected if not (self._instance.root / source).is_file()
        ]
        if missing:
            self._report.anomalies.append(
                f"{rel} should attach {missing}, which the export never wrote — nothing "
                "rewritten for it; the item keeps the wrong copy until the missing "
                "file(s) are restored under raw/"
            )
            return
        self._rewrite(path, item, collision.corrected, shortid)

    def finish(self) -> None:
        """Record what the channel's rewrites did, when they did anything."""
        if not self._rewritten:
            return
        self._report.actions.append(
            f"raw/gspace/{self._channel}: {self._rewritten} item(s) rewritten, "
            f"{self._copied} file(s) copied out of raw/, {self._removed} stale "
            "media file(s) deleted"
        )

    def _rewrite(
        self, path: Path, item: corpus.CorpusItem, corrected: list[str], shortid: str
    ) -> None:
        root = self._instance.root
        present = _materialized(root, shortid)
        dropped: list[str] = []
        media = materialize_attachments(
            corrected, shortid, instance=self._instance, warn=dropped.append
        )
        rel = path.relative_to(root).as_posix()
        self._report.skipped.extend(
            Skipped(what=rel, why=line.removeprefix("warn: ")) for line in dropped
        )
        self._copied += sum(1 for entry in media if entry.rsplit("/", 1)[-1] not in present)
        # The stale copies go before the item is written: an apply interrupted
        # here leaves the export's spelling stored, so the re-apply repeats the
        # whole repair — where the reverse order would strand them forever.
        self._removed += _remove_stale(root, shortid, old=item.media, new=media)
        corpus.write_item(path, dataclasses.replace(item, attachments=corrected, media=media))
        self._rewritten += 1


def _collisions(manifest: Path, channel: str) -> dict[str, _Collision]:
    """Every topic in the manifest whose attachment list needs re-spelling.

    Args:
        manifest: The channel's ``messages.json``.
        channel: The channel directory's name, which the paths are under.

    Returns:
        Topic id -> both spellings, for the topics where they differ.

    Raises:
        _ManifestError: The file does not parse, or is not the shape the
            occurrence counting depends on.
    """
    occurrences: Counter[str] = Counter()
    stored: dict[str, list[str]] = defaultdict(list)
    corrected: dict[str, list[str]] = defaultdict(list)
    for topic_id, export_name in _attached(manifest):
        ordinal = occurrences[export_name]
        occurrences[export_name] += 1
        stored[topic_id].append(f"raw/gspace/{channel}/{export_name}")
        corrected[topic_id].append(f"raw/gspace/{channel}/{_on_disk(export_name, ordinal)}")
    return {
        topic_id: _Collision(stored=paths, corrected=corrected[topic_id])
        for topic_id, paths in stored.items()
        if paths != corrected[topic_id]
    }


def _attached(manifest: Path) -> Iterator[tuple[str, str]]:
    """Every attached file in the manifest, in order, as (topic id, export name).

    Order is the whole point: the ordinal a file's disk name carries is its
    position in this stream. A message that attaches nothing contributes
    nothing and is read no further; anything else that is not the shape
    below refuses the channel rather than mis-counting the rest of it.
    """
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise _ManifestError(f"does not parse ({e})") from e
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise _ManifestError('is not {"messages": [...]}')
    for position, message in enumerate(payload["messages"], start=1):
        if not isinstance(message, dict):
            raise _ManifestError(f"has a message at position {position} that is not an object")
        files = message.get("attached_files") or []
        if not isinstance(files, list):
            raise _ManifestError(
                f"has a message at position {position} whose attached_files is not a list"
            )
        if not files:
            continue
        topic_id = message.get("topic_id")
        if not isinstance(topic_id, str) or not topic_id:
            raise _ManifestError(
                f"has a message at position {position} that attaches files under no topic_id"
            )
        for entry in files:
            name = entry.get("export_name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or "/" in name or name in ("", ".", ".."):
                raise _ManifestError(
                    f"has an attachment at message position {position} whose export_name "
                    "is not a plain file name"
                )
            yield topic_id, name


def _on_disk(export_name: str, ordinal: int) -> str:
    """The name the export actually wrote for this occurrence of a name.

    The first occurrence keeps the bare name; every later one takes its
    ordinal in parentheses before the LAST dot, or appended when the name
    has no dot at all.
    """
    if ordinal == 0:
        return export_name
    stem, dot, extension = export_name.rpartition(".")
    if not dot:
        return f"{export_name}({ordinal})"
    return f"{stem}({ordinal}){dot}{extension}"


def _shortid(channel: str, topic_id: str) -> str:
    """The shortid the hand normalizer derived for a topic's corpus item."""
    return hashlib.sha1(f"{channel}/{topic_id}".encode()).hexdigest()[:6]  # noqa: S324 — reproducing an id, not a security context


def _items_by_shortid(root: Path) -> dict[str, Path]:
    """Every corpus item by its id's trailing shortid; first in sort order wins."""
    corpus_dir = root / "corpus"
    if not corpus_dir.is_dir():
        return {}
    items: dict[str, Path] = {}
    for path in sorted(corpus_dir.rglob("*.md")):
        items.setdefault(path.stem.rsplit("-", 1)[-1], path)
    return items


def _materialized(root: Path, shortid: str) -> set[str]:
    """The names already under ``media/<shortid>/`` — what a copy this run adds to."""
    directory = root / "media" / shortid
    return {entry.name for entry in directory.iterdir()} if directory.is_dir() else set()


def _remove_stale(root: Path, shortid: str, *, old: list[str], new: list[str]) -> int:
    """Delete the copies the old ``media:`` named and the new one does not.

    Only inside the item's own ``media/<shortid>/`` — a stored path is data,
    and a migration that unlinks whatever one points at is a migration that
    deletes the instance.
    """
    directory = (root / "media" / shortid).resolve()
    keep = set(new)
    removed = 0
    for entry in old:
        if entry in keep:
            continue
        path = resolve_repo_path(root, entry)
        if path is None or path.parent != directory or not path.is_file():
            continue
        path.unlink()
        removed += 1
    return removed
