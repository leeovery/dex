"""dex-map: compile the instance map — ``state/map.json``, cached and mechanical.

One compile reads ``state/taxonomy.json``, ``state/entity-members.json``,
the corpus and the wiki, and writes the one cached artifact that answers
"what does this instance hold" without call-time derivation:

  topics — every taxonomy topic except ``uncategorized-shares`` (a ledger,
  not a topic): description, sorted member ids, count, whether a wiki page
  of that name exists, and the newest member's share date (the corpus
  ``date:``; null when no member's file resolves).

  entities — every taxonomy entity: kind, folded aliases, members from
  ``state/entity-members.json``, count, has-page.

  graph — typed edges between taxonomy names; the nodes are implicit (the
  topics and entities above, so ``uncategorized-shares`` is off the graph
  too). ``wikilink`` is directed: page A's body links ``[[B]]``, kept only
  when both endpoints are taxonomy names. ``shared-items`` is undirected:
  topic↔topic and entity↔topic membership intersections, weighted,
  zero-weight pairs omitted, each pair stored once with the smaller name
  first.

The output is deterministic to the byte — sorted names, sorted member ids,
a fixed edge order — so recompiling unchanged inputs reproduces the file
exactly, which is what lets a freshness check diff a recompile against
what is on disk.

A missing taxonomy or entity-members file compiles an honestly empty map:
a young instance has a map, truthfully empty. A malformed one is refused
loudly with nothing written — a map compiled from a file that cannot be
honestly read would state the corpus wrong with nothing saying so. An
individual corpus or wiki file that will not read or parse contributes
nothing: lint is what reports broken files.
"""

import argparse
import datetime
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import atomic, corpus, frontmatter, wikitext
from .pipeline.types import Instance

__all__ = ["CompiledMap", "build_parser", "compile_map", "main", "serialize_map", "write_map"]

# A ledger for low-signal items, not a classification — it is a taxonomy
# key for the coverage invariant's sake, and no map consumer's topic.
UNCATEGORIZED = "uncategorized-shares"


@dataclass(frozen=True, slots=True, kw_only=True)
class CompiledMap:
    """One compiled map: the JSON payload, and the counts the summary states."""

    payload: dict[str, object]
    topics: int
    entities: int
    edges: int


@dataclass(frozen=True, slots=True, kw_only=True)
class _Topic:
    """One taxonomy topic, validated: description and sorted unique member ids."""

    description: str
    items: list[str]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Entity:
    """One taxonomy entity, validated: kind and sorted unique aliases."""

    kind: str
    aliases: list[str]


def compile_map(instance: Instance) -> CompiledMap:
    """Compile the map from taxonomy, entity-members, corpus and wiki.

    Args:
        instance: The instance.

    Returns:
        The compiled map, unwritten.

    Raises:
        ValueError: ``state/taxonomy.json`` or ``state/entity-members.json``
            is malformed — invalid JSON, or a shape the documented format
            forbids. The two judgment files are the only inputs that can
            refuse; broken corpus and wiki files contribute nothing.
    """
    topics, entities = _taxonomy(instance.taxonomy_path)
    members = _entity_members(instance.state_dir / "entity-members.json")
    pages = _pages(instance)
    dates = _share_dates(instance)
    topic_items = {
        name: set(topic.items) for name, topic in topics.items() if name != UNCATEGORIZED
    }
    # Namespace custody is the taxonomy's: an entity is a node because the
    # taxonomy names it, and an entity-members key the taxonomy does not
    # name is drift for lint, never a node here.
    entity_items = {name: members.get(name, set()) for name in sorted(entities)}
    graph = _graph(topic_items, entity_items, pages)
    payload: dict[str, object] = {
        "topics": {
            name: {
                "description": topics[name].description,
                "items": topics[name].items,
                "count": len(topics[name].items),
                "has_page": name in pages,
                "newest": _newest(topics[name].items, dates),
            }
            for name in sorted(topic_items)
        },
        "entities": {
            name: {
                "kind": entities[name].kind,
                "aliases": entities[name].aliases,
                "items": sorted(entity_items[name]),
                "count": len(entity_items[name]),
                "has_page": name in pages,
            }
            for name in sorted(entities)
        },
        "graph": graph,
    }
    return CompiledMap(
        payload=payload, topics=len(topic_items), entities=len(entities), edges=len(graph)
    )


def serialize_map(payload: dict[str, object]) -> str:
    """The map's one canonical byte form — what ``state/map.json`` holds."""
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def write_map(instance: Instance) -> CompiledMap:
    """Compile and write ``state/map.json`` atomically.

    Args:
        instance: The instance.

    Returns:
        The compiled map, as written.

    Raises:
        ValueError: A malformed input file (:func:`compile_map`); nothing
            is written, and an existing map stands untouched.
    """
    compiled = compile_map(instance)
    instance.map_path.parent.mkdir(exist_ok=True)
    atomic.write_text(instance.map_path, serialize_map(compiled.payload))
    return compiled


# ---------------------------------------------------------------------------
# The judgment inputs: taxonomy and entity-members, validated loudly
# ---------------------------------------------------------------------------


def _taxonomy(path: Path) -> tuple[dict[str, _Topic], dict[str, _Entity]]:
    """The parsed topics and entities, both empty when the file is missing.

    Absence and wrong type part ways here: a missing key is a young or
    sparse file and reads as empty, a wrong-typed value is a malformed one
    and refuses — silently defaulting it would compile a map that states
    memberships the file never held.

    Raises:
        ValueError: Invalid JSON, or a wrong-typed value anywhere in the
            documented shape; the message names the file and the spot.
    """
    raw = _json_object(path)
    if raw is None:
        return {}, {}
    topics = {
        name: _topic(path, name, value)
        for name, value in _object_field(path, raw, "topics").items()
    }
    entities = {
        name: _entity(path, name, value)
        for name, value in _object_field(path, raw, "entities").items()
    }
    return topics, entities


def _topic(path: Path, name: str, value: object) -> _Topic:
    if not isinstance(value, dict):
        raise ValueError(
            f"{path.name}: topic {name!r} must be an object, got {type(value).__name__}"
        )
    return _Topic(
        description=_str_field(path, f"topic {name!r}", value, "description"),
        items=sorted(set(_str_list_field(path, f"topic {name!r}", value, "items"))),
    )


def _entity(path: Path, name: str, value: object) -> _Entity:
    if not isinstance(value, dict):
        raise ValueError(
            f"{path.name}: entity {name!r} must be an object, got {type(value).__name__}"
        )
    return _Entity(
        kind=_str_field(path, f"entity {name!r}", value, "kind"),
        aliases=sorted(set(_str_list_field(path, f"entity {name!r}", value, "raw"))),
    )


def _entity_members(path: Path) -> dict[str, set[str]]:
    """Entity name -> member item ids; empty when the file is missing.

    The whole documented shape — an object of string -> list of item-id
    strings — is validated, so a wrong-typed value refuses rather than
    compiling an entity whose members were never there.

    Raises:
        ValueError: Invalid JSON, or a value that is not a list of strings.
    """
    raw = _json_object(path)
    if raw is None:
        return {}
    members: dict[str, set[str]] = {}
    for name, ids in raw.items():
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError(f"{path.name}: entity {name!r} must map to a list of item-id strings")
        members[name] = set(ids)
    return members


def _json_object(path: Path) -> dict[str, object] | None:
    """The file parsed as a JSON object; None when it is missing.

    Raises:
        ValueError: The file exists but is not a JSON object.
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path.name}: invalid JSON ({' '.join(str(e).split())})") from e
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: expected a JSON object, got {type(raw).__name__}")
    return raw


def _object_field(path: Path, obj: dict[str, object], key: str) -> dict[str, object]:
    value = obj.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}: {key} must be an object, got {type(value).__name__}")
    return value


def _str_field(path: Path, where: str, obj: dict[str, object], key: str) -> str:
    value = obj.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{path.name}: {where} {key} must be a string, got {type(value).__name__}")
    return value


def _str_list_field(path: Path, where: str, obj: dict[str, object], key: str) -> list[str]:
    value = obj.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{path.name}: {where} {key} must be a list of strings")
    return value


# ---------------------------------------------------------------------------
# The mechanical inputs: corpus dates and wiki pages, read tolerantly
# ---------------------------------------------------------------------------


def _share_dates(instance: Instance) -> dict[str, datetime.date]:
    """Item id -> share date, for every corpus file that reads and parses.

    The filename stems the whole system keys on, never the frontmatter
    ``id:``. One pass over the corpus, shared by every topic's newest-date
    reading — an item filed in three topics is parsed once, not three times.
    """
    dates: dict[str, datetime.date] = {}
    for path in sorted(instance.corpus_dir.glob("*/*.md")):
        try:
            item = corpus.read_item(path)
        except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError):
            continue
        dates[path.stem] = item.date
    return dates


def _newest(item_ids: list[str], dates: dict[str, datetime.date]) -> str | None:
    """The newest member's share date, ISO; None when no member's file answers."""
    stamped = [dates[item_id] for item_id in item_ids if item_id in dates]
    return max(stamped).isoformat() if stamped else None


def _pages(instance: Instance) -> dict[str, Path]:
    """Every wiki page by stem — the has-page answer, and where edges read from.

    Anywhere under ``wiki/``, sorted so a stem held by two files (lint's
    finding, not this one's) resolves to the same file on every compile.
    """
    pages: dict[str, Path] = {}
    for path in sorted(instance.wiki_dir.rglob("*.md")):
        pages.setdefault(path.stem, path)
    return pages


# ---------------------------------------------------------------------------
# The graph: compile-time inversion of files already on disk
# ---------------------------------------------------------------------------


def _graph(
    topic_items: dict[str, set[str]],
    entity_items: dict[str, set[str]],
    pages: dict[str, Path],
) -> list[dict[str, object]]:
    """Every typed edge: wikilink first (the curated relation), then shared-items."""
    nodes = set(topic_items) | set(entity_items)
    edges: list[dict[str, object]] = [
        {"type": "wikilink", "source": source, "target": target}
        for source, target in _wikilink_edges(nodes, pages)
    ]
    edges += [
        {"type": "shared-items", "source": source, "target": target, "weight": weight}
        for source, target, weight in _shared_edges(topic_items, entity_items)
    ]
    return edges


def _wikilink_edges(nodes: set[str], pages: dict[str, Path]) -> list[tuple[str, str]]:
    """Directed (source, target) pairs: a node's page body links another node.

    Bodies only — page frontmatter is ``generated:`` bookkeeping, and a
    link in it is not the page saying anything. A target outside the
    taxonomy (a synthesis, an unbuilt name) is the wiki's business, not an
    edge; a self-link relates nothing.
    """
    edges: set[tuple[str, str]] = set()
    for name in sorted(nodes & pages.keys()):
        try:
            text = pages[name].read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        edges |= {
            (name, target)
            for target in wikitext.wikilinks(frontmatter.body(text))
            if target in nodes and target != name
        }
    return sorted(edges)


def _shared_edges(
    topic_items: dict[str, set[str]], entity_items: dict[str, set[str]]
) -> list[tuple[str, str, int]]:
    """Undirected (source, target, weight) triples: how many members two names share.

    Topic↔topic and entity↔topic pairs only, each stored once with the
    smaller name first; a zero intersection is no edge.
    """
    weights: dict[tuple[str, str], int] = {}
    topics = sorted(topic_items)
    for i, a in enumerate(topics):
        for b in topics[i + 1 :]:
            weight = len(topic_items[a] & topic_items[b])
            if weight:
                weights[a, b] = weight
    for entity, members in entity_items.items():
        for topic, items in topic_items.items():
            if entity == topic:
                continue
            weight = len(members & items)
            if weight:
                weights[min(entity, topic), max(entity, topic)] = weight
    return sorted((a, b, weight) for (a, b), weight in weights.items())


# ---------------------------------------------------------------------------
# CLI — parse, build, call; zero business logic
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-map."""
    return argparse.ArgumentParser(
        prog="dex-map",
        description="Compile the instance map — topics, entities and their relation "
        "graph — into state/map.json.",
    )


def main(argv: list[str] | None = None) -> None:
    """Parse, build the Instance, compile, write, print the summary."""
    build_parser().parse_args(argv)
    instance = Instance(root=Path.cwd())
    try:
        compiled = write_map(instance)
    except (OSError, ValueError) as e:
        sys.exit(f"dex-map: {e}")
    sys.stdout.write(
        f"map compiled: {compiled.topics} topics, {compiled.entities} entities, "
        f"{compiled.edges} edges -> state/map.json\n"
    )


if __name__ == "__main__":
    main()
