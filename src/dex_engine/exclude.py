"""dex-exclude: purge out-of-scope corpus items, permanently and on the record.

``bin/dex exclude <exclusions.json>`` — the file is a JSON list of
``{"id": ..., "reason": ...}`` records, written by the scope-filter pass.

Each exclusion appends to ``state/exclusions.tsv`` (consulted by
``normalize.py`` so excluded clusters are never regenerated), removes
``corpus/<year>/<id>.md``, removes ``enrichment/<id>/``, removes
``state/digests/<id>.md``, and removes the item's ledger entries — the
item is gone, so seeding will never raise that work again and the lines
would otherwise linger forever. The digest goes with the rest: it is a
permanent fact index over content this instance has just ruled out of
scope, and nothing else ever deletes one, so leaving it behind keeps a
permanently excluded item feeding query and wiki forever.

Except the work another live corpus item still claims. A work unit is
keyed by URL, not by item, so two items listing one URL share one ledger
entry that names only one of them; purging on the name alone would delete
a history the survivor still owns. The summary states both counts — this
runs in bulk from the scope-filter pass, so that line is the owner's only
signal. A corpus file that cannot be read claims nothing, which would make
that veto fail open toward deletion, so a batch that meets one purges no
ledger entries at all and the summary says why.

The exclusions file is LLM-authored and this deletes recursively, so every
entry in the batch is validated before anything is written or removed: an
id is a corpus item id, never a path, every value is checked for its type,
and the batch is refused whole. A half-applied batch is worse than none —
its first entry is recorded in ``state/exclusions.tsv`` permanently, with
its ledger purge unrun and the entries after it silently lost.

One id twice in a batch is not a refusal — the operation is idempotent, so
the second copy asks for what the first already did. The copies collapse to
one entry (:func:`_deduplicated`) and the summary states how many, because
applied twice they wrote the permanent record twice and reported the second
pass as an item already gone.
"""

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from . import corpus
from .pipeline import ledger
from .pipeline.ownership import corpus_owners
from .pipeline.registry import DRIVERS
from .pipeline.types import Instance
from .pipeline.urls import resolve_repo_path

__all__ = ["build_parser", "main", "run_exclude"]

_DEFAULT_REASON = "out of scope"

# An item id names one file in the corpus tree — the normalizer mints
# `<date>-<slug>-<shortid>`, a single path component. Anything with a
# separator in it is not an id, and `corpus_dir / item_id[:4] /
# f"{item_id}.md"` would follow it out of the instance: an absolute path
# replaces the root outright, `..` climbs above it.
_ITEM_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Exclusion:
    """One validated entry: the item id, its reason, and the paths it removes."""

    item_id: str
    reason: str
    item_path: Path
    enrichment_path: Path
    digest_path: Path


def run_exclude(instance: Instance, entries: list[dict[str, object]]) -> str:
    """Exclude the given items: record why, delete item, enrichment, digest and ledger entries.

    Args:
        instance: The instance.
        entries: ``{"id": ..., "reason": ...}`` records (reason optional).

    Returns:
        The one-line summary.

    Raises:
        ValueError: An entry is not an object, has no ``id``, has an ``id``
            that is not a corpus item id or resolves outside the instance,
            or has a non-string ``reason``. The whole batch is refused
            before anything is written or deleted.
    """
    validated = _deduplicated([_validated(instance, entry) for entry in entries])
    exclusions = instance.state_dir / "exclusions.tsv"
    existing: set[str] = set()
    if exclusions.exists():
        existing = {
            line.split("\t")[0]
            for line in exclusions.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    removed = missing = digests = 0
    exclusions.parent.mkdir(parents=True, exist_ok=True)
    with exclusions.open("a", encoding="utf-8") as f:
        for exclusion in validated:
            if exclusion.item_id not in existing:
                f.write(f"{exclusion.item_id}\t{exclusion.reason}\n")
            if exclusion.item_path.exists():
                exclusion.item_path.unlink()
                removed += 1
            else:
                missing += 1
            shutil.rmtree(exclusion.enrichment_path, ignore_errors=True)
            if exclusion.digest_path.exists():
                exclusion.digest_path.unlink()
                digests += 1
    # One rewrite for the whole batch, after the TSV record lands: an
    # interruption before it leaves the entries in place, and the re-run
    # (which the TSV makes idempotent) purges them.
    # Scanned after the deletions above, so a purged item cannot claim its
    # own work — what remains is what the surviving corpus still lists.
    purged = {exclusion.item_id for exclusion in validated}
    collapsed = len(entries) - len(validated)
    counted = f"{len(validated)}"
    if collapsed:
        counted += f" ({collapsed} duplicate id(s) collapsed)"
    summary = (
        f"excluded {counted}: removed {removed} items ({missing} already gone), "
        f"{digests} digests, "
    )
    if not instance.ledger_path.exists():
        return summary + _purge_counts(0, 0)
    unreadable = _unreadable_corpus_items(instance.root)
    if unreadable:
        # The claim veto decides a deletion, and a corpus file that cannot
        # be read cannot say which work it claims: purging anyway would
        # take a live item's history and report a clean drop. So nothing is
        # purged until the file is readable — the item and its enrichment
        # still go, and the re-run purges.
        return (
            summary
            + _purge_counts(0, 0)
            + f"; {unreadable} corpus file(s) could not be read, so no ledger entries "
            "were purged — repair them (dex-lint names them) and re-run"
        )
    claimed = corpus_owners(instance.root, DRIVERS)
    dropped, kept = ledger.drop_items(instance.ledger_path, purged, claimed=claimed)
    return summary + _purge_counts(dropped, kept)


def _deduplicated(validated: list[_Exclusion]) -> list[_Exclusion]:
    """One entry per id, the first occurrence winning.

    The exclusions file is LLM-authored and runs in bulk, so the same id
    twice is a realistic input — and it is not an error: excluding an item
    is idempotent by construction (the TSV record is exactly what makes a
    re-run safe), so the second copy asks for what the first already did.
    Applied twice it wrote the id to ``state/exclusions.tsv`` twice — a
    permanent record — and counted the second pass as ``already gone``,
    reporting an item nothing had removed on the batch's only signal line.

    A second reason for an id is dropped with it; the batch states how many
    copies it collapsed so the count is never silently short.
    """
    by_id: dict[str, _Exclusion] = {}
    for exclusion in validated:
        by_id.setdefault(exclusion.item_id, exclusion)
    return list(by_id.values())


def _purge_counts(dropped: int, kept: int) -> str:
    return (
        f"{dropped} ledger entries dropped, {kept} kept (work another live corpus item "
        "still claims)"
    )


def _validated(instance: Instance, entry: object) -> _Exclusion:
    """One entry's types checked and its paths resolved inside the instance.

    Every value is LLM-authored, so none is trusted for its type: the batch
    runs in bulk and the operation is a recursive delete, so a wrong shape
    must be a refusal naming the entry, not a ``TypeError`` half way down
    the list. The id is checked for shape AND its paths resolved under the
    root: the shape catches an id that is a path, the resolution catches a
    corpus file, enrichment directory or digest symlinked out of the
    instance.

    Raises:
        ValueError: Any of those, naming the offending entry.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"exclusion entry is not an object: {entry!r}")
    item_id = entry.get("id")
    if not item_id:
        raise ValueError(f"exclusion entry has no id: {entry!r}")
    if not isinstance(item_id, str) or not _ITEM_ID_RE.fullmatch(item_id):
        raise ValueError(
            f"exclusion id is not a corpus item id: {item_id!r} (in {entry!r}) — an id "
            "names one file in the corpus tree, never a path"
        )
    raw_reason = entry.get("reason", _DEFAULT_REASON)
    if not isinstance(raw_reason, str):
        raise ValueError(
            f"exclusion reason for {item_id} must be a string, got "
            f"{type(raw_reason).__name__}: {entry!r}"
        )
    item_path = resolve_repo_path(instance.root, f"corpus/{item_id[:4]}/{item_id}.md")
    enrichment_path = resolve_repo_path(instance.root, f"enrichment/{item_id}")
    digest_path = resolve_repo_path(instance.root, f"state/digests/{item_id}.md")
    if item_path is None or enrichment_path is None or digest_path is None:
        raise ValueError(
            f"exclusion id {item_id!r} resolves outside the instance — an operation "
            "writes only inside this instance's root, so the batch is refused"
        )
    return _Exclusion(
        item_id=item_id,
        # The reason is LLM-authored free text: collapse every whitespace
        # run (tabs and newlines included) so the TSV stays one record per
        # line, tab-delimited, by construction.
        reason=" ".join(raw_reason.split()) or _DEFAULT_REASON,
        item_path=item_path,
        enrichment_path=enrichment_path,
        digest_path=digest_path,
    )


def _unreadable_corpus_items(root: Path) -> int:
    """How many corpus files cannot be read at all.

    ``corpus_owners`` skips them silently, rightly for its other callers —
    but here its map is a delete decision, and it fails OPEN: a hash
    claimed only by an item whose frontmatter will not parse reads as
    claimed by nobody, and the hash's whole history goes, reported as a
    clean drop. Counted separately (a second parse, on a command that runs
    rarely and in bulk) rather than by changing what that map means to
    every caller.
    """
    corpus_dir = root / "corpus"
    if not corpus_dir.is_dir():
        return 0
    return sum(1 for path in sorted(corpus_dir.glob("*/*.md")) if not _readable(path))


def _readable(path: Path) -> bool:
    try:
        corpus.read_item(path)
    except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError):
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-exclude <exclusions.json>."""
    parser = argparse.ArgumentParser(
        prog="dex-exclude",
        description="Permanently exclude out-of-scope corpus items "
        '(JSON list of {"id", "reason"}); survives re-normalization.',
    )
    parser.add_argument("file", type=Path, help="the exclusions JSON file")
    return parser


def _load_entries(path: Path) -> list[dict[str, object]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        raise ValueError(f"{path}: expected a JSON list of objects")
    return raw


def main(argv: list[str] | None = None) -> None:
    """Parse, build the Instance, exclude, print the summary."""
    args = build_parser().parse_args(argv)
    instance = Instance(root=Path.cwd())
    try:
        summary = run_exclude(instance, _load_entries(args.file))
    except (OSError, ValueError) as e:
        sys.exit(f"dex-exclude: {e}")
    sys.stdout.write(summary + "\n")


if __name__ == "__main__":
    main()
