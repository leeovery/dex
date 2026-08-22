"""dex-lint: the mechanical health check. Judgment repairs are the session's job.

Checks:

  wiki — broken wikilinks (vs reserved/unbuilt), citations of ids not in the
  corpus, shortid-shaped citations (backticked 6-hex is a probable malformed
  citation everywhere, index included), coverage orphans, index consistency,
  stale pages, page item-count drift (frontmatter ``items:`` vs the page's
  MEMBER count — its taxonomy topic's items, or its entity-members list;
  never its citation count), and difflib sentence similarity ("possible restated
  fact — merge?").

  state — ledger schema validation (via ``ledger.load``), ledger↔tree
  referential integrity (items with no corpus file, one row per finding
  with its entry count — excluded-on-record told apart from renamed and
  from unclaimed; ``done`` entries whose output path is missing on disk),
  waiting cohorts and cognitive-job
  summary, harvest passes recorded under old rules, and the
  enrichment-newer-than-digest orphan listing (the interrupted-session
  backstop, shared with ``enrich status``).

``--write`` reconciles derived wiki frontmatter mechanically: ``items:``
counts are set to the derived member count, and a page that cites items
but carries no ``generated:`` date gains one (today) so staleness has a
baseline. Existing ``generated:`` dates are never rewritten — they are the
staleness reference, and resetting them would mask exactly what the stale
check exists to find.

Output renders through the ``health-report`` surface. Exit 1 on hard
failures: broken wikilinks, bad citations, or a ledger schema error.
"""

import argparse
import datetime
import difflib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .capabilities import Capabilities
from .pipeline import ledger
from .pipeline.ownership import corpus_owners
from .pipeline.registry import DRIVERS
from .pipeline.run import HARVEST_RULES_VERSION, digest_orphans
from .pipeline.types import Config, Format, Instance, LedgerEntry, Need, Status
from .render import surfaces

__all__ = ["LintOutcome", "build_parser", "main", "run_lint"]

ID_RE = re.compile(r"`(\d{4}-\d{2}-\d{2}-[a-z0-9-]+-[0-9a-f]{6})`")
LINK_RE = re.compile(r"\[\[([a-z0-9-]+)\]\]")
# A backticked bare 6-hex token: shortid-shaped, a probable malformed
# citation anywhere it appears — full ids never match this.
SHORTID_RE = re.compile(r"`([0-9a-f]{6})`")
# Frontmatter fields — matched inside the frontmatter block ONLY, never
# against page bodies (a body line reading `items: 99` is prose).
GENERATED_RE = re.compile(r"^generated: (\d{4}-\d{2}-\d{2})$", re.MULTILINE)
TOPIC_RE = re.compile(r"^topic: (.+)$", re.MULTILINE)
ENTITY_RE = re.compile(r"^entity: (.+)$", re.MULTILINE)
ITEMS_RE = re.compile(r"^items: (\d+)$", re.MULTILINE)

# Same-page sentence similarity: difflib, no models. Sentences shorter
# than the floor are boilerplate-prone; the ratio is SequenceMatcher's.
RESTATED_RATIO = 0.85
RESTATED_MIN_CHARS = 40

IsCognitive = Callable[[Need, Format | None], bool]


@dataclass(frozen=True, slots=True, kw_only=True)
class LintOutcome:
    """One lint run: the rendered report and the process exit code."""

    report: str
    exit_code: int


@dataclass(slots=True, kw_only=True)
class _WikiScan:
    """What one pass over the wiki pages collected."""

    cited: set[str] = field(default_factory=set)
    broken_links: list[dict[str, str]] = field(default_factory=list)
    reserved_links: int = 0
    bad_citations: list[dict[str, str]] = field(default_factory=list)
    shortid_citations: list[dict[str, str]] = field(default_factory=list)
    stale_pages: list[dict[str, object]] = field(default_factory=list)
    count_drift: list[dict[str, object]] = field(default_factory=list)
    restated: list[dict[str, str]] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def run_lint(
    instance: Instance,
    *,
    is_cognitive: IsCognitive,
    today: Callable[[], datetime.date],
    write: bool = False,
) -> LintOutcome:
    """Run every mechanical check against the instance.

    Args:
        instance: The instance.
        is_cognitive: Whether a waiting need resolves to the
            cognitive floor (wired to ``Capabilities.is_cognitive`` by the
            CLI; faked in tests).
        today: Injected clock — stamps ``--write``'s repaired
            ``generated:`` dates.
        write: Reconcile derived wiki frontmatter (``items:`` counts,
            missing ``generated:`` dates).

    Returns:
        The outcome: rendered report and exit code.
    """
    special = _pre_taxonomy_outcome(instance)
    if special is not None:
        return special
    taxonomy, taxonomy_error = _load_taxonomy(instance)
    entity_members = _entity_members(instance)
    corpus_ids = {path.stem for path in instance.corpus_dir.glob("*/*.md")}
    pages = _pages(instance)
    scan = _scan_wiki(
        pages, taxonomy, entity_members, corpus_ids, write=write, today=today
    )

    ledgered = _uncategorized_items(taxonomy)
    orphans = sorted(corpus_ids - scan.cited - ledgered)
    unindexed, ghost_index = _index_consistency(instance, pages, taxonomy)
    # Shortid-shaped citations flag everywhere — index included: latent
    # shortids in an index never tripped the old citation check because no
    # page existed to fail against.
    index_path = instance.root / "wiki" / "index.md"
    if index_path.exists():
        scan.shortid_citations += [
            {"page": "index", "token": token}
            for token in sorted(set(SHORTID_RE.findall(index_path.read_text(encoding="utf-8"))))
        ]

    payload: dict[str, object] = {
        "summary": {
            "corpus_items": len(corpus_ids),
            "pages": len(pages),
            "cited": len(scan.cited),
        },
        "broken_links": scan.broken_links,
        "reserved_links": scan.reserved_links,
        "bad_citations": scan.bad_citations,
        "shortid_citations": scan.shortid_citations,
        "orphans": orphans,
        "unindexed": unindexed,
        "ghost_index": ghost_index,
        "stale_pages": scan.stale_pages,
        "count_drift": scan.count_drift,
        "restated": scan.restated,
    }
    if taxonomy_error is not None:
        payload["taxonomy_error"] = taxonomy_error
    ledger_error = _state_checks(instance, payload, is_cognitive, corpus_ids)
    if scan.reconciled:
        payload["reconciled"] = scan.reconciled
    if scan.notes:
        payload["notes"] = scan.notes

    failed = bool(
        scan.broken_links or scan.bad_citations or ledger_error or taxonomy_error
    )
    return LintOutcome(
        report=surfaces.render("health-report", payload),
        exit_code=1 if failed else 0,
    )


def _load_taxonomy(instance: Instance) -> tuple[dict[str, object], str | None]:
    """The parsed taxonomy, or an empty one plus the finding when it is broken.

    Lint's job is reporting broken state files, so a malformed
    ``state/taxonomy.json`` becomes a loud finding — never a crash; the
    wiki checks still run, against the empty taxonomy.
    """
    path = instance.state_dir / "taxonomy.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {}, f"{path.name}: invalid JSON ({' '.join(str(e).split())})"
    if not isinstance(raw, dict):
        return {}, f"{path.name}: expected a JSON object, got {type(raw).__name__}"
    return raw, None


def _uncategorized_items(taxonomy: dict[str, object]) -> set[str]:
    """The uncategorized-shares ledger's item ids, tolerating malformed shapes."""
    topics = taxonomy.get("topics", {})
    uncategorized = (
        topics.get("uncategorized-shares", {}) if isinstance(topics, dict) else {}
    )
    items = uncategorized.get("items", []) if isinstance(uncategorized, dict) else []
    if not isinstance(items, list):
        return set()
    return {item for item in items if isinstance(item, str)}


def _pre_taxonomy_outcome(instance: Instance) -> LintOutcome | None:
    """The two pre-taxonomy states: fresh instance, or broken mid-ingest."""
    if (instance.state_dir / "taxonomy.json").exists():
        return None
    stranded = sorted(path.stem for path in instance.corpus_dir.glob("*/*.md"))
    if stranded:
        head = (
            f"broken mid-ingest: {len(stranded)} corpus item(s) but no "
            "state/taxonomy.json — placement never ran:"
        )
        lines = [head, *(f"  - {item}" for item in stranded)]
        return LintOutcome(report="\n".join(lines) + "\n", exit_code=1)
    return LintOutcome(
        report="fresh instance: no state/taxonomy.json yet — nothing to lint.\n",
        exit_code=0,
    )


def _pages(instance: Instance) -> dict[str, Path]:
    wiki = instance.root / "wiki"
    pages: dict[str, Path] = {}
    for group in ("topics", "syntheses", "entities"):
        pages |= {path.stem: path for path in wiki.glob(f"{group}/*.md")}
    return pages


# ---------------------------------------------------------------------------
# Wiki checks
# ---------------------------------------------------------------------------


def _entity_members(instance: Instance) -> dict[str, list[str]]:
    path = instance.state_dir / "entity-members.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected an object of entity -> item list")
    return {
        name: members for name, members in raw.items() if isinstance(members, list)
    }


def _frontmatter(text: str) -> str | None:
    """The frontmatter block's inner text, or None without a complete fence."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end == -1:
        return None
    return text[4:end]


def _scan_wiki(  # noqa: PLR0913 — one argument per data source the scan reads
    pages: dict[str, Path],
    taxonomy: dict[str, object],
    entity_members: dict[str, list[str]],
    corpus_ids: set[str],
    *,
    write: bool,
    today: Callable[[], datetime.date],
) -> _WikiScan:
    topics = taxonomy.get("topics", {})
    entities = taxonomy.get("entities", {})
    valid_targets = (
        set(topics if isinstance(topics, dict) else ())
        | set(entities if isinstance(entities, dict) else ())
        | set(pages)
    )
    scan = _WikiScan()
    for name, path in sorted(pages.items()):
        text = path.read_text(encoding="utf-8")
        _scan_links(scan, name, text, pages, valid_targets)
        ids = set(ID_RE.findall(text))
        scan.cited |= ids & corpus_ids
        scan.bad_citations += [
            {"page": name, "id": item_id} for item_id in sorted(ids - corpus_ids)
        ]
        scan.shortid_citations += [
            {"page": name, "token": token} for token in sorted(set(SHORTID_RE.findall(text)))
        ]
        frontmatter = _frontmatter(text)
        _scan_staleness(scan, name, frontmatter, topics if isinstance(topics, dict) else {})
        expected = _expected_members(
            frontmatter, topics if isinstance(topics, dict) else {}, entity_members
        )
        text = _scan_counts(
            scan, name, path, text, expected=expected, cites=len(ids), write=write, today=today
        )
        scan.restated += _restated_pairs(name, text)
    return scan


def _expected_members(
    frontmatter: str | None,
    topics: dict[str, object],
    entity_members: dict[str, list[str]],
) -> int | None:
    """The member count the page's ``items:`` field records, or None.

    ``items:`` counts MEMBERS, not citations — the wild convention: a topic
    page records its taxonomy topic's item count, an entity page its
    ``entity-members.json`` list length (a page routinely cites fewer items
    than its topic holds). None = underivable (no resolvable topic/entity):
    the check skips rather than inventing an expectation.
    """
    if frontmatter is None:
        return None
    topic = TOPIC_RE.search(frontmatter)
    if topic and topic.group(1) in topics:
        entry = topics[topic.group(1)]
        items = entry.get("items", []) if isinstance(entry, dict) else []
        return len(items) if isinstance(items, list) else None
    entity = ENTITY_RE.search(frontmatter)
    if entity and entity.group(1) in entity_members:
        return len(entity_members[entity.group(1)])
    return None


def _scan_links(
    scan: _WikiScan,
    name: str,
    text: str,
    pages: dict[str, Path],
    valid_targets: set[str],
) -> None:
    for target in sorted(set(LINK_RE.findall(text))):
        if target in pages:
            continue
        if target in valid_targets:
            scan.reserved_links += 1
        else:
            scan.broken_links.append({"page": name, "target": target})


def _scan_staleness(
    scan: _WikiScan, name: str, frontmatter: str | None, topics: dict[str, object]
) -> None:
    if frontmatter is None:
        return
    generated = GENERATED_RE.search(frontmatter)
    topic = TOPIC_RE.search(frontmatter)
    if not generated or not topic or topic.group(1) not in topics:
        return
    members = topics[topic.group(1)]
    items = members.get("items", []) if isinstance(members, dict) else []
    if not isinstance(items, list):
        items = []
    newer = sum(
        1 for item_id in items if isinstance(item_id, str) and item_id[:10] > generated.group(1)
    )
    if newer:
        scan.stale_pages.append({"page": name, "newer": newer})


def _scan_counts(  # noqa: PLR0913 — the reconcile touches scan, page identity, text, both counts, and both seams
    scan: _WikiScan,
    name: str,
    path: Path,
    text: str,
    *,
    expected: int | None,
    cites: int,
    write: bool,
    today: Callable[[], datetime.date],
) -> str:
    """The item-count consistency check, with the ``--write`` reconcile.

    ``items:`` records the page's MEMBER count (taxonomy topic / entity
    members), never its citation count. Only the frontmatter block is read
    or rewritten. Returns the (possibly rewritten) page text so downstream
    checks see what is now on disk.
    """
    frontmatter = _frontmatter(text)
    if frontmatter is None:
        if cites:
            # No complete fence → nothing derivable can be maintained; a
            # rewrite here would be guessing at structure, so it is a note
            # even under --write — never a claimed repair.
            scan.notes.append(
                f"{name}: cites items but has no complete frontmatter fence — "
                "items:/generated: cannot be maintained"
            )
        return text
    updated = frontmatter
    recorded = ITEMS_RE.search(frontmatter)
    if (
        recorded is not None
        and expected is not None
        and int(recorded.group(1)) != expected
    ):
        scan.count_drift.append(
            {"page": name, "recorded": recorded.group(1), "actual": expected}
        )
        if write:
            updated = ITEMS_RE.sub(f"items: {expected}", updated, count=1)
            scan.reconciled.append(f"{name}: items: {recorded.group(1)} -> {expected}")
    if cites and GENERATED_RE.search(frontmatter) is None:
        if write:
            stamp = f"generated: {today().isoformat()}"
            updated = updated + f"\n{stamp}"
            scan.reconciled.append(f"{name}: {stamp} added (was missing)")
        else:
            scan.notes.append(
                f"{name}: cites items but has no generated: date — staleness cannot "
                "be computed (lint --write adds one)"
            )
    if updated != frontmatter:
        fence_end = text.find("\n---\n", 3)
        text = "---\n" + updated + text[fence_end:]
        path.write_text(text, encoding="utf-8")
    return text


# -- restated facts: difflib, no models --------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_MAX_SENTENCES = 200  # n² guard; a page this big has bigger problems


def _restated_pairs(name: str, text: str) -> list[dict[str, str]]:
    sentences = _sentences(text)
    if len(sentences) > _MAX_SENTENCES:
        return []
    pairs: list[dict[str, str]] = []
    for i, (raw_a, norm_a) in enumerate(sentences):
        for raw_b, norm_b in sentences[i + 1 :]:
            matcher = difflib.SequenceMatcher(None, norm_a, norm_b)
            if (
                matcher.real_quick_ratio() >= RESTATED_RATIO
                and matcher.quick_ratio() >= RESTATED_RATIO
                and matcher.ratio() >= RESTATED_RATIO
            ):
                pairs.append(
                    {"page": name, "first": _clip(raw_a), "second": _clip(raw_b)}
                )
    return pairs


def _sentences(text: str) -> list[tuple[str, str]]:
    """Prose sentences with their comparison form (citations/links stripped).

    Citations are stripped before comparing: the same fact cited from two
    sources is exactly the restatement the check exists to catch — the standing
    rule is add-the-citation, not write-a-new-sentence.
    """
    _, _, body = text.partition("\n---\n")
    prose: list[str] = []
    in_fence = False
    for line in (body or text).split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped or stripped.startswith("#"):
            continue
        prose.append(stripped.removeprefix("- "))
    out: list[tuple[str, str]] = []
    for sentence in _SENTENCE_SPLIT.split(" ".join(prose)):
        raw = sentence.strip()
        norm = ID_RE.sub("", raw)
        norm = SHORTID_RE.sub("", norm)
        norm = LINK_RE.sub(lambda m: m.group(1), norm)
        norm = " ".join(norm.split())
        if len(norm) >= RESTATED_MIN_CHARS:
            out.append((raw, norm))
    return out


def _clip(sentence: str, limit: int = 120) -> str:
    flat = " ".join(sentence.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _index_consistency(
    instance: Instance, pages: dict[str, Path], taxonomy: dict[str, object]
) -> tuple[list[str], list[str]]:
    index_path = instance.root / "wiki" / "index.md"
    index = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    unindexed = sorted(name for name in pages if f"[[{name}]]" not in index)
    topics = taxonomy.get("topics", {})
    entities = taxonomy.get("entities", {})
    valid_targets = (
        set(topics if isinstance(topics, dict) else ())
        | set(entities if isinstance(entities, dict) else ())
        | set(pages)
    )
    ghost = sorted(
        target
        for target in set(LINK_RE.findall(index))
        if target not in pages and target not in valid_targets
    )
    return unindexed, ghost


# ---------------------------------------------------------------------------
# State checks
# ---------------------------------------------------------------------------


def _state_checks(
    instance: Instance,
    payload: dict[str, object],
    is_cognitive: IsCognitive,
    corpus_ids: set[str],
) -> bool:
    """Fill the payload's state sections; True when the ledger failed to load."""
    try:
        entries = ledger.load(instance.ledger_path)
    except ledger.LedgerSchemaError as e:
        payload["ledger_error"] = " ".join(str(e).split())
        entries = None
    if entries is not None:
        payload["ledger_entries"] = len(entries)
        waiting: dict[str, int] = {}
        cognitive: list[dict[str, str]] = []
        for entry in entries.values():
            if entry.status is not Status.WAITING or entry.needs is None:
                continue
            waiting[entry.needs.value] = waiting.get(entry.needs.value, 0) + 1
            if is_cognitive(entry.needs, entry.format):
                cognitive.append(
                    {"item": entry.item, "url": entry.url, "need": entry.needs.value}
                )
        payload["waiting"] = waiting
        payload["cognitive"] = cognitive
        ghost, missing = _referential_integrity(instance, entries, corpus_ids)
        payload["ghost_items"] = ghost
        payload["missing_outputs"] = missing
    payload["stale_passes"] = _stale_passes(instance)
    payload["digest_orphans"] = digest_orphans(instance)
    return entries is None


EXCLUDED_ON_RECORD = "excluded on record"
_ITEM_UNCLAIMED = "no exclusions.tsv record, and no live corpus item lists this work"


def _referential_integrity(
    instance: Instance,
    entries: dict[str, LedgerEntry],
    corpus_ids: set[str],
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    """The ledger's two pointers into the tree: item ids, and output paths.

    Schema validity says nothing about whether a line points at anything
    that exists. An entry naming an id with no corpus file is residue: a
    purge made before ``dex exclude`` swept the ledger, an item removed by
    hand, or work another live item still claims and ``exclude`` therefore
    kept. Worth seeing, and worth telling apart — an item RENAMED since the
    line was written has a live item under a new id claiming its work,
    which reads nothing like an item nothing claims at all. A ``done``
    entry whose output file is gone is the enrichment claiming work whose
    product no longer exists. The two are asked independently — an item
    purged by ``dex exclude`` answers both, and each finding is still true.

    Ghost rows are one per (item, finding): an item named by ten entries
    for one reason is one row carrying the count, not ten rows a reader
    cannot tell apart.
    """
    excluded = _excluded_items(instance)
    dead = [entry for entry in entries.values() if entry.item not in corpus_ids]
    owners = corpus_owners(instance.root, DRIVERS) if dead else {}
    counts: dict[tuple[str, str], int] = {}
    for entry in dead:
        owner = owners.get(entry.hash)
        if owner is not None:
            why = f"renamed — {owner} lists this work"
        elif entry.item in excluded:
            why = EXCLUDED_ON_RECORD
        else:
            why = _ITEM_UNCLAIMED
        counts[(entry.item, why)] = counts.get((entry.item, why), 0) + 1
    ghost: list[dict[str, object]] = [
        {"item": item, "why": why, "entries": count}
        for (item, why), count in sorted(counts.items())
    ]
    missing: list[dict[str, str]] = [
        {"item": entry.item, "path": entry.path}
        for entry in entries.values()
        if entry.path is not None and not (instance.root / entry.path).exists()
    ]
    missing.sort(key=lambda row: (row["item"], row["path"]))
    return ghost, missing


def _excluded_items(instance: Instance) -> set[str]:
    """Item ids in ``state/exclusions.tsv`` — the on-the-record purges."""
    path = instance.state_dir / "exclusions.tsv"
    if not path.exists():
        return set()
    return {
        line.split("\t")[0].strip()
        for line in path.read_text(encoding="utf-8").split("\n")
        if line.strip()
    }


def _stale_passes(instance: Instance) -> list[dict[str, object]]:
    """Items whose latest harvest pass predates the current rules."""
    path = instance.passes_path
    if not path.exists():
        return []
    latest: dict[str, int] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno}: unparseable pass record ({e})") from e
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{lineno}: pass record must be an object: {line!r}")
        if record.get("stage") != "harvest":
            continue
        item = record.get("item")
        rules = record.get("rules")
        if isinstance(item, str) and isinstance(rules, int):
            latest[item] = rules  # later lines supersede (append-order)
    return [
        {"item": item, "rules": rules}
        for item, rules in sorted(latest.items())
        if rules < HARVEST_RULES_VERSION
    ]


# ---------------------------------------------------------------------------
# CLI — parse, build, call; zero business logic
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-lint [--write]."""
    parser = argparse.ArgumentParser(
        prog="dex-lint",
        description="Mechanical health check for the instance at cwd; renders the "
        "health-report surface.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="reconcile derived wiki frontmatter (items: counts, missing generated: dates)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse, build the Instance and capability seam, lint, print, exit."""
    args = build_parser().parse_args(argv)
    instance = Instance(root=Path.cwd())
    try:
        config = Config.load(instance.config_path)
        capabilities = Capabilities.build(config)
        outcome = run_lint(
            instance,
            is_cognitive=capabilities.is_cognitive,
            today=datetime.date.today,
            write=args.write,
        )
    except (ValueError, RuntimeError, OSError, TypeError, AttributeError) as e:
        # TypeError/AttributeError are the backstop for state files whose
        # shape no isinstance guard anticipated — a named exit, never a
        # traceback.
        sys.exit(f"dex-lint: {e}")
    sys.stdout.write(outcome.report)
    if outcome.exit_code:
        sys.exit(outcome.exit_code)


if __name__ == "__main__":
    main()
