"""dex-exclude: purge out-of-scope corpus items, permanently and on the record.

``bin/dex exclude <exclusions.json>`` — the file is a JSON list of
``{"id": ..., "reason": ...}`` records, written by the scope-filter pass.

Each exclusion appends to ``state/exclusions.tsv`` (consulted by
``normalize.py`` so excluded clusters are never regenerated), removes
``corpus/<year>/<id>.md``, removes ``enrichment/<id>/``, and removes the
item's ledger entries — the item is gone, so seeding will never raise
that work again and the lines would otherwise linger forever.

Except the work another live corpus item still claims. A work unit is
keyed by URL, not by item, so two items listing one URL share one ledger
entry that names only one of them; purging on the name alone would delete
a history the survivor still owns. The summary states both counts — this
runs in bulk from the scope-filter pass, so that line is the owner's only
signal.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from .pipeline import ledger
from .pipeline.ownership import corpus_owners
from .pipeline.registry import DRIVERS
from .pipeline.types import Instance

__all__ = ["build_parser", "main", "run_exclude"]

_DEFAULT_REASON = "out of scope"


def run_exclude(instance: Instance, entries: list[dict[str, str]]) -> str:
    """Exclude the given items: record why, delete item, enrichment and ledger entries.

    Args:
        instance: The instance.
        entries: ``{"id": ..., "reason": ...}`` records (reason optional).

    Returns:
        The one-line summary.

    Raises:
        ValueError: An entry has no ``id``.
    """
    exclusions = instance.state_dir / "exclusions.tsv"
    existing: set[str] = set()
    if exclusions.exists():
        existing = {
            line.split("\t")[0]
            for line in exclusions.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    removed = missing = 0
    purged: set[str] = set()
    exclusions.parent.mkdir(parents=True, exist_ok=True)
    with exclusions.open("a", encoding="utf-8") as f:
        for entry in entries:
            item_id = entry.get("id")
            if not item_id:
                raise ValueError(f"exclusion entry has no id: {entry!r}")
            purged.add(item_id)
            # The reason is LLM-authored free text: collapse every
            # whitespace run (tabs and newlines included) so the TSV stays
            # one record per line, tab-delimited, by construction.
            reason = " ".join(entry.get("reason", _DEFAULT_REASON).split()) or _DEFAULT_REASON
            if item_id not in existing:
                f.write(f"{item_id}\t{reason}\n")
            item_path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
            if item_path.exists():
                item_path.unlink()
                removed += 1
            else:
                missing += 1
            shutil.rmtree(instance.enrichment_dir / item_id, ignore_errors=True)
    # One rewrite for the whole batch, after the TSV record lands: an
    # interruption before it leaves the entries in place, and the re-run
    # (which the TSV makes idempotent) purges them.
    # Scanned after the deletions above, so a purged item cannot claim its
    # own work — what remains is what the surviving corpus still lists.
    claimed = corpus_owners(instance.root, DRIVERS) if instance.ledger_path.exists() else {}
    dropped, kept = ledger.drop_items(instance.ledger_path, purged, claimed=claimed)
    return (
        f"excluded {len(entries)}: removed {removed} items ({missing} already gone), "
        f"{dropped} ledger entries dropped, {kept} kept (work another live corpus item "
        "still claims)"
    )


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-exclude <exclusions.json>."""
    parser = argparse.ArgumentParser(
        prog="dex-exclude",
        description="Permanently exclude out-of-scope corpus items "
        '(JSON list of {"id", "reason"}); survives re-normalization.',
    )
    parser.add_argument("file", type=Path, help="the exclusions JSON file")
    return parser


def _load_entries(path: Path) -> list[dict[str, str]]:
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
