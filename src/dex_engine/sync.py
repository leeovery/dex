"""Sync engine-managed machinery into an instance.

Refreshes from the engine's bundled instance template: .claude/skills/dex-*,
.claude/dex-contract.md, bin/dex, .gitattributes. Instance-owned files
(CLAUDE.md, README, content) are never touched.

Run from the instance root: dex-sync (or bin/dex sync)
"""

import os
from importlib import resources
from pathlib import Path


def _write_if_changed(root: Path, dest: Path, content: str, changed: list) -> None:
    if dest.exists() and dest.read_text() == content:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    changed.append(str(dest.relative_to(root)))


def _copy_tree(root: Path, src_dir, dest_dir: Path, changed: list) -> None:
    for item in src_dir.iterdir():
        if item.is_dir():
            _copy_tree(root, item, dest_dir / item.name, changed)
        elif item.is_file():
            _write_if_changed(root, dest_dir / item.name, item.read_text(), changed)


def sync(root: Path) -> list:
    tpl = resources.files("dex_engine") / "instance"
    changed: list = []
    for skill in (tpl / "skills").iterdir():
        if skill.is_dir():
            _copy_tree(root, skill, root / ".claude" / "skills" / skill.name, changed)
    _write_if_changed(root, root / ".claude" / "dex-contract.md",
                      (tpl / "dex-contract.md").read_text(), changed)
    _write_if_changed(root, root / "bin" / "dex", (tpl / "dex").read_text(), changed)
    os.chmod(root / "bin" / "dex", 0o755)
    _write_if_changed(root, root / ".gitattributes",
                      (tpl / "gitattributes").read_text(), changed)
    return changed


def main() -> None:
    changed = sync(Path.cwd())
    if changed:
        print("synced from engine:")
        for c in changed:
            print(f"  {c}")
        print("review + commit these.")
    else:
        print("instance machinery already current.")


if __name__ == "__main__":
    main()
