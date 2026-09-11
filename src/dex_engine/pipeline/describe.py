"""Media description writing: ``enrich item describe``.

A description is the session's written stand-in for a file nothing
transcribes and nothing extracts — an image, a markdown document the item
carries — and it was the one enrichment file a session still wrote by
hand. The reading is the judgment; the rest is plumbing this module owns:
the ``media-<n>.md`` slot the file takes, the first line that names the
file it covers, and the item's derived ``enrichment:``/``status``, which
refresh in the same call. Hand-written, a description for the standing
describe queue was followed by no verb, so its item read as undescribed
and ``raw`` until the next run's sweep.

Only a file the item carries can be described, read exactly as the
describe queue counts them: a path the item's ``media:`` states that is a
file under the instance root, or a ``media-<n>.<ext>`` download in
``enrichment/<id>/``. Anything else is refused — a description can only
close a row that exists. An extraction asset is refused with the rest: a
figure lifted out of a document already extracted owes nothing, and a
description of one would be counted against a media file it does not
cover.

Rewriting is allowed — a session revising its reading describes the file
again — and the engine's own first line is how the earlier description is
found: slots are counted, never paired to a download's number, so that
line is the only tie between a description and the file it covers.
"""

from pathlib import Path

from dex_engine import atomic, corpus

from .run import RunContext, is_media_file, live_item, refresh_item_frontmatter
from .types import Instance
from .urls import resolve_repo_path

__all__ = ["DescribeError", "item_describe"]


class DescribeError(ValueError):
    """A description names no file the item carries, or holds no text."""


def item_describe(item_id: str, *, of: str, text_path: Path, ctx: RunContext) -> str:
    """Write one description of a file the item carries, refreshing its frontmatter.

    Args:
        item_id: The corpus item, resolved as every reader resolves it
            (:func:`live_item`).
        of: The file described — a path the item's ``media:`` states, or
            the bare name of a ``media-<n>.<ext>`` download in its
            enrichment directory.
        text_path: The description text (conventionally under ``cache/``).
        ctx: The run context.

    Returns:
        A one-line confirmation naming the file written.

    Raises:
        ValueError: No live item answers to ``item_id``.
        DescribeError: The item does not parse, ``of`` is not a file it
            carries, or the text is empty. Nothing is written.
        OSError: The text cannot be read, or the description cannot be
            written.
    """
    instance = ctx.instance
    item = _corpus_item(
        instance, live_item(instance, item_id, claim="a description covers an existing item's file")
    )
    if not _carried(instance, item, of):
        raise DescribeError(
            f"{of!r} is not a file {item.id} carries — a description covers a path its "
            f"media: states, or a media-<n>.<ext> download in enrichment/{item.id}/"
        )
    text = text_path.read_text(encoding="utf-8").strip()
    if not text:
        raise DescribeError(f"{text_path}: the description is empty — it stands in for nothing")
    header = f"Describes `{of}`"
    item_dir = instance.enrichment_dir / item.id
    target = _target(item_dir, header)
    rewrite = target.exists()
    item_dir.mkdir(parents=True, exist_ok=True)
    atomic.write_text(target, f"{header}\n\n{text}\n")
    confirmation = (
        f"{'rewrote' if rewrite else 'wrote'} enrichment/{item.id}/{target.name} · describes {of}"
    )
    detail = refresh_item_frontmatter(instance, item.id)
    if detail is not None:
        confirmation += f" · frontmatter NOT refreshed: {detail}"
    return confirmation


def _corpus_item(instance: Instance, item_id: str) -> corpus.CorpusItem:
    """The live item: the source of the ``media:`` paths a description may cover.

    Raises:
        DescribeError: The item does not parse.
    """
    rel = f"corpus/{item_id[:4]}/{item_id}.md"
    try:
        return corpus.read_item(instance.corpus_dir / item_id[:4] / f"{item_id}.md")
    except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError) as e:
        raise DescribeError(f"{rel} does not parse, so it states no media to describe: {e}") from e


def _carried(instance: Instance, item: corpus.CorpusItem, of: str) -> bool:
    """Whether ``of`` is a file the item carries, as the describe queue counts them."""
    if of in item.media:
        stated = resolve_repo_path(instance.root, of)
        return stated is not None and stated.is_file()
    download = instance.enrichment_dir / item.id / of
    return of.startswith("media-") and download.name == of and is_media_file(download)


def _target(item_dir: Path, header: str) -> Path:
    """The description already opening with ``header``, else the lowest free slot."""
    for path in sorted(item_dir.glob("media-*.md")):
        if _first_line(path) == header:
            return path
    n = 0
    while (item_dir / f"media-{n}.md").exists():
        n += 1
    return item_dir / f"media-{n}.md"


def _first_line(path: Path) -> str | None:
    """The file's first line, or None for one that cannot be read as text."""
    try:
        return path.read_text(encoding="utf-8").partition("\n")[0]
    except (OSError, UnicodeDecodeError):
        return None
