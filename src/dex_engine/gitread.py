"""Read-only git queries against a repository: what git printed, or nothing.

A query git cannot answer, because there is no repository, no such object
or remote, or no git at all, reads as ``None``, and the caller decides what
that absence means.
"""

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

__all__ = ["checkout_bytes", "git_output"]


def git_output(root: Path, args: Sequence[str]) -> str | None:
    """What ``git -C root <args>`` printed, or ``None`` when git could not answer.

    Decoded tolerantly: a stored file or a remote URL that is not UTF-8
    still reads, with each undecodable byte replaced.
    """
    printed = _git(root, args)
    return None if printed is None else printed.decode("utf-8", "replace")


def checkout_bytes(root: Path, revision: str, path: str) -> bytes | None:
    """The bytes a checkout of ``path`` at ``revision`` would write, or ``None``.

    Read through the working tree's filters, so an LFS-tracked file comes
    back as its content, from the local object store or else from the LFS
    remote. Content neither holds comes back as the committed pointer,
    which a later ``git add`` stages as the same blob it was.
    """
    return _git(
        root,
        ["cat-file", "--filters", f"{revision}:{path}"],
        env={"GIT_LFS_SKIP_DOWNLOAD_ERRORS": "1"},
    )


def _git(root: Path, args: Sequence[str], *, env: Mapping[str, str] | None = None) -> bytes | None:
    try:
        done = subprocess.run(  # noqa: S603 — engine-built args, no shell
            ["git", "-C", str(root), *args],  # noqa: S607 — git resolves via PATH like every dev tool
            capture_output=True,
            check=False,
            timeout=60,
            env=None if env is None else {**os.environ, **env},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout
