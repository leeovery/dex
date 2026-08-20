"""Migration 3 — ensure ``cache/`` is gitignored on existing instances.

``cache/`` holds ephemera (in-flight audio, render payloads) and must never
be tracked; fresh instances gitignore it from birth, but instances
scaffolded before that seed existed do not — and the moment a transcription
run caches audio, the untracked files read as dirt to dex-run's dirty-tree
guard, stopping the very run that created them.

The fix is one append-only line: ``cache/`` added to ``.gitignore`` when no
line already ignores the directory. Provably safe (appending an ignore
pattern can't untrack anything, and the file is created when absent) and
idempotent (present in any spelling → no-op). ``.gitignore`` is
instance-owned, so the action line tells the owner their file was touched
rather than editing it silently.
"""

import datetime
from collections.abc import Callable
from pathlib import Path

from dex_engine.pipeline.types import MigrationReport, Skipped

__all__ = ["CacheGitignore", "build"]

NUMBER = 3
INTENT = "gitignore cache/: ephemera (in-flight audio, render payloads) must never read as dirt"

# Any of these lines already ignores the directory — appending another
# would be noise, not safety.
_CACHE_LINES = frozenset({"cache/", "cache", "/cache/", "/cache"})


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — the shared build signature; nothing here is dated
    engine_version: str,  # noqa: ARG001 — nothing here is version-stamped
) -> "CacheGitignore":
    """Build migration 3 (the shared build signature; no state is stamped)."""
    return CacheGitignore()


class CacheGitignore:
    """Migration 3: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def apply(self, root: Path) -> MigrationReport:
        """Append ``cache/`` to ``.gitignore`` unless a line already covers it.

        Args:
            root: The instance root.

        Returns:
            The report — one action when the line was added (naming the
            instance-owned file for the owner), empty when already covered.
        """
        gitignore = root / ".gitignore"
        text = ""
        if gitignore.exists():
            try:
                text = gitignore.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return MigrationReport(
                    skipped=[
                        Skipped(
                            what=".gitignore",
                            why="not UTF-8 — add a `cache/` line by hand",
                        )
                    ]
                )
        if any(line.strip() in _CACHE_LINES for line in text.split("\n")):
            return MigrationReport()
        addition = "cache/\n" if not text or text.endswith("\n") else "\ncache/\n"
        with gitignore.open("a", encoding="utf-8") as f:
            f.write(addition)
        action = (
            "gitignore: cache/ appended — ephemera (in-flight audio, render "
            "payloads) must not read as a dirty tree; .gitignore is "
            "instance-owned, so noting the addition for the owner"
        )
        return MigrationReport(actions=[action])
