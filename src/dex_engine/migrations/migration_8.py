"""Migration 8 — materialize backfill attachments into ``media/``.

``attachments:`` was write-only. Ledger seeding and unit ownership read
``media:`` alone, so every local file a backfill recorded was invisible to
the pipeline: documents never queued for extraction, images never entered a
digest's media listing, and digests were written from the surrounding chat
text alone. Normalize now derives ``media:`` from the attachment list and
copies the files out of ``raw/`` — but that heals only what a normalize run
regenerates, nobody re-runs normalize by hand, and one legacy source
(``raw/gspace/…``) has no export for it to walk at all.

So the derivation runs here once, keyed off the frontmatter field rather
than off what is under ``raw/``: every item with a non-empty
``attachments:`` gets ``media:`` set to what normalize would derive, and its
files copied. It is normalize's own function, given the stored paths and the
item's shortid — the two inputs normalize gives it — so a later normalize
run over the export that produced an item derives the same list and rewrites
nothing.

A missing source is tolerated exactly as normalize tolerates it: ``raw/`` is
not guaranteed to outlive the corpus it produced, the ``media:`` entry
stands, and the pipeline parks the unit that needs the bytes. A stored path
that reaches outside ``raw/`` is dropped from ``media:`` and named for the
session — a hand-written backfill's mistake is exactly what judgment is for.

Idempotent by construction: the copy is skip-if-present and ``media:`` is
derived from the attachment list alone, so a second apply copies nothing and
writes nothing.
"""

import dataclasses
import datetime
from collections.abc import Callable
from pathlib import Path

from dex_engine import corpus
from dex_engine.normalize import materialize_attachments
from dex_engine.pipeline.types import Instance, MigrationReport, Skipped

__all__ = ["AttachmentMaterialization", "build"]

NUMBER = 8
INTENT = (
    "materialize backfill attachments into media/: attachments: was write-only, so every "
    "local file a backfill recorded was invisible to the pipeline — never extracted, never "
    "in a digest's media listing"
)


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — the shared build signature; nothing here is dated
    now: Callable[[], datetime.datetime],  # noqa: ARG001 — and nothing here is a ledger line
    engine_version: str,  # noqa: ARG001 — nothing here is version-stamped
) -> "AttachmentMaterialization":
    """Build migration 8 (the shared build signature; no state is stamped)."""
    return AttachmentMaterialization()


class AttachmentMaterialization:
    """Migration 8: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def apply(self, root: Path) -> MigrationReport:
        """Derive every attachment-bearing item's ``media:`` and copy its files.

        Args:
            root: The instance root.

        Returns:
            The report: what was copied, rewritten and left without a source,
            plus a skip per item the derivation could not fully serve.
        """
        corpus_dir = root / "corpus"
        if not corpus_dir.is_dir():
            return MigrationReport()
        instance = Instance(root=root)
        skipped: list[Skipped] = []
        copied = missing = written = 0
        for path in sorted(corpus_dir.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            try:
                item = corpus.read_item(path)
            except (corpus.CorpusSchemaError, OSError, UnicodeDecodeError) as e:
                skipped.append(
                    Skipped(
                        what=rel, why=f"could not be read, so its attachments are unknown ({e})"
                    )
                )
                continue
            if not item.attachments:
                continue
            shortid = item.id.rsplit("-", 1)[-1]
            present = _materialized(root, shortid)
            dropped: list[str] = []
            media = materialize_attachments(
                item.attachments, shortid, instance=instance, warn=dropped.append
            )
            skipped.extend(Skipped(what=rel, why=line.removeprefix("warn: ")) for line in dropped)
            for entry in media:
                if not (root / entry).exists():
                    missing += 1
                elif entry.rsplit("/", 1)[-1] not in present:
                    copied += 1
            if media != item.media:
                corpus.write_item(path, dataclasses.replace(item, media=media))
                written += 1
        return MigrationReport(
            actions=_actions(copied=copied, written=written, missing=missing), skipped=skipped
        )


def _materialized(root: Path, shortid: str) -> set[str]:
    """The names already under ``media/<shortid>/`` — what a copy this run adds to."""
    directory = root / "media" / shortid
    return {entry.name for entry in directory.iterdir()} if directory.is_dir() else set()


def _actions(*, copied: int, written: int, missing: int) -> list[str]:
    """One line per thing that happened; each count stands on its own.

    A run can copy without rewriting (a source restored under ``raw/`` after
    an earlier apply already derived the entry) and rewrite without copying
    (every source long gone), so neither count speaks for the other.
    """
    actions: list[str] = []
    if copied:
        actions.append(f"media: {copied} attachment(s) copied out of raw/")
    if written:
        actions.append(f"corpus: {written} item(s) rewritten — media: derived from attachments:")
    if missing:
        actions.append(
            f"media: {missing} listed file(s) have no source under raw/ — the entries stand "
            "and the pipeline parks the units that need the bytes"
        )
    return actions
