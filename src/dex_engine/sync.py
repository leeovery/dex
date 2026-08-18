"""Sync engine-managed machinery into the current instance.

Refreshes from the engine's bundled templates: .claude/skills/dex-*,
bin/dex, .gitattributes. Instance-owned files (CLAUDE.md, README,
content) are never touched.

Run from the instance root: dex-sync (or bin/dex sync)
"""

import os
from importlib import resources
from pathlib import Path

ROOT = Path.cwd()


def _write_if_changed(dest: Path, content: str, changed: list) -> None:
    if dest.exists() and dest.read_text() == content:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    changed.append(str(dest.relative_to(ROOT)))


def main() -> None:
    tpl = resources.files("dex_engine") / "templates"
    changed: list = []
    skills = tpl / "skills"
    for skill in skills.iterdir():
        if not skill.is_dir():
            continue
        for src in skill.iterdir():
            if src.is_file():
                _write_if_changed(ROOT / ".claude" / "skills" / skill.name / src.name,
                                  src.read_text(), changed)
    _write_if_changed(ROOT / "bin" / "dex", (tpl / "dex").read_text(), changed)
    os.chmod(ROOT / "bin" / "dex", 0o755)
    old_shim = ROOT / "bin" / "kb"
    if old_shim.exists():
        old_shim.unlink()
        changed.append("bin/kb (removed — renamed to bin/dex)")
    _write_if_changed(ROOT / ".gitattributes", (tpl / "gitattributes").read_text(), changed)
    if changed:
        print("synced from engine:")
        for c in changed:
            print(f"  {c}")
        print("review + commit these.")
    else:
        print("instance machinery already current.")


if __name__ == "__main__":
    main()
