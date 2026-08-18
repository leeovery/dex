"""Mechanical lint for the dex wiki. Judgment checks are the agent's job.

Checks:
  1. wikilinks: broken (not a taxonomy key or page) vs reserved (valid, unbuilt)
  2. citations: backticked item ids that don't exist in corpus
  3. coverage: corpus items cited nowhere and not in the uncategorized-shares ledger
  4. index: pages missing from wiki/index.md, index entries without pages
  5. staleness: topic pages whose members include items newer than the page

  bin/dex lint           # report to stdout
  bin/dex lint --write   # also write state/lint-report.md
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path.cwd()  # engine runs from the instance repo root
WIKI = ROOT / "wiki"

ID_RE = re.compile(r"`(\d{4}-\d{2}-\d{2}-[a-z0-9-]+-[0-9a-f]{6})`")
LINK_RE = re.compile(r"\[\[([a-z0-9-]+)\]\]")


def main() -> None:
    tax_path = ROOT / "state" / "taxonomy.json"
    if not tax_path.exists():
        print("fresh instance: no state/taxonomy.json yet — nothing to lint.")
        return
    tax = json.loads(tax_path.read_text())
    topics, entities = set(tax["topics"]), set(tax["entities"])
    corpus_ids = {p.stem for p in (ROOT / "corpus").glob("*/*.md")}
    pages = {p.stem: p for p in WIKI.glob("topics/*.md")}
    pages |= {p.stem: p for p in WIKI.glob("syntheses/*.md")}
    pages |= {p.stem: p for p in WIKI.glob("entities/*.md")}
    valid_targets = topics | entities | set(pages)

    broken_links, reserved_links = [], []
    bad_citations = []
    cited = set()
    stale = []

    for name, path in sorted(pages.items()):
        text = path.read_text()
        for target in set(LINK_RE.findall(text)):
            if target in pages:
                continue
            elif target in valid_targets:
                reserved_links.append((name, target))
            else:
                broken_links.append((name, target))
        ids = set(ID_RE.findall(text))
        cited |= ids & corpus_ids
        bad_citations += [(name, i) for i in ids - corpus_ids]
        gen = re.search(r"^generated: (\d{4}-\d{2}-\d{2})", text, re.M)
        topic = re.search(r"^topic: (.+)$", text, re.M)
        if gen and topic and topic.group(1) in tax["topics"]:
            newer = [i for i in tax["topics"][topic.group(1)]["items"]
                     if i[:10] > gen.group(1)]
            if newer:
                stale.append((name, len(newer)))

    ledgered = set(tax["topics"].get("uncategorized-shares", {}).get("items", []))
    orphans = corpus_ids - cited - ledgered

    index = (WIKI / "index.md").read_text() if (WIKI / "index.md").exists() else ""
    unindexed = [n for n in pages if f"[[{n}]]" not in index]
    ghost_index = [t for t in set(LINK_RE.findall(index))
                   if t not in pages and t not in valid_targets]

    lines = [
        f"corpus items: {len(corpus_ids)} | pages: {len(pages)} | cited: {len(cited)}",
        f"1. broken wikilinks: {len(broken_links)}",
        *[f"   {p} -> [[{t}]]" for p, t in broken_links[:20]],
        f"   (reserved/unbuilt links: {len(reserved_links)} — informational)",
        f"2. bad citations (id not in corpus): {len(bad_citations)}",
        *[f"   {p} -> {i}" for p, i in bad_citations[:20]],
        f"3. orphan items (uncited, unledgered): {len(orphans)}",
        *[f"   {i}" for i in sorted(orphans)[:20]],
        f"4. pages missing from index: {len(unindexed)} {unindexed[:10]}",
        f"   ghost index entries: {len(ghost_index)} {ghost_index[:10]}",
        f"5. stale pages (members newer than page): {len(stale)}",
        *[f"   {n}: {c} newer items" for n, c in sorted(stale, key=lambda x: -x[1])[:15]],
    ]
    report = "\n".join(lines)
    print(report)
    if "--write" in sys.argv:
        out = ROOT / "state" / "lint-report.md"
        out.write_text(f"# Lint report — {datetime.now():%Y-%m-%d}\n\n```\n{report}\n```\n")
        print(f"\nwritten: {out.relative_to(ROOT)}")
    if broken_links or bad_citations:
        sys.exit(1)


if __name__ == "__main__":
    main()
