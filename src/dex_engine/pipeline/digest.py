"""Digest writing from a JSON payload: ``enrich item digest``.

Digests are the last state file a session wrote by hand, and the shape was
the session's to get right. The judgment is `signal`, `topics`, the
entities and the facts; the fence, the field order, the list style and the
bullets are serialization. So the judgment arrives as JSON and this module
writes the file — every shape the health check looks for is decided here,
by code, and none of them can come out wrong.

Two fields are never taken from the payload. `date` is the item's share
date, stated by the corpus item; `media:` is every media file the item
carries — the paths its frontmatter states, then the media-family files in
its enrichment directory. Both are derived from what is on disk, so a
payload naming either is refused rather than trusted.

Rewriting is allowed — a session revising its judgment writes the payload
again — and nothing carries over from the file being replaced. Every field
is either the payload's judgment or a fact of the item on disk, so a
rewrite is a full re-derivation and there is nothing only the old file
knew.

The verb also records the item's digest pass — the comparand the
staleness backstop reads — through the same seam ``enrich pass`` uses, so
a digest cannot land unrecorded. The record goes in after validation and
before the file write: a refused payload records nothing, and a crash
between the two writes leaves a pass with no digest file, which the
backstop's no-digest branch lists loudly. The other residue — a digest
with no pass — has no reader at all, and is exactly the silently
uncomparable state this recording exists to end.
"""

import json
import re
from pathlib import Path
from typing import NoReturn

from dex_engine import atomic, corpus

from .run import RunContext, is_media_file, record_pass
from .types import Instance

__all__ = ["SIGNALS", "DigestPayloadError", "item_digest", "item_media"]

SIGNALS = ("high", "medium", "low")

_REQUIRED_KEYS = frozenset({"id", "signal", "topics", "facts"})
_OPTIONAL_KEYS = frozenset({"entities"})
# Named in a payload, these would be a caller's guess at what the item on
# disk already shows — so they are refused rather than merged.
_DERIVED_KEYS = frozenset({"date", "media"})

# An item id names one file in the corpus tree and in `state/digests/`,
# never a path: anything with a separator in it would follow the write out
# of the instance.
_ITEM_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*")


class DigestPayloadError(ValueError):
    """A digest payload does not conform to the shape the verb writes."""


def item_digest(payload_path: Path, *, ctx: RunContext) -> str:
    """Write one item's digest from its JSON payload, recording the pass.

    The digest pass goes through :func:`record_pass` — the one
    pass-writing path — after the payload survives validation and before
    the file lands: a refused payload records nothing, and a crash
    between the record and the write leaves a pass with no digest file,
    the state the staleness backstop's no-digest branch reports loudly.
    Writing the file first would leave the opposite residue, a digest no
    record dates, which no reader ever flags.

    Args:
        payload_path: The JSON payload (conventionally under ``cache/``):
            ``{"id", "signal", "topics", "facts"}``, ``entities`` optional.
        ctx: The run context.

    Returns:
        A one-line confirmation naming the file written.

    Raises:
        DigestPayloadError: The payload is not a JSON object, a key is
            missing, unknown, engine-owned or the wrong type, or the id
            names no readable corpus item. Nothing is written and no pass
            is recorded.
        OSError: The payload file cannot be read, or the digest cannot be
            written.
    """
    instance = ctx.instance
    payload = _load(payload_path)
    item_id = _text(payload_path, payload, "id")
    if not _ITEM_ID_RE.fullmatch(item_id):
        _fail(
            payload_path,
            f"id {item_id!r} is not a corpus item id — an id names one file in the "
            "corpus tree, never a path",
        )
    signal = _text(payload_path, payload, "signal")
    if signal not in SIGNALS:
        _fail(payload_path, f"signal must be one of {', '.join(SIGNALS)}, got {signal!r}")
    topics = _names(payload_path, payload, "topics")
    if not topics:
        _fail(payload_path, "topics is empty — every item is placed, uncategorized-shares included")
    entities = _names(payload_path, payload, "entities")
    facts = _facts(payload_path, payload)
    item = _corpus_item(payload_path, instance, item_id)

    record_pass(ctx, item_id, "digest")
    target = instance.digests_dir / f"{item_id}.md"
    rewrite = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(
        target,
        _serialize(
            item,
            signal=signal,
            topics=topics,
            entities=entities,
            media=item_media(instance, item),
            facts=facts,
        ),
    )
    return f"{'rewrote' if rewrite else 'wrote'} state/digests/{item_id}.md · digest pass recorded"


def _corpus_item(payload_path: Path, instance: Instance, item_id: str) -> corpus.CorpusItem:
    """The corpus item the digest is about: the source of its date and stated media.

    Raises:
        DigestPayloadError: There is no such item, it does not parse, or
            its frontmatter claims an id its filename does not.
    """
    rel = f"corpus/{item_id[:4]}/{item_id}.md"
    path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    if not path.is_file():
        _fail(
            payload_path,
            f"id {item_id!r} is not a corpus item in this instance — there is no {rel}",
        )
    try:
        item = corpus.read_item(path)
    except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError) as e:
        _fail(payload_path, f"{rel} does not parse, so it states no date to digest under: {e}")
    if item.id != item_id:
        # The id keys the corpus, the taxonomy and every citation. Filing
        # the digest under either of two disagreeing ids leaves it
        # unresolvable, so the corpus item is repaired first.
        _fail(payload_path, f"{rel} declares id {item.id!r} — repair the corpus item first")
    return item


def item_media(instance: Instance, item: corpus.CorpusItem) -> list[str]:
    """Every media file the item carries, root-relative, stated paths first.

    The corpus item's own `media:` is only half of it: the media stage
    downloads into ``enrichment/<id>/`` and never writes the frontmatter
    back, so an item whose media arrived that way states none at all.

    THE derivation, shared with the health check's drift advisory: what a
    standing digest states is compared against what this returns, so a
    second reading of the rule would report drift that is only disagreement.
    """
    item_dir = instance.enrichment_dir / item.id
    if not item_dir.is_dir():
        return list(item.media)
    downloaded = sorted(
        (path for path in item_dir.iterdir() if is_media_file(path)), key=lambda path: path.name
    )
    return [*item.media, *(str(path.relative_to(instance.root)) for path in downloaded)]


def _serialize(  # noqa: PLR0913 — one keyword per digest field
    item: corpus.CorpusItem,
    *,
    signal: str,
    topics: list[str],
    entities: list[str],
    media: list[str],
    facts: list[str],
) -> str:
    """The canonical digest text: derived fields off the item, judgment off the payload."""
    lines = [
        "---",
        f"id: {item.id}",
        f"date: {item.date.isoformat()}",
        f"signal: {signal}",
        f"topics: [{', '.join(topics)}]",
    ]
    if entities:
        lines.append(f"entities: [{', '.join(entities)}]")
    if media:
        # Block form, unlike the name lists above: a media path ends in a
        # filed asset's own filename, which may hold a comma.
        lines.append("media:")
        lines.extend(f"  - {path}" for path in media)
    lines.append("---")
    return "\n".join(lines) + "\n" + "".join(f"- {fact}\n" for fact in facts)


# ---------------------------------------------------------------------------
# Payload validation. Every value is written by a session, so none is
# trusted for its type, and the message names the key that broke.
# ---------------------------------------------------------------------------


def _fail(payload_path: Path, message: str) -> NoReturn:
    raise DigestPayloadError(f"{payload_path}: {message}")


def _load(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise DigestPayloadError(f"{path}: invalid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise DigestPayloadError(f"{path}: expected a JSON object, got {type(raw).__name__}")
    missing = sorted(_REQUIRED_KEYS - set(raw))
    if missing:
        _fail(path, f"missing required key(s) {missing}")
    derived = sorted(_DERIVED_KEYS & set(raw))
    if derived:
        _fail(
            path,
            f"{derived} is the engine's to write, not the payload's — the item's own "
            "date and the media it carries are what its digest states",
        )
    unknown = sorted(set(raw) - _REQUIRED_KEYS - _OPTIONAL_KEYS - _DERIVED_KEYS)
    if unknown:
        _fail(path, f"unknown key(s) {unknown}")
    return raw


def _text(path: Path, payload: dict[str, object], key: str) -> str:
    """One required scalar, trimmed — surrounding space is never part of the value."""
    value = payload[key]
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        _fail(path, f"{key} must be a non-empty single-line string, got {value!r}")
    return value.strip()


def _names(path: Path, payload: dict[str, object], key: str) -> list[str]:
    """A list of taxonomy names, trimmed; absent reads as empty."""
    value = payload.get(key, [])
    if not isinstance(value, list):
        _fail(path, f"{key} must be a list of names, got {type(value).__name__}")
    names: list[str] = []
    for i, name in enumerate(value):
        if not isinstance(name, str) or not name.strip() or "\n" in name:
            _fail(path, f"{key}[{i}] must be a non-empty single-line name, got {name!r}")
        if any(char in name for char in ",[]"):
            _fail(path, f"{key}[{i}] may not contain ',', '[' or ']': {name!r}")
        names.append(name.strip())
    return names


def _facts(path: Path, payload: dict[str, object]) -> list[str]:
    """The fact bullets, trimmed — one line each, at least one of them."""
    value = payload["facts"]
    if not isinstance(value, list):
        _fail(path, f"facts must be a list of strings, got {type(value).__name__}")
    if not value:
        _fail(path, "facts is empty — the fact bullets are the one thing a digest exists to hold")
    facts: list[str] = []
    for i, fact in enumerate(value):
        if not isinstance(fact, str) or not fact.strip() or "\n" in fact:
            _fail(path, f"facts[{i}] must be a non-empty single-line string, got {fact!r}")
        facts.append(fact.strip())
    return facts
