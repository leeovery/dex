"""dex-lint: the mechanical health check. Judgment repairs are the session's job.

Checks:

  wiki — broken wikilinks (vs reserved/unbuilt), citations of ids not in the
  corpus, shortid-shaped citations (backticked 6-hex is a probable malformed
  citation everywhere, index included), coverage orphans and their cited
  complement (items pages cite that no taxonomy topic records), ghost
  members (a taxonomy topic's or entity's member id with no corpus file —
  ``exclude`` never edits the session-owned lists, so the id lingers),
  index consistency,
  stale pages, page item-count drift (frontmatter ``items:`` vs the page's
  MEMBER count — its taxonomy topic's items, or its entity-members list;
  never its citation count), and difflib sentence similarity ("possible restated
  fact — merge?").

  state — ledger schema validation (via ``ledger.load``), ledger↔tree
  referential integrity (items with no corpus file, one row per finding
  with its entry count — excluded-on-record told apart from renamed and
  from unclaimed; ``done`` entries whose output is nowhere on disk, and
  ``done`` entries whose output is on disk under a DIFFERENT item's
  enrichment directory, where the item that owns it cannot list it —
  both asked of the item the corpus says owns the unit),
  waiting cohorts and cognitive-job
  summary, items whose fetched pages landed but whose harvest pass was
  never recorded, harvest passes recorded under old rules, and the
  enrichment-newer-than-digest orphan listing (the interrupted-session
  backstop, shared with ``enrich status``).

  judgment drift — the signals the pipeline records for this check: the
  re-entry cap fires nobody overrode (ledger lines carrying a ``cap`` and
  not ``forced``: are the bounds too tight for this corpus, or is harvest
  over-promoting?) and
  thread-completeness markers in enrichment frontmatter
  (``thread_cap_hit`` / ``chain_incomplete``: a stored thread a digest
  could otherwise cite as whole).

  digests — the shape ``state/digests/<id>.md`` promises: frontmatter with
  ``id``, ``date``, a ``signal`` of high|medium|low, and ``topics``, and at
  least one fact bullet. ``enrich item digest`` writes digests and cannot
  write any of those faults, so this is the backstop for the files that
  predate the verb and for anything hand-edited since. Plus one advisory
  on a conforming digest: a ``media:`` listing that no longer names the
  files the item carries, in either direction.

``--write`` reconciles derived wiki frontmatter mechanically: ``items:``
counts are set to the derived member count, and a page that cites items
but carries no ``generated:`` date gains one (today) so staleness has a
baseline. Existing ``generated:`` dates are never rewritten — they are the
staleness reference, and resetting them would mask exactly what the stale
check exists to find.

Output renders through the ``health-report`` surface. Exit 1 on hard
failures: broken wikilinks, bad citations, a ledger schema error, a
malformed taxonomy or entity-members file, an unparseable pass record, a
malformed digest, and the pre-taxonomy broken-mid-ingest state (corpus
items but no ``state/taxonomy.json`` — placement never ran). Every one
renders as a failure row on the report naming the file and its repair;
lint never dies bare on a state file it exists to check.
"""

import argparse
import datetime
import difflib
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import corpus, frontmatter
from .capabilities import Capabilities
from .pipeline import ledger
from .pipeline.classify import ITEM_ID_PATTERN
from .pipeline.digest import item_media
from .pipeline.enrichment import read_enrichment_fields
from .pipeline.ownership import unit_owners
from .pipeline.registry import default_drivers
from .pipeline.run import (
    CAP_BOUNDS,
    HARVEST_RULES_VERSION,
    digest_orphans,
    digested_items,
    items_owing_work,
    never_harvested,
)
from .pipeline.types import Config, Format, Instance, LedgerEntry, Need, Status
from .render import surfaces

__all__ = ["LintOutcome", "build_parser", "main", "run_lint"]

# The strict id grammar is classify's ITEM_ID_PATTERN — one spelling,
# shared with the scrubber and the observed-report rejectors; this regex
# adds only the backticks a citation wears.
ID_RE = re.compile(rf"`({ITEM_ID_PATTERN})`")
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

_DIGEST_SIGNALS = ("high", "medium", "low")
_DIGEST_REQUIRED = ("id", "date", "signal", "topics")

# Thread-completeness markers the x driver stamps into enrichment
# frontmatter when a walk-up stops short of the root.
THREAD_MARKERS = ("thread_cap_hit", "chain_incomplete")

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
    entity_members, entity_members_error = _entity_members(instance)
    corpus_ids = {path.stem for path in instance.corpus_dir.glob("*/*.md")}
    pages = _pages(instance)
    scan = _scan_wiki(pages, taxonomy, entity_members, corpus_ids, write=write, today=today)

    ledgered = _uncategorized_items(taxonomy)
    ghost_members = _ghost_members(taxonomy, entity_members, corpus_ids)
    orphans = sorted(corpus_ids - scan.cited - ledgered)
    # The coverage invariant's other face: a citation is not a placement,
    # so a cited item can still be in no topic's items and off the
    # uncategorized ledger — invisible to the orphan check, which only
    # asks about the uncited. The cited set is the wiki scan's; the
    # placed set is the taxonomy's own listing.
    unplaced = sorted(scan.cited - _placed_items(taxonomy))
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
        "unplaced": unplaced,
        "ghost_members": ghost_members,
        "unindexed": unindexed,
        "ghost_index": ghost_index,
        "stale_pages": scan.stale_pages,
        "count_drift": scan.count_drift,
        "restated": scan.restated,
    }
    if taxonomy_error is not None:
        payload["taxonomy_error"] = taxonomy_error
    if entity_members_error is not None:
        payload["entity_members_error"] = entity_members_error
    ledger_error = _state_checks(instance, payload, is_cognitive, corpus_ids, notes=scan.notes)
    digests = _digest_checks(instance)
    payload["digests"] = digests.count
    payload["digest_errors"] = digests.errors
    payload["digest_media_drift"] = digests.media_drift
    if scan.reconciled:
        payload["reconciled"] = scan.reconciled
    if scan.notes:
        payload["notes"] = scan.notes

    failed = bool(
        scan.broken_links
        or scan.bad_citations
        or ledger_error
        or taxonomy_error
        or entity_members_error
        or "passes_error" in payload
        or digests.errors
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
    uncategorized = topics.get("uncategorized-shares", {}) if isinstance(topics, dict) else {}
    items = uncategorized.get("items", []) if isinstance(uncategorized, dict) else []
    if not isinstance(items, list):
        return set()
    return {item for item in items if isinstance(item, str)}


def _ghost_members(
    taxonomy: dict[str, object],
    entity_members: dict[str, list[str]],
    corpus_ids: set[str],
) -> list[dict[str, str]]:
    """Member ids in the session-owned lists that no corpus file answers for.

    ``bin/dex exclude`` deletes the item, its enrichment, its digest and
    its ledger lines — but ``state/taxonomy.json`` and
    ``state/entity-members.json`` are session-maintained judgment, which
    no verb edits, so the excluded id sits in a topic's ``items`` (or an
    entity's member list) forever: counted by every member-count check,
    read by staleness, resolvable by nothing. One row per (id, list),
    naming both, so the repair needs no hunt.

    A finding, not a failure: nothing downstream crashes on a ghost
    member — it is drift. The repair is the session's: remove the id from
    the named list, and where the item was excluded that is the whole
    repair. Malformed shapes contribute nothing here — they are their own
    loud findings.
    """
    rows: list[dict[str, str]] = []
    topics = taxonomy.get("topics", {})
    if isinstance(topics, dict):
        for name, topic in sorted(topics.items()):
            items = topic.get("items", []) if isinstance(topic, dict) else []
            if not isinstance(items, list):
                continue
            rows += [
                {"item": item, "where": f"topic {name!r}"}
                for item in items
                if isinstance(item, str) and item not in corpus_ids
            ]
    for name, members in sorted(entity_members.items()):
        rows += [
            {"item": member, "where": f"entity {name!r}"}
            for member in members
            if member not in corpus_ids
        ]
    return rows


def _placed_items(taxonomy: dict[str, object]) -> set[str]:
    """Every item id some topic's ``items`` records — uncategorized-shares included.

    The coverage invariant's whole placed set, read with the same
    tolerance for hand-mangled shapes :func:`_uncategorized_items` shows:
    a malformed topic contributes nothing rather than a crash.
    """
    topics = taxonomy.get("topics", {})
    if not isinstance(topics, dict):
        return set()
    placed: set[str] = set()
    for topic in topics.values():
        items = topic.get("items", []) if isinstance(topic, dict) else []
        if isinstance(items, list):
            placed |= {item for item in items if isinstance(item, str)}
    return placed


def _pre_taxonomy_outcome(instance: Instance) -> LintOutcome | None:
    """The two pre-taxonomy states: fresh instance, or broken mid-ingest.

    Rendered through the health-report surface's pre-taxonomy shape like
    every other report — never hand-drawn.
    """
    if (instance.state_dir / "taxonomy.json").exists():
        return None
    stranded = sorted(path.stem for path in instance.corpus_dir.glob("*/*.md"))
    return LintOutcome(
        report=surfaces.render("health-report", {"pre_taxonomy": {"stranded": stranded}}),
        exit_code=1 if stranded else 0,
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


def _entity_members(instance: Instance) -> tuple[dict[str, list[str]], str | None]:
    """The entity-members map, or an empty one plus the finding when it is broken.

    The taxonomy's treatment (:func:`_load_taxonomy`), applied to the
    other session-maintained state file the wiki checks read: reporting a
    broken state file is lint's job, so a malformed
    ``state/entity-members.json`` becomes a loud failure row naming the
    file and its repair — never the bare ``Expecting value…`` exit that
    named nothing. The wiki checks still run, with no entity members.

    The WHOLE documented shape is validated — an object of string ->
    list-of-strings. Tolerating a wrong-typed value read
    ``{"name": "string"}`` as an entity someone emptied: the member-count
    checks ran against a list that was never there, silently, while the
    file the wiki layer depends on stood broken.
    """
    path = instance.state_dir / "entity-members.json"
    if not path.exists():
        return {}, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {}, f"{path.name}: invalid JSON ({' '.join(str(e).split())})"
    if not isinstance(raw, dict):
        return {}, (
            f"{path.name}: expected an object of entity -> item list, got {type(raw).__name__}"
        )
    for name, members in raw.items():
        if not isinstance(members, list):
            return {}, (
                f"{path.name}: entity {name!r} must map to a list of item ids, "
                f"got {type(members).__name__}"
            )
        bad = [m for m in members if not isinstance(m, str)]
        if bad:
            return {}, (
                f"{path.name}: entity {name!r} holds a non-string member "
                f"({type(bad[0]).__name__}: {bad[0]!r}) — members are item-id strings"
            )
    return raw, None


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
    if recorded is not None and expected is not None and int(recorded.group(1)) != expected:
        scan.count_drift.append({"page": name, "recorded": recorded.group(1), "actual": expected})
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
                pairs.append({"page": name, "first": _clip(raw_a), "second": _clip(raw_b)})
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
    *,
    notes: list[str],
) -> bool:
    """Fill the payload's state sections; True when the ledger failed to load.

    ``notes`` is the report's shared note list — the state checks append
    what has no row of its own.
    """
    try:
        entries = ledger.load(instance.ledger_path)
    except ledger.LedgerSchemaError as e:
        payload["ledger_error"] = " ".join(str(e).split())
        entries = None
    if entries is not None:
        payload["ledger_entries"] = len(entries)
        # One corpus pass answers ownership for everything built from these
        # entries: the cognitive rows here — manual-work pointers, owed to
        # the live item like every other surface (:func:`_owner`), never to
        # a renamed-away id — and the integrity scan below. The ONE
        # resolution the engine's own readers use (`ownership.unit_owners`),
        # parent chains included: a renamed item's harvested children and
        # media units are claimed by no frontmatter, and hashing `urls:`
        # alone read every one of them as unclaimed work with a destroyed
        # output — advice that pointed at deleting live state.
        owners = unit_owners(instance.root, entries, default_drivers())
        waiting: dict[str, int] = {}
        cognitive: list[dict[str, str]] = []
        for entry in entries.values():
            if entry.status is not Status.WAITING or entry.needs is None:
                continue
            waiting[entry.needs.value] = waiting.get(entry.needs.value, 0) + 1
            if is_cognitive(entry.needs, entry.format):
                cognitive.append(
                    {
                        "item": _owner(entry, owners, corpus_ids),
                        "url": entry.url,
                        "need": entry.needs.value,
                    }
                )
        payload["waiting"] = waiting
        payload["cognitive"] = cognitive
        _exempt_parked_orphans(instance, payload, entries, corpus_ids, owners)
        integrity = _referential_integrity(instance, entries, corpus_ids, owners)
        payload["ghost_items"] = integrity.ghost
        payload["missing_outputs"] = integrity.missing
        payload["misfiled_outputs"] = integrity.misfiled
        payload["capped"] = _cap_fires(entries)
    payload["never_harvested"] = never_harvested(instance)
    payload["stale_passes"], passes_error = _stale_passes(instance)
    if passes_error is not None:
        payload["passes_error"] = passes_error
    threads = _incomplete_threads(instance)
    payload["incomplete_threads"] = threads.rows
    notes += threads.notes
    payload["digest_orphans"] = digest_orphans(instance)
    return entries is None


def _exempt_parked_orphans(
    instance: Instance,
    payload: dict[str, object],
    entries: dict[str, LedgerEntry],
    corpus_ids: set[str],
    owners: Mapping[str, tuple[str, ...]],
) -> None:
    """Drop the correctly parked items from the coverage-orphan row.

    The row asks which items no page cites and the taxonomy does not
    record — but an item still owing a unit (blocked, waiting, manual,
    queued, error) derives ``raw``, and the ingest procedure forbids
    digesting or placing a raw item: firing on it steered sessions to
    ledger a still-parked item into uncategorized-shares. The exemption
    mirrors the run report's parked handling (:func:`items_owing_work`,
    the same reading the digest backstop applies), and it reaches only
    items whose digest is not recorded: a digested item has been through
    placement, so its absence from the taxonomy is real drift however its
    later units sit.
    """
    orphans = payload.get("orphans")
    if not isinstance(orphans, list):
        return
    digested = digested_items(instance, corpus_ids)
    parked = {item_id for item_id in items_owing_work(entries, owners) if item_id not in digested}
    payload["orphans"] = [item_id for item_id in orphans if item_id not in parked]


EXCLUDED_ON_RECORD = "excluded on record"
_ITEM_UNCLAIMED = "no exclusions.tsv record, and no live corpus item lists this work"


@dataclass(slots=True, kw_only=True)
class _IntegrityScan:
    """The three ledger→tree pointer findings, each its own listing."""

    ghost: list[dict[str, object]] = field(default_factory=list)
    missing: list[dict[str, str]] = field(default_factory=list)
    misfiled: list[dict[str, str]] = field(default_factory=list)


def _referential_integrity(
    instance: Instance,
    entries: dict[str, LedgerEntry],
    corpus_ids: set[str],
    owners: Mapping[str, tuple[str, ...]],
) -> _IntegrityScan:
    """The ledger's pointers into the tree: item ids, and output paths.

    Schema validity says nothing about whether a line points at anything
    that exists. ``dex exclude`` removes an item's ledger entries along with
    the item, and migration 1 drops any line it can attribute only to a dead
    item, so a live entry naming a missing item is no longer anything's
    normal outcome: it is a purge that died partway, an item deleted by
    hand, or work another live item still claims and ``exclude`` therefore
    kept. Which one shows in ``why`` — the exclusions record is the
    difference between a purge that happened and a disappearance nobody
    recorded, and an item RENAMED since the line was written has a live item
    under a new id claiming its work, which reads nothing like an item
    nothing claims at all. A ``done`` entry whose output file is gone is the
    enrichment claiming work whose product no longer exists. The checks are
    asked independently — an item purged by ``dex exclude`` answers two of
    them, and each finding is still true.

    The output path is asked twice, because "is it there" and "is it where
    its item can see it" are different questions with different repairs. An
    item's frontmatter ``enrichment:`` listing, and therefore its derived
    ``raw``/``enriched`` status, is the markdown IN ``enrichment/<item>/`` —
    so a ``done`` output filed under a DIFFERENT item is invisible to the
    item that owns it, which shows an empty listing while the unit is
    ``done`` and never drainable again. A rename done in steps and
    interrupted between the corpus file and the enrichment directory leaves
    exactly that. Moving files is not the drain's business; naming the state
    is this check's.

    **Both are asked of the item the corpus says owns the unit**
    (:func:`_owner`), never of the line's stored ``item``, because both
    resolve a path under ``enrichment/<id>/`` and the stored string is the
    attribution as of the day the line was written. A rename carried
    through moves the corpus file AND the enrichment directory, leaving a
    recorded path that names a directory that is gone while the file sits
    under the new id, under the same name — the rename moved the directory,
    never the file in it. So the owner's directory is the second place each
    check looks, and a completed rename answers neither: nothing is
    missing, nothing is misfiled, and the ghost row alone says what
    happened, which is that the id on the line is history.

    **The record is asked before the claim.** A hash two live items shared
    always has another claimant after one of them is excluded, so asking
    the claim first made every deliberate on-the-record exclusion read as
    a rename — a cause the check never established, over the one the owner
    wrote down. The claim still shows, as the reason those lines survived
    the purge rather than as what became of the item.

    Ghost rows are one per (item, finding): an item named by ten entries for
    one reason is one row carrying the count, not ten rows a reader cannot
    tell apart.

    All three stay findings rather than failures. Nothing downstream
    resolves an entry's item back to a corpus file, so none breaks a later
    stage the way a schema error or a bad citation does; and the repair —
    was this purged on purpose? should the ledger line go, the item come
    back, or the directory follow the rename? — is judgment, which is the
    report's business, not the exit code's.
    """
    excluded = _excluded_items(instance)
    dead = [entry for entry in entries.values() if entry.item not in corpus_ids]
    # ``owners`` is the caller's one corpus pass, shared with the cognitive
    # rows and handed in unconditionally, so :func:`_owner` decides between
    # the stored string and the claim on its own rather than on whether the
    # pass was made at all.
    counts: dict[tuple[str, str], int] = {}
    for entry in dead:
        owner = _live_claimant(entry, owners, corpus_ids)
        if entry.item in excluded:
            # The record answers first, always: an exclusion is a ruling the
            # owner made and wrote down, and the claim says only that
            # `exclude` rightly kept work a survivor shares — which is why
            # the lines are still here, not what happened to the item.
            why = (
                EXCLUDED_ON_RECORD
                if owner is None
                else f"{EXCLUDED_ON_RECORD} — {owner} shares this work"
            )
        elif owner is not None:
            why = f"renamed — {owner} lists this work"
        else:
            why = _ITEM_UNCLAIMED
        counts[(entry.item, why)] = counts.get((entry.item, why), 0) + 1
    ghost: list[dict[str, object]] = [
        {"item": item, "why": why, "entries": count}
        for (item, why), count in sorted(counts.items())
    ]
    landed = [
        (entry.path, _owner(entry, owners, corpus_ids))
        for entry in entries.values()
        if entry.path is not None
    ]
    missing: list[dict[str, str]] = [
        {"item": owner, "path": path}
        for path, owner in landed
        if not (instance.root / path).exists()
        and not (instance.enrichment_dir / owner / Path(path).name).exists()
    ]
    missing.sort(key=lambda row: (row["item"], row["path"]))
    misfiled: list[dict[str, str]] = [
        {"item": owner, "path": path}
        for path, owner in landed
        if (instance.root / path).exists() and Path(path).parent != Path("enrichment") / owner
    ]
    misfiled.sort(key=lambda row: (row["item"], row["path"]))
    return _IntegrityScan(ghost=ghost, missing=missing, misfiled=misfiled)


def _owner(entry: LedgerEntry, owners: Mapping[str, tuple[str, ...]], corpus_ids: set[str]) -> str:
    """The live item that owns this unit — the corpus's answer, else the line's.

    The same rule the engine's readers and its write path apply
    (``run._Drain.owner_of``): the stored string wins while it is still
    live, the corpus answers for a unit whose item was renamed away, and a
    unit nothing live claims keeps the id it was written under — which is
    the ghost finding above, not something to resolve here.
    """
    if entry.item in corpus_ids:
        return entry.item
    return _live_claimant(entry, owners, corpus_ids) or entry.item


def _live_claimant(
    entry: LedgerEntry, owners: Mapping[str, tuple[str, ...]], corpus_ids: set[str]
) -> str | None:
    """The first LIVE item the resolution hands this unit to, or None.

    The resolution always answers — its last fallback is the stored string
    — so a claim reading has to filter to the ids a corpus file actually
    answers for: a dead fallback id proves no claim, and treating it as one
    would erase the unclaimed finding entirely.
    """
    return next((item for item in owners.get(entry.hash, ()) if item in corpus_ids), None)


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


def _stale_passes(instance: Instance) -> tuple[list[dict[str, object]], str | None]:
    """Items whose latest harvest pass predates the current rules, and the file's fault.

    A record no reader can parse — the torn line an interrupted append
    leaves, trailing at first but relocatable mid-file by later appends
    or a union merge — is the file's finding, never a bare crash: the
    report gets a failure row naming ``state/passes.jsonl`` and the line,
    with the sanctioned repair (delete the named torn line — a
    half-written line is not a record). The stale readings computed from
    the parseable records still render; the first broken line is the one
    named.
    """
    path = instance.passes_path
    if not path.exists():
        return [], None
    latest: dict[str, int] = {}
    error: str | None = None
    for lineno, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            detail = " ".join(str(e).split())
            error = error or f"{path.name}:{lineno}: unparseable pass record ({detail})"
            continue
        if not isinstance(record, dict):
            error = error or f"{path.name}:{lineno}: pass record must be an object: {line!r}"
            continue
        if record.get("stage") != "harvest":
            continue
        item = record.get("item")
        rules = record.get("rules")
        if isinstance(item, str) and isinstance(rules, int):
            latest[item] = rules  # later lines supersede (append-order)
    stale: list[dict[str, object]] = [
        {"item": item, "rules": rules}
        for item, rules in sorted(latest.items())
        if rules < HARVEST_RULES_VERSION
    ]
    return stale, error


# ---------------------------------------------------------------------------
# Judgment-drift signals. The pipeline records these for the health check
# specifically — they reach no user-facing surface, and until now had no
# reader.
# ---------------------------------------------------------------------------


def _cap_fires(entries: dict[str, LedgerEntry]) -> list[dict[str, str]]:
    """The cap fires standing in the ledger that nobody overrode, one row each.

    A fire is a tuning reading, not a fault: it says either that the
    depth/URL bounds are too tight for this corpus, or that harvest
    judgment is promoting links the subject rule would not. Which one it
    is takes the shape of the fires — how many, spread across how many
    items, under which bound — so the row carries the item, the refused
    URL, and the bound that refused it, and the surface aggregates.

    The bound comes from the entry's typed ``cap``, never from its stated
    reason: the reason is prose written for the audit trail, and a bound
    worded two ways would aggregate as two bounds.

    **What the reading splits on is the override, not who asked.** Every
    promotion is a URL a session named with ``enrich fetch``, so "who
    typed it" separates nothing; what a refusal says about the bounds is
    the same whether the session promoted the link on the subject rule or
    the owner named it by hand. A refusal that stood is drift to read. A
    refusal the owner then waived with ``--force`` is a decision already
    taken, so it leaves the reading — read off the typed ``forced``, for
    the same reason the bound is read off ``cap``. The refusal is still
    answered where it was asked, on the run report, with its ``--force``
    route; the two surfaces do different jobs and both do theirs.

    Newest first, by item id (date-prefixed). Nothing supersedes a cap
    fire that stood — the skipped line is the ledger's standing record
    that the URL was refused, and compaction keeps it — so this listing
    only grows, and oldest-first meant the rows the surface shows were
    held forever by the oldest items, with a fire stamped today appearing
    nowhere.
    """
    return [
        {"item": entry.item, "url": entry.url, "reason": CAP_BOUNDS[entry.cap]}
        for entry in sorted(entries.values(), key=lambda e: (e.item, e.url), reverse=True)
        if entry.cap is not None and not entry.forced
    ]


@dataclass(slots=True, kw_only=True)
class _ThreadScan:
    """What the marker scan over the enrichment files found."""

    rows: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _incomplete_threads(instance: Instance) -> _ThreadScan:
    """Enrichment files whose stored thread stops short of the root.

    The x driver stamps ``thread_cap_hit`` / ``chain_incomplete`` into
    frontmatter when a walk-up hits the driver's hop bound (a sanity stop
    against a cycle, not an editorial one) or a parent it cannot fetch.
    Unread, a half-thread reads exactly like a whole one, and the digest
    cites it as though the root were there. The bound is stated in one
    place — ``drivers.x.MAX_HOPS`` — and restated here in no number.

    Only the frontmatter is read: enrichment bodies are whole transcripts.
    A file this scan cannot read is reported as a note rather than skipped:
    no other check in lint opens an enrichment file, so a dropped one is
    invisible to every check, and the half-thread inside it is invisible
    forever. Unreadable covers both shapes — bytes no decoder accepts, and
    a frontmatter block that opens and never closes, which is what a run
    interrupted mid-write leaves behind. It stays a note, never a failure —
    the markers are a caution to the session, not a broken contract
    downstream.

    Newest first, and read as a standing count rather than a work list.
    Nothing clears a marker — a parent that 404s stays unfetchable — so
    this listing only grows, as the cap-fire listing above it does, and
    oldest-first would mean a thread stamped today is never the one shown.
    Newest is by item id (date-prefixed): the stamping instant is not
    recorded, and a file mtime resets on any checkout.
    """
    scan = _ThreadScan()
    for path in sorted(instance.enrichment_dir.glob("*/*.md"), reverse=True):
        rel = str(path.relative_to(instance.root))
        try:
            fields = read_enrichment_fields(path)
        except (OSError, UnicodeDecodeError) as e:
            scan.notes.append(_unread_note(rel, f"unreadable ({e.__class__.__name__})"))
            continue
        except ValueError as e:
            scan.notes.append(_unread_note(rel, str(e)))
            continue
        markers = [marker for marker in THREAD_MARKERS if fields.get(marker) == "true"]
        if not markers:
            continue
        note = " ".join((fields.get("chain_note") or "").split())
        scan.rows.append(
            {
                "path": rel,
                # The driver's note says how far the walk got; without one
                # the marker names itself. Both render on a single line.
                "why": note or " + ".join(markers),
            }
        )
    return scan


def _unread_note(rel: str, why: str) -> str:
    """One note for an enrichment file the marker scan got no markers out of."""
    return f"{rel}: {why} — thread completeness unknown, and no other check reads enrichment files"


# ---------------------------------------------------------------------------
# Digest shape. `enrich item digest` serializes the judgment, so a digest
# written since it shipped conforms by construction; this reads the ones
# that predate it, and anything a hand has been through since.
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class _DigestScan:
    """What the one pass over ``state/digests/`` found."""

    count: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    media_drift: list[str] = field(default_factory=list)


def _digest_checks(instance: Instance) -> _DigestScan:
    """How many digests there are, the ones that do not conform, and media drift.

    Shape first. How MANY facts a digest states is the source's business —
    two lines of tweet yield two facts and no honest digest can invent a
    third — so there is no target count and no count finding. Stating
    none at all is different in kind: the file is empty of the one thing
    it exists to hold.

    The count comes back with the findings because the section's heading
    states its scale, and this pass is the one that walks the directory.

    Media drift is asked only of a digest whose shape holds: a malformed
    one already carries the same repair (rewrite it through the verb), and
    a second row about the same file would say nothing the first did not.
    """
    scan = _DigestScan()
    for path in sorted(instance.digests_dir.glob("*.md")):
        scan.count += 1
        item = path.stem or path.name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            scan.errors.append({"item": item, "why": f"unreadable ({e.__class__.__name__})"})
            continue
        fields, body = _digest_parts(text)
        if fields is None:
            scan.errors.append({"item": item, "why": "no complete frontmatter fence"})
            continue
        why = _digest_frontmatter_fault(item, fields) or _digest_body_fault(body)
        if why is not None:
            scan.errors.append({"item": item, "why": why})
        elif _media_drifted(instance, item, fields):
            scan.media_drift.append(item)
    return scan


def _media_drifted(instance: Instance, item_id: str, fields: dict[str, str | list[str]]) -> bool:
    """Whether the digest's ``media:`` disagrees with the files the item carries.

    The wiki layer embeds from this listing, so a digest that has lost it
    costs the page its pointer — and nothing else detects that. The
    staleness backstop compares pass records, not content, so a digest
    written before the engine derived the list (engine ≤0.1.4, which stated
    only what the corpus frontmatter did) sits degraded until some
    unrelated rerun happens to touch the item.

    A FINDING, never a failure: the repair is re-emitting the digest
    through ``enrich item digest``, which is a session's act, and failing
    the check on legacy drift would stop every scheduled run on work no
    machine can do.

    Compared as SETS, in either direction. Legacy digests wrote the flow
    form and the verb writes block form; a hand-written listing may be in
    any order; and a path stated for a file since deleted is drift the
    same as a file on disk the listing never gained. Membership is the
    only thing separating a healthy digest from a drifted one.

    An item with no readable corpus file derives nothing to compare
    against, so it is no drift here — its absence is the ghost and
    coverage rows' business.
    """
    path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    try:
        item = corpus.read_item(path)
    except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError):
        return False
    return _stated_media(fields) != set(item_media(instance, item))


def _stated_media(fields: dict[str, str | list[str]]) -> set[str]:
    """The paths a digest's ``media:`` names, in whichever form it wrote them."""
    stated = fields.get("media", [])
    if isinstance(stated, str):
        return {stated} if stated else set()
    return set(stated)


def _digest_body_fault(body: str) -> str | None:
    """The body fault: no fact bullets at all, or None."""
    if any(line.startswith("- ") for line in body.split("\n")):
        return None
    return "states no facts — the digest body has no bullets"


def _digest_parts(text: str) -> tuple[dict[str, str | list[str]] | None, str]:
    """A digest's frontmatter fields and body; fields None without a fence.

    Any YAML a digest may hold, not the canonical subset the verb emits:
    the files this reads are the ones written before the verb or edited by
    hand since, and nothing told that hand to leave a scalar unquoted. So
    quotes come off (the same way every other frontmatter reader takes
    them off), and a list is read in either form — flow (``[a, b]``) or
    block (``- a`` lines under the key). A shape check that failed on a
    correct digest would route a healthy instance to repair.
    """
    if not text.startswith("---\n"):
        return None, text
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        return None, text
    fields: dict[str, str | list[str]] = {}
    lines = head.split("\n")
    i = 0
    while i < len(lines):
        key, colon, value = lines[i].partition(":")
        i += 1
        if not colon:
            continue
        raw = value.strip()
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            fields[key.strip()] = (
                [frontmatter.unquote(part.strip()) for part in inner.split(",")] if inner else []
            )
        elif raw:
            fields[key.strip()] = frontmatter.unquote(raw)
        else:
            # A key with nothing after the colon opens a block list; with
            # no items under it, it is the empty value it looks like.
            items: list[str] = []
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                items.append(frontmatter.unquote(lines[i].lstrip()[2:].strip()))
                i += 1
            fields[key.strip()] = items or ""
    return fields, body


def _digest_frontmatter_fault(item: str, fields: dict[str, str | list[str]]) -> str | None:
    """The first way this digest's frontmatter breaks the contract, or None."""
    missing = [key for key in _DIGEST_REQUIRED if fields.get(key, "") == ""]
    if missing:
        return f"frontmatter missing {', '.join(missing)}"
    if fields["signal"] not in _DIGEST_SIGNALS:
        return f"signal must be one of {', '.join(_DIGEST_SIGNALS)}, got {fields['signal']!r}"
    if not fields["topics"]:
        return "topics is empty — every item is placed, uncategorized-shares included"
    if fields["id"] != item:
        # The id keys the corpus, the taxonomy, and every citation; a digest
        # filed under one id claiming another is unresolvable either way.
        return f"id is {fields['id']!r} but the file is {item}.md"
    return None


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
