"""Atomic file writes: THE one same-dir-temp-then-replace implementation.

Every state write shares one discipline — a temp file in the target's own
directory (same filesystem, so the replace is a rename), finished bytes
first, then one atomic replace. A crash mid-write never truncates or loses
the original, and a failed write leaves no temp orphan behind. The purges'
line-dropping rewrite of the append-only state files is built on it here,
once, so every purge edits a record file the same way.

Imports nothing from the rest of the package, so any layer may use it.
"""

import contextlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

__all__ = ["TEMP_SUFFIX", "drop_lines", "write_bytes", "write_text"]

# What a half-finished write is called: ``<target name>.<random>.tmp``, in
# the target's own directory. The finally-clause below clears it, but a
# process killed mid-write leaves one standing — so anything that scans a
# directory for real files has to know the shape, and reads it from here.
TEMP_SUFFIX = ".tmp"


def write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, UTF-8 encoded."""
    write_bytes(path, text.encode("utf-8"))


def write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=TEMP_SUFFIX)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        tmp.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def drop_lines(path: Path, drop: Callable[[str], bool]) -> int:
    """Rewrite a line-per-record file without the lines ``drop`` names.

    Every other line is carried byte for byte, blank lines aside, and a
    file with nothing to drop is not rewritten at all, so a purge that
    removes nothing leaves the union-merged history untouched.

    Args:
        path: The file; it must exist.
        drop: Whether one line goes. It sees every non-blank line, so it
            has to keep any line it cannot read: a purge must never become
            incidental data loss.

    Returns:
        How many lines were dropped.
    """
    kept: list[str] = []
    dropped = 0
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        if drop(line):
            dropped += 1
        else:
            kept.append(line)
    if dropped:
        write_text(path, "".join(line + "\n" for line in kept))
    return dropped
