"""dex-lint: the mechanical health check. Judgment repairs are the session's job.

Checks (§14's grown set):

  wiki — broken wikilinks (vs reserved/unbuilt), citations of ids not in the
  corpus, shortid-shaped citations (backticked 6-hex is a probable malformed
  citation everywhere, index included), coverage orphans, index consistency,
  stale pages, page item-count drift (frontmatter ``items:`` vs actual
  citations), and difflib same-page sentence similarity ("possible restated
  fact — merge?").

  state — ledger schema validation (via ``ledger.load``), waiting cohorts and
  cognitive-job summary, harvest passes recorded under old rules, the
  non-empty quarantine file flag (§12), and the enrichment-newer-than-digest
  orphan listing (the interrupted-session backstop, shared with
  ``enrich status``).

``--write`` reconciles derived wiki frontmatter mechanically: ``items:``
counts are set to the actual distinct citations, and a page that cites items
but carries no ``generated:`` date gains one (today) so staleness has a
baseline. Existing ``generated:`` dates are never rewritten — they are the
staleness reference, and resetting them would mask exactly what the stale
check exists to find.

Output renders through the ``health-report`` surface (§11). Exit 1 on hard
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
from .pipeline.run import HARVEST_RULES_VERSION, digest_orphans
from .pipeline.types import Config, Format, Instance, Need, Status
from .render import surfaces

__all__ = ["LintOutcome", "build_parser", "main", "run_lint"]

ID_RE = re.compile(r"`(\d{4}-\d{2}-\d{2}-[a-z0-9-]+-[0-9a-f]{6})`")
LINK_RE = re.compile(r"\[\[([a-z0-9-]+)\]\]")
# A backticked bare 6-hex token: shortid-shaped, a probable malformed
# citation anywhere it appears (§14) — full ids never match this.
SHORTID_RE = re.compile(r"`([0-9a-f]{6})`")
GENERATED_RE = re.compile(r"^generated: (\d{4}-\d{2}-\d{2})$", re.MULTILINE)
TOPIC_RE = re.compile(r"^topic: (.+)$", re.MULTILINE)
ITEMS_RE = re.compile(r"^items: (\d+)$", re.MULTILINE)

# Same-page sentence similarity (§14): difflib, no models. Sentences shorter
# than the floor are boilerplate-prone; the ratio is SequenceMatcher's.
RESTATED_RATIO = 0.85
RESTATED_MIN_CHARS = 40

# The §12 quarantine file — lint flags it non-empty at every health check.
QUARANTINE_FILE = "enrichment-ledger.unmigrated.jsonl"

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
    """Run every mechanical check against the instance (§14).

    Args:
        instance: The instance.
        is_cognitive: The §6 seam — whether a waiting need resolves to the
            cognitive floor (wired to ``Capabilities.is_cognitive`` by the
            CLI; faked in tests).
        today: Injected clock (§14) — stamps ``--write``'s repaired
            ``generated:`` dates.
        write: Reconcile derived wiki frontmatter (``items:`` counts,
            missing ``generated:`` dates).

    Returns:
        The outcome: rendered report and exit code.
    """
    special = _pre_taxonomy_outcome(instance)
    if special is not None:
        return special
    taxonomy = json.loads((instance.state_dir / "taxonomy.json").read_text(encoding="utf-8"))
    corpus_ids = {path.stem for path in instance.corpus_dir.glob("*/*.md")}
    pages = _pages(instance)
    scan = _scan_wiki(pages, taxonomy, corpus_ids, write=write, today=today)

    ledgered = set(
        taxonomy.get("topics", {}).get("uncategorized-shares", {}).get("items", [])
    )
    orphans = sorted(corpus_ids - scan.cited - ledgered)
    unindexed, ghost_index = _index_consistency(instance, pages, taxonomy)
    # Shortid-shaped citations flag everywhere — index included (§14: latent
    # shortids in an index never tripped the citation check because no page
    # existed to fail against).
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
    ledger_error = _state_checks(instance, payload, is_cognitive)
    if scan.reconciled:
        payload["reconciled"] = scan.reconciled
    if scan.notes:
        payload["notes"] = scan.notes

    failed = bool(scan.broken_links or scan.bad_citations or ledger_error)
    return LintOutcome(
        report=surfaces.render("health-report", payload),
        exit_code=1 if failed else 0,
    )


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


def _scan_wiki(
    pages: dict[str, Path],
    taxonomy: dict[str, object],
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
        _scan_staleness(scan, name, text, topics if isinstance(topics, dict) else {})
        text = _scan_counts(scan, name, path, text, len(ids), write=write, today=today)
        scan.restated += _restated_pairs(name, text)
    return scan


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


def _scan_staleness(scan: _WikiScan, name: str, text: str, topics: dict[str, object]) -> None:
    generated = GENERATED_RE.search(text)
    topic = TOPIC_RE.search(text)
    if not generated or not topic or topic.group(1) not in topics:
        return
    members = topics[topic.group(1)]
    items = members.get("items", []) if isinstance(members, dict) else []
    newer = sum(1 for item_id in items if item_id[:10] > generated.group(1))
    if newer:
        scan.stale_pages.append({"page": name, "newer": newer})


def _scan_counts(  # noqa: PLR0913 — the reconcile touches scan, page identity, text, count, and both seams
    scan: _WikiScan,
    name: str,
    path: Path,
    text: str,
    actual: int,
    *,
    write: bool,
    today: Callable[[], datetime.date],
) -> str:
    """The item-count consistency check, with the ``--write`` reconcile (§14).

    Returns the (possibly rewritten) page text so downstream checks see what
    is now on disk.
    """
    recorded = ITEMS_RE.search(text)
    if recorded is not None and int(recorded.group(1)) != actual:
        scan.count_drift.append({"page": name, "recorded": recorded.group(1), "actual": actual})
        if write:
            text = ITEMS_RE.sub(f"items: {actual}", text, count=1)
            path.write_text(text, encoding="utf-8")
            scan.reconciled.append(f"{name}: items: {recorded.group(1)} -> {actual}")
    if actual and GENERATED_RE.search(text) is None:
        if write and text.startswith("---\n"):
            stamp = f"generated: {today().isoformat()}"
            text = text.replace("\n---\n", f"\n{stamp}\n---\n", 1)
            path.write_text(text, encoding="utf-8")
            scan.reconciled.append(f"{name}: {stamp} added (was missing)")
        else:
            scan.notes.append(
                f"{name}: cites items but has no generated: date — staleness cannot "
                "be computed (lint --write adds one)"
            )
    return text


# -- restated facts (§14): difflib, no models --------------------------------

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
    sources is exactly the restatement the check exists to catch — §14's
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
    instance: Instance, payload: dict[str, object], is_cognitive: IsCognitive
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
    payload["stale_passes"] = _stale_passes(instance)
    payload["quarantine"] = _quarantine_lines(instance)
    payload["digest_orphans"] = digest_orphans(instance)
    return entries is None


def _stale_passes(instance: Instance) -> list[dict[str, object]]:
    """Items whose latest harvest pass predates the current rules (§3/§10)."""
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


def _quarantine_lines(instance: Instance) -> int:
    path = instance.state_dir / QUARANTINE_FILE
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").split("\n") if line.strip())


# ---------------------------------------------------------------------------
# CLI — parse, build, call; zero business logic (§14)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-lint [--write]."""
    parser = argparse.ArgumentParser(
        prog="dex-lint",
        description="Mechanical health check for the instance at cwd; renders the "
        "health-report surface (§14).",
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
    except (ValueError, RuntimeError, OSError) as e:
        sys.exit(f"dex-lint: {e}")
    sys.stdout.write(outcome.report)
    if outcome.exit_code:
        sys.exit(outcome.exit_code)


if __name__ == "__main__":
    main()
