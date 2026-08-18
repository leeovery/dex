"""Sync engine-managed machinery into the current volume.

Refreshes from the engine's bundled templates: .claude/skills/dex-*,
bin/kb, .github/workflows/inbox.yml. Volume-owned files (CLAUDE.md, README,
content) are never touched.

Run from the volume root: kb-sync (or bin/kb sync)
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
        src = skill / "SKILL.md"
        if src.is_file():
            _write_if_changed(ROOT / ".claude" / "skills" / skill.name / "SKILL.md",
                              src.read_text(), changed)
    _write_if_changed(ROOT / "bin" / "kb", (tpl / "kb").read_text(), changed)
    os.chmod(ROOT / "bin" / "kb", 0o755)
    _write_if_changed(ROOT / ".github" / "workflows" / "inbox.yml",
                      (tpl / "inbox-caller.yml").read_text(), changed)
    if changed:
        print("synced from engine:")
        for c in changed:
            print(f"  {c}")
        print("review + commit these.")
    else:
        print("volume machinery already current.")


if __name__ == "__main__":
    main()
