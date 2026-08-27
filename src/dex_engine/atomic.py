"""Atomic file writes: THE one same-dir-temp-then-replace implementation.

Every state write shares one discipline — a temp file in the target's own
directory (same filesystem, so the replace is a rename), finished bytes
first, then one atomic replace. A crash mid-write never truncates or loses
the original, and a failed write leaves no temp orphan behind.

Imports nothing from the rest of the package, so any layer may use it.
"""

import contextlib
import os
import tempfile
from pathlib import Path

__all__ = ["TEMP_SUFFIX", "write_bytes", "write_text"]

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
