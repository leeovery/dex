"""Corpus items: the ONE frontmatter read/write point.

A :class:`CorpusItem` is the typed form of one corpus file. ``parse`` and
``serialize`` round-trip it: the Claude-authored body passes through
byte-exact; frontmatter is re-emitted canonically (same fields, normalized
layout — semantically identical). Structure never passes through freehand
writing, so a malformed item cannot be written.

``kinds`` is deliberately ``list[str]``, not ``list[Kind]``: corpus
frontmatter kinds are provisional forever (the ledger is authoritative),
and migration 1 must parse pre-rename vocabulary (``tweet``/``blog``) in
order to rewrite it.
"""

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import atomic

__all__ = [
    "CorpusItem",
    "CorpusSchemaError",
    "parse",
    "read_item",
    "serialize",
    "write_item",
]


class CorpusSchemaError(ValueError):
    """A corpus item does not conform to the schema (schema.md)."""


ITEM_STATUSES = frozenset({"raw", "enriched"})

# Frontmatter keys, in canonical emission order. Optional keys are omitted
# when empty/absent; presence of an empty list and absence are the same
# semantics (schema.md).
_SCALAR_KEYS = ("id", "source", "channel", "shared_by", "date", "status")
_BLOCK_LIST_KEYS = ("urls", "attachments", "media")  # one `  - value` line each
_FLOW_LIST_KEYS = ("kinds", "enrichment")  # `[a, b]` inline
_REQUIRED_KEYS = frozenset(
    {"id", "source", "channel", "shared_by", "date", "kinds", "status", "enrichment"}
)
_ALL_KEYS = frozenset((*_SCALAR_KEYS, *_BLOCK_LIST_KEYS, *_FLOW_LIST_KEYS, "reactions"))

_KEY_RE = re.compile(r"^[a-z_]+$")


@dataclass(frozen=True, slots=True, kw_only=True)
class CorpusItem:
    """One corpus item: provenance frontmatter plus the verbatim body."""

    id: str
    source: str
    channel: str
    shared_by: str
    date: datetime.date
    urls: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    reactions: int | None = None
    attachments: list[str] = field(default_factory=list)
    status: str = "raw"
    enrichment: list[str] = field(default_factory=list)
    media: list[str] = field(default_factory=list)
    body: str = ""

    def __post_init__(self) -> None:
        for name in ("id", "source", "channel", "shared_by", "status"):
            _validate_scalar(name, getattr(self, name))
        if self.status not in ITEM_STATUSES:
            options = ", ".join(sorted(ITEM_STATUSES))
            raise CorpusSchemaError(f"status must be one of {options}, got {self.status!r}")
        if not self.kinds:
            raise CorpusSchemaError("kinds must name at least one kind")
        if self.reactions is not None and self.reactions < 1:
            raise CorpusSchemaError(f"reactions must be >= 1 when present, got {self.reactions}")
        for name in _BLOCK_LIST_KEYS:
            for value in getattr(self, name):
                _validate_scalar(name, value)
        for name in _FLOW_LIST_KEYS:
            for value in getattr(self, name):
                _validate_scalar(name, value)
                if any(ch in value for ch in ",[]"):
                    raise CorpusSchemaError(
                        f"{name} entries may not contain ',', '[' or ']': {value!r}"
                    )


def _validate_scalar(name: str, value: str) -> None:
    if not value:
        raise CorpusSchemaError(f"{name} must not be empty")
    if "\n" in value:
        raise CorpusSchemaError(f"{name} must be a single line: {value!r}")
    if value != value.strip():
        raise CorpusSchemaError(f"{name} must not have leading/trailing whitespace: {value!r}")


def parse(text: str) -> CorpusItem:
    """Parse a corpus file's full text into a :class:`CorpusItem`.

    The body — everything after the closing ``---`` fence line — is kept
    byte-exact. List fields accept both block (``- value`` lines) and flow
    (``[a, b]``) forms; serialization is canonical.

    Args:
        text: The complete file content.

    Returns:
        The parsed item.

    Raises:
        CorpusSchemaError: The frontmatter is malformed, has unknown or
            duplicate keys, or is missing required keys.
    """
    if not text.startswith("---\n"):
        raise CorpusSchemaError("corpus item must open with a '---' frontmatter fence")
    fence = text.find("\n---\n", 3)
    if fence == -1:
        raise CorpusSchemaError("unterminated frontmatter: no closing '---' fence")
    fm_text = text[4:fence]
    fm_lines = fm_text.split("\n") if fm_text else []
    body = text[fence + 5 :]

    fields = _parse_frontmatter(fm_lines)
    missing = sorted(_REQUIRED_KEYS - set(fields))
    if missing:
        raise CorpusSchemaError(f"missing required frontmatter key(s): {missing}")

    try:
        return CorpusItem(
            id=str(fields["id"]),
            source=str(fields["source"]),
            channel=str(fields["channel"]),
            shared_by=str(fields["shared_by"]),
            date=_parse_date(str(fields["date"])),
            urls=_as_list(fields.get("urls", [])),
            kinds=_as_list(fields["kinds"]),
            reactions=_parse_reactions(fields.get("reactions")),
            attachments=_as_list(fields.get("attachments", [])),
            status=str(fields["status"]),
            enrichment=_as_list(fields["enrichment"]),
            media=_as_list(fields.get("media", [])),
            body=body,
        )
    except CorpusSchemaError:
        raise
    except ValueError as e:
        raise CorpusSchemaError(str(e)) from e


def _parse_frontmatter(lines: list[str]) -> dict[str, str | list[str]]:
    fields: dict[str, str | list[str]] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        key, sep, rest = line.partition(":")
        if not sep or not _KEY_RE.match(key):
            raise CorpusSchemaError(f"not a 'key: value' frontmatter line: {line!r}")
        if key not in _ALL_KEYS:
            raise CorpusSchemaError(
                f"unknown frontmatter key {key!r} — known keys: {sorted(_ALL_KEYS)}"
            )
        if key in fields:
            raise CorpusSchemaError(f"duplicate frontmatter key {key!r}")
        value = rest.strip()
        i += 1
        if key in _BLOCK_LIST_KEYS or key in _FLOW_LIST_KEYS:
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1].strip()
                fields[key] = [part.strip() for part in inner.split(",")] if inner else []
            elif value == "":
                items: list[str] = []
                while i < len(lines) and lines[i].startswith("  - "):
                    items.append(lines[i][4:].strip())
                    i += 1
                fields[key] = items
            else:
                raise CorpusSchemaError(
                    f"{key} must be a flow list ('[a, b]') or block list ('  - a' lines), "
                    f"got {value!r}"
                )
        else:
            if not value:
                raise CorpusSchemaError(f"frontmatter key {key!r} has no value")
            fields[key] = value
    return fields


def _parse_date(value: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as e:
        raise CorpusSchemaError(f"date must be ISO (YYYY-MM-DD), got {value!r}") from e


def _parse_reactions(value: str | list[str] | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, list) or not value.isdigit():
        raise CorpusSchemaError(f"reactions must be a positive integer, got {value!r}")
    return int(value)


def _as_list(value: str | list[str]) -> list[str]:
    if isinstance(value, str):  # a scalar where a list belongs
        raise CorpusSchemaError(f"expected a list, got scalar {value!r}")
    return value


def serialize(item: CorpusItem) -> str:
    """Emit the canonical file text for an item; the body passes through byte-exact."""
    lines = [
        "---",
        f"id: {item.id}",
        f"source: {item.source}",
        f"channel: {item.channel}",
        f"shared_by: {item.shared_by}",
        f"date: {item.date.isoformat()}",
    ]
    if item.urls:
        lines.append("urls:")
        lines.extend(f"  - {url}" for url in item.urls)
    lines.append(f"kinds: [{', '.join(item.kinds)}]")
    if item.attachments:
        lines.append("attachments:")
        lines.extend(f"  - {path}" for path in item.attachments)
    if item.reactions is not None:
        lines.append(f"reactions: {item.reactions}")
    lines.append(f"status: {item.status}")
    lines.append(f"enrichment: [{', '.join(item.enrichment)}]")
    if item.media:
        lines.append("media:")
        lines.extend(f"  - {path}" for path in item.media)
    lines.append("---")
    return "\n".join(lines) + "\n" + item.body


def read_item(path: Path) -> CorpusItem:
    """Read and parse one corpus file.

    Raises:
        CorpusSchemaError: The file does not conform; the message names it.
    """
    try:
        return parse(path.read_text(encoding="utf-8"))
    except CorpusSchemaError as e:
        raise CorpusSchemaError(f"{path}: {e}") from e


def write_item(path: Path, item: CorpusItem) -> None:
    """Serialize and write one corpus file atomically, creating parent directories.

    Same-dir temp file then replace: a crash mid-write must never lose an
    existing item.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, serialize(item))
