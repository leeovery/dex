"""Placement writing from a JSON payload: ``enrich place``.

``state/taxonomy.json`` and ``state/entity-members.json`` were the last
state files a session wrote by hand. Placement stays the session's
judgment — what to file where, when a topic exists at all — but the
judgment arrives as JSON and this module writes both files: sorted names,
sorted unique member ids and aliases, one canonical byte form, so neither
file can come out malformed and rewriting unchanged judgment reproduces
the bytes exactly.

The payload holds up to five operation lists, applied in this order:
``topics`` and ``entities`` upsert definitions (create, or redefine — a
redefined topic keeps its members, and an entity's ``raw`` always folds
its canonical name in), ``place`` adds memberships, ``unplace`` removes
them, ``drop`` removes a whole definition with its memberships. The order
is what makes one payload a complete move: a place may fill a topic the
same payload defines, and a sweep or split is place plus unplace in one
file.

Validation is whole-payload: any refusal — an unknown key, a wrong type,
a name that is not kebab-case, placing into a topic or entity that
neither exists nor is defined earlier in the payload, unplacing what is
not placed, dropping what does not exist — writes nothing and names the
field. Item ids are checked for shape only, never against the corpus:
placement may precede or follow the item's other work, and ghost members
are lint's finding, not this verb's.

Both files are serialized before either write, taxonomy first — it is
the namespace the member file hangs off. The write canonicalizes: only
the documented shape survives (nothing reads anything else), and an
entity holds an ``entity-members.json`` key only while it has members.
A successful write recompiles ``state/map.json`` — membership is a map
input — and a recompile failure rides the summary into the command's
error exit rather than un-reporting writes that already landed.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from dex_engine import atomic, instance_map

from .types import Instance

__all__ = ["PlacementPayloadError", "place"]

_TOP_KEYS = frozenset({"topics", "entities", "place", "unplace", "drop"})

# An item id names one file in the corpus tree — a single path component
# in the one grammar every creator emits. It never touches the filesystem
# here, but a freehand string accepted as an id would sit in a member
# list as a ghost forever, so the shape is refused up front.
_ITEM_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*")

# Topic and entity names define the wikilink namespace, and every reader
# — pages, lint, the map — matches them verbatim.
_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


class PlacementPayloadError(ValueError):
    """A placement payload does not conform to the shape the verb applies."""


@dataclass(slots=True, kw_only=True)
class _Topic:
    """One topic under mutation: description and member ids."""

    description: str
    items: set[str]


@dataclass(slots=True, kw_only=True)
class _Entity:
    """One entity under mutation: kind and folded aliases."""

    kind: str
    aliases: set[str]


@dataclass(slots=True, kw_only=True)
class _State:
    """The two judgment files, loaded mutable — what the operations apply to."""

    topics: dict[str, _Topic]
    entities: dict[str, _Entity]
    members: dict[str, set[str]]


@dataclass(slots=True, kw_only=True)
class _Tally:
    """What actually changed, in application order — the summary's counts.

    ``placed`` counts memberships added, not pairs asked for: adding an id
    a list already holds is idempotent and silent, exactly as re-excluding
    an already-gone item is.
    """

    defined_topics: int = 0
    defined_entities: int = 0
    placed: int = 0
    unplaced: int = 0
    dropped: int = 0

    def __str__(self) -> str:
        return (
            f"{self.defined_topics} topics defined, {self.defined_entities} entities defined, "
            f"{self.placed} placed, {self.unplaced} unplaced, {self.dropped} dropped"
        )


def place(payload_path: Path, *, instance: Instance) -> str:
    """Apply one placement payload to taxonomy and entity-members, then recompile the map.

    On a fresh instance the first run bootstraps both files (and ``state/``
    itself when needed); on any other it rewrites them whole, so the byte
    form is this module's on every path.

    Args:
        payload_path: The JSON payload (conventionally under ``cache/``):
            any of ``topics``, ``entities``, ``place``, ``unplace``,
            ``drop``, applied in that order.
        instance: The instance.

    Returns:
        A one-line confirmation stating what changed.

    Raises:
        PlacementPayloadError: The payload is not a JSON object, a key is
            missing, unknown or the wrong type, a name is not kebab-case,
            or an operation contradicts the state it applies to. Nothing
            is written.
        ValueError: An existing state file is malformed — nothing is
            written; or the state landed and the map did not recompile
            (the message says which, and carries the summary).
        OSError: The payload cannot be read, or a state write failed —
            after the first write landed, the torn pair is lint's finding.
    """
    payload = _load(payload_path)
    state = _loaded(instance)
    tally = _apply(payload_path, payload, state)
    taxonomy_text = _serialized_taxonomy(state)
    members_text = _serialized_members(state)
    instance.state_dir.mkdir(exist_ok=True)
    atomic.write_text(instance.taxonomy_path, taxonomy_text)
    atomic.write_text(instance.entity_members_path, members_text)
    summary = f"wrote state/taxonomy.json + state/entity-members.json ({tally})"
    instance_map.recompile(instance, summary)
    return f"{summary} · map recompiled"


def _loaded(instance: Instance) -> _State:
    """The two judgment files as mutable state; missing files read as empty.

    Raises:
        ValueError: Either file is malformed — the refusal happens here,
            before any operation is judged, with nothing written.
    """
    topics, entities = instance_map.load_taxonomy(instance.taxonomy_path)
    return _State(
        topics={
            name: _Topic(description=topic.description, items=set(topic.items))
            for name, topic in topics.items()
        },
        entities={
            name: _Entity(kind=entity.kind, aliases=set(entity.aliases))
            for name, entity in entities.items()
        },
        members=instance_map.load_entity_members(instance.entity_members_path),
    )


# ---------------------------------------------------------------------------
# The operations, applied in order against the loaded state. Sequential on
# purpose: existence is judged where the operation stands, so a place may
# fill a topic the payload defined two sections up.
# ---------------------------------------------------------------------------


def _apply(path: Path, payload: dict[str, object], state: _State) -> _Tally:
    tally = _Tally()
    tally.defined_topics = _upserts(path, payload, "topics", _upsert_topic, state)
    tally.defined_entities = _upserts(path, payload, "entities", _upsert_entity, state)
    for i, record in enumerate(_records(path, payload, "place")):
        _place(path, f"place[{i}]", record, state, tally)
    for i, record in enumerate(_records(path, payload, "unplace")):
        _unplace(path, f"unplace[{i}]", record, state, tally)
    _drop(path, payload, state, tally)
    return tally


def _upserts(
    path: Path,
    payload: dict[str, object],
    key: str,
    upsert: Callable[[Path, str, dict[str, object], _State], str],
    state: _State,
) -> int:
    defined: set[str] = set()
    for i, record in enumerate(_records(path, payload, key)):
        name = upsert(path, f"{key}[{i}]", record, state)
        if name in defined:
            # Two definitions for one name are two judgments; applying the
            # later silently would discard one of them unseen.
            _fail(path, f"{key}[{i}].name: {name!r} is defined twice in one payload")
        defined.add(name)
    return len(defined)


def _upsert_topic(path: Path, where: str, record: dict[str, object], state: _State) -> str:
    if "items" in record:
        _fail(
            path,
            f"{where}: items is the engine's to derive — membership moves through "
            "place and unplace, never through a definition",
        )
    _known_keys(path, where, record, required=frozenset({"name", "description"}))
    name = _name(path, f"{where}.name", record["name"])
    description = _line(path, f"{where}.description", record["description"])
    existing = state.topics.get(name)
    state.topics[name] = _Topic(
        description=description, items=existing.items if existing else set()
    )
    return name


def _upsert_entity(path: Path, where: str, record: dict[str, object], state: _State) -> str:
    _known_keys(
        path, where, record, required=frozenset({"name", "kind"}), optional=frozenset({"raw"})
    )
    name = _name(path, f"{where}.name", record["name"])
    kind = _line(path, f"{where}.kind", record["kind"])
    raw = record.get("raw", [])
    if not isinstance(raw, list):
        _fail(path, f"{where}.raw must be a list of aliases, got {type(raw).__name__}")
    aliases = {_line(path, f"{where}.raw[{i}]", alias) for i, alias in enumerate(raw)}
    # The convention the existing files follow: `raw` lists everything that
    # folds into the canonical name, the canonical name included.
    state.entities[name] = _Entity(kind=kind, aliases=aliases | {name})
    return name


def _place(path: Path, where: str, record: dict[str, object], state: _State, tally: _Tally) -> None:
    item_id, topics, entities = _membership(path, where, record)
    for name in topics:
        topic = state.topics.get(name)
        if topic is None:
            _fail(
                path,
                f"{where}: topic {name!r} does not exist and this payload does not define it",
            )
        if item_id not in topic.items:
            topic.items.add(item_id)
            tally.placed += 1
    for name in entities:
        if name not in state.entities:
            _fail(
                path,
                f"{where}: entity {name!r} does not exist and this payload does not define it",
            )
        members = state.members.setdefault(name, set())
        if item_id not in members:
            members.add(item_id)
            tally.placed += 1


def _unplace(
    path: Path, where: str, record: dict[str, object], state: _State, tally: _Tally
) -> None:
    item_id, topics, entities = _membership(path, where, record)
    for name in topics:
        topic = state.topics.get(name)
        if topic is None or item_id not in topic.items:
            _fail(path, f"{where}: {item_id} is not placed in topic {name!r}")
        topic.items.remove(item_id)
        tally.unplaced += 1
    for name in entities:
        # Judged against the member file alone, taxonomy or no: what is
        # placed is what can be unplaced, which is how a list left behind
        # by drifted state empties out through the verb.
        members = state.members.get(name)
        if members is None or item_id not in members:
            _fail(path, f"{where}: {item_id} is not a member of entity {name!r}")
        members.remove(item_id)
        tally.unplaced += 1


def _drop(path: Path, payload: dict[str, object], state: _State, tally: _Tally) -> None:
    section = payload.get("drop")
    if section is None:
        return
    if not isinstance(section, dict):
        _fail(path, f"drop must be an object, got {type(section).__name__}")
    _known_keys(
        path, "drop", section, required=frozenset(), optional=frozenset({"topics", "entities"})
    )
    topics = _names(path, "drop.topics", section.get("topics", []))
    entities = _names(path, "drop.entities", section.get("entities", []))
    if not topics and not entities:
        _fail(path, "drop names no topic and no entity — the section asks nothing")
    for name in topics:
        if name == instance_map.UNCATEGORIZED:
            _fail(path, f"drop: {name} is the coverage ledger and is never dropped")
        if name not in state.topics:
            _fail(path, f"drop: topic {name!r} does not exist")
        del state.topics[name]
        tally.dropped += 1
    for name in entities:
        if name not in state.entities:
            _fail(path, f"drop: entity {name!r} does not exist")
        del state.entities[name]
        state.members.pop(name, None)
        tally.dropped += 1


# ---------------------------------------------------------------------------
# Payload validation. Every value is written by a session, so none is
# trusted for its type, and the message names the field that broke.
# ---------------------------------------------------------------------------


def _fail(payload_path: Path, message: str) -> NoReturn:
    raise PlacementPayloadError(f"{payload_path}: {message}")


def _load(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PlacementPayloadError(f"{path}: invalid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise PlacementPayloadError(f"{path}: expected a JSON object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise PlacementPayloadError(f"{path}: unknown key(s) {unknown}")
    if not any(raw.get(key) for key in _TOP_KEYS):
        raise PlacementPayloadError(f"{path}: the payload states no operations")
    return raw


def _records(path: Path, payload: dict[str, object], key: str) -> list[dict[str, object]]:
    """One operation list's records, each an object; absent reads as empty."""
    value = payload.get(key, [])
    if not isinstance(value, list):
        _fail(path, f"{key} must be a list, got {type(value).__name__}")
    for i, record in enumerate(value):
        if not isinstance(record, dict):
            _fail(path, f"{key}[{i}] must be an object, got {type(record).__name__}")
    return value


def _membership(
    path: Path, where: str, record: dict[str, object]
) -> tuple[str, list[str], list[str]]:
    """One place/unplace record parsed: the item id and the names it moves against."""
    _known_keys(
        path, where, record, required=frozenset({"id"}), optional=frozenset({"topics", "entities"})
    )
    item_id = _line(path, f"{where}.id", record["id"])
    if not _ITEM_ID_RE.fullmatch(item_id):
        _fail(path, f"{where}.id: {item_id!r} is not a corpus item id")
    topics = _names(path, f"{where}.topics", record.get("topics", []))
    entities = _names(path, f"{where}.entities", record.get("entities", []))
    if not topics and not entities:
        _fail(path, f"{where} names no topic and no entity — the record asks nothing")
    return item_id, topics, entities


def _known_keys(
    path: Path,
    where: str,
    record: dict[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    missing = sorted(required - set(record))
    if missing:
        _fail(path, f"{where}: missing required key(s) {missing}")
    unknown = sorted(set(record) - required - optional)
    if unknown:
        _fail(path, f"{where}: unknown key(s) {unknown}")


def _names(path: Path, where: str, value: object) -> list[str]:
    if not isinstance(value, list):
        _fail(path, f"{where} must be a list of names, got {type(value).__name__}")
    return [_name(path, f"{where}[{i}]", name) for i, name in enumerate(value)]


def _name(path: Path, where: str, value: object) -> str:
    name = _line(path, where, value)
    if not _NAME_RE.fullmatch(name):
        _fail(
            path,
            f"{where}: {name!r} is not kebab-case — topic and entity names define the "
            "wikilink namespace: lowercase a-z0-9 runs joined by single hyphens",
        )
    return name


def _line(path: Path, where: str, value: object) -> str:
    """One string value, trimmed — surrounding space is never part of the value."""
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        _fail(path, f"{where} must be a non-empty single-line string, got {value!r}")
    return value.strip()


# ---------------------------------------------------------------------------
# Serialization: the one canonical byte form for each file
# ---------------------------------------------------------------------------


def _serialized_taxonomy(state: _State) -> str:
    payload: dict[str, object] = {
        "topics": {
            name: {"description": topic.description, "items": sorted(topic.items)}
            for name, topic in sorted(state.topics.items())
        },
        "entities": {
            name: {"kind": entity.kind, "raw": sorted(entity.aliases)}
            for name, entity in sorted(state.entities.items())
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _serialized_members(state: _State) -> str:
    # An entity holds a key only while it has members: an emptied list's
    # key goes with the last id, and absent already reads as empty
    # everywhere the file is consumed.
    payload = {name: sorted(ids) for name, ids in sorted(state.members.items()) if ids}
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
