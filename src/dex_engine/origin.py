"""The instance's ``origin`` remote, read as the GitHub repository it names.

The inbox's release checks and the README's join prompt both need the
``owner/repo`` an instance lives at on GitHub, and both read it from
``origin``: a local-only instance has no origin, and one hosted anywhere
else names no GitHub repository.
"""

import re
import subprocess
from pathlib import Path

__all__ = ["github_repo", "origin_url"]

_GITHUB_RE = re.compile(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?$")


def github_repo(url: str) -> str | None:
    """The ``owner/repo`` a remote URL names on GitHub, or ``None`` when it names none.

    Both of git's spellings are read, ``git@github.com:owner/repo.git`` and
    ``https://github.com/owner/repo``, with or without the ``.git``.
    """
    match = _GITHUB_RE.search(url.strip())
    return match.group(1) if match else None


def origin_url(root: Path) -> str | None:
    """The URL of the ``origin`` remote of the repository at ``root``.

    Returns:
        The URL, or ``None`` when there is no origin to read: no such
        remote, no repository at ``root``, or no git to ask.
    """
    try:
        done = subprocess.run(  # noqa: S603 — engine-built args, no shell
            ["git", "-C", str(root), "remote", "get-url", "origin"],  # noqa: S607 — git resolves via PATH like every dev tool
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace").strip()
