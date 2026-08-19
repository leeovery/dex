"""Scaffold a new dex instance from the engine's bundled instance template.

  uvx --from git+https://github.com/leeovery/dex dex-new <name>

Creates ./<name>: the directory tree, seed CLAUDE.md and README.md (both to
be personalized), the engine-managed machinery, git init, and local LFS.
"""

import subprocess
import sys
from importlib import resources
from pathlib import Path

from .sync import sync

TREE = ["corpus", "enrichment", "wiki/topics", "wiki/entities",
        "wiki/syntheses", "state/digests", "raw", "bin", "media", "inbox"]

SEEDS = {
    ".gitignore": ".DS_Store\n.env\n",
    "state/normalize-config.json":
        '{\n  "name_map": {},\n  "internal_domains": []\n}\n',
    "wiki/index.md": "# Index\n\nNo pages yet — first ingest pending.\n",
    "wiki/log.md": "# Ops log\n",
    "wiki/pins.md":
        "# Pins\n\nHuman corrections as claim+anchor; regeneration must "
        "re-apply these.\n",
    "inbox/.gitkeep": "",
}


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: dex-new <name>")
    root = Path.cwd() / sys.argv[1]
    if root.exists() and any(root.iterdir()):
        sys.exit(f"{root} exists and is not empty")

    tpl = resources.files("dex_engine") / "instance"
    for d in TREE:
        (root / d).mkdir(parents=True, exist_ok=True)
    for rel, content in SEEDS.items():
        (root / rel).write_text(content)
    (root / "CLAUDE.md").write_text((tpl / "CLAUDE.md").read_text())
    (root / "README.md").write_text((tpl / "README.md").read_text())
    sync(root)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "lfs", "install", "--local"], cwd=root,
                   check=True, capture_output=True)

    print(f"created {root}")
    print("next: personalize CLAUDE.md and README.md, commit, then:")
    print(f"  gh repo create {root.name} --private --source . --push")
    print("  bin/dex inbox ensure")


if __name__ == "__main__":
    main()
