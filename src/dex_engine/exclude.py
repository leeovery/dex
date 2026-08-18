"""Exclude corpus items as out-of-scope: delete item + enrichment, record why.

  bin/exclude.py exclusions.json      # [{"id": "...", "reason": "..."}, ...]

Appends to state/exclusions.tsv (consulted by bin/normalize.py so excluded
threads are never regenerated), removes corpus/<year>/<id>.md and
enrichment/<id>/.
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()  # engine runs from the brain repo root
EXCLUSIONS = ROOT / "state" / "exclusions.tsv"


def main() -> None:
    entries = json.loads(Path(sys.argv[1]).read_text())
    existing = set()
    if EXCLUSIONS.exists():
        existing = {l.split("\t")[0] for l in EXCLUSIONS.read_text().splitlines() if l.strip()}
    removed = missing = 0
    with EXCLUSIONS.open("a") as f:
        for e in entries:
            iid, reason = e["id"], e.get("reason", "out of scope")
            if iid not in existing:
                f.write(f"{iid}\t{reason}\n")
            year = iid[:4]
            item = ROOT / "corpus" / year / f"{iid}.md"
            if item.exists():
                item.unlink()
                removed += 1
            else:
                missing += 1
            shutil.rmtree(ROOT / "enrichment" / iid, ignore_errors=True)
    print(f"excluded {len(entries)}: removed {removed} items ({missing} already gone)")


if __name__ == "__main__":
    main()
