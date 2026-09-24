"""Read-only git queries against a repository: what git printed, or nothing.

A query git cannot answer, because there is no repository, no such object
or remote, or no git at all, reads as ``None``, and the caller decides what
that absence means.
"""

import subprocess
from collections.abc import Sequence
from pathlib import Path

__all__ = ["git_output"]


def git_output(root: Path, args: Sequence[str]) -> str | None:
    """What ``git -C root <args>`` printed, or ``None`` when git could not answer.

    Decoded tolerantly: a stored file or a remote URL that is not UTF-8
    still reads, with each undecodable byte replaced.
    """
    try:
        done = subprocess.run(  # noqa: S603 — engine-built args, no shell
            ["git", "-C", str(root), *args],  # noqa: S607 — git resolves via PATH like every dev tool
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace")
