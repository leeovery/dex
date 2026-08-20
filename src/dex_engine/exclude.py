"""dex-exclude: purge out-of-scope corpus items, permanently and on the record.

``bin/dex exclude <exclusions.json>`` — the file is a JSON list of
``{"id": ..., "reason": ...}`` records, written by the scope-filter pass.

Each exclusion appends to ``state/exclusions.tsv`` (consulted by
``normalize.py`` so excluded clusters are never regenerated), removes
``corpus/<year>/<id>.md``, and removes ``enrichment/<id>/``.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from .pipeline.types import Instance

__all__ = ["build_parser", "main", "run_exclude"]

_DEFAULT_REASON = "out of scope"


def run_exclude(instance: Instance, entries: list[dict[str, str]]) -> str:
    """Exclude the given items: record why, delete item and enrichment.

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
    exclusions.parent.mkdir(parents=True, exist_ok=True)
    with exclusions.open("a", encoding="utf-8") as f:
        for entry in entries:
            item_id = entry.get("id")
            if not item_id:
                raise ValueError(f"exclusion entry has no id: {entry!r}")
            reason = entry.get("reason", _DEFAULT_REASON)
            if item_id not in existing:
                f.write(f"{item_id}\t{reason}\n")
            item_path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
            if item_path.exists():
                item_path.unlink()
                removed += 1
            else:
                missing += 1
            shutil.rmtree(instance.enrichment_dir / item_id, ignore_errors=True)
    return f"excluded {len(entries)}: removed {removed} items ({missing} already gone)"


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
