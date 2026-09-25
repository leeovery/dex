"""The owner's two seeds, ``README.md`` and ``lens.md``, rendered for one instance.

Both are template files with the instance's name to fill in, and both
belong to the owner once written, so sync never touches either. ``dex-new``
writes them when it creates an instance, and the directives that bring an
older instance into the same shape render them here too, so a new instance
and a converted one start from the same text. An instance's name is its
root directory's name. The README's lens line links ``lens.md`` when the
instance has one and otherwise says the instance is a general knowledge
dex, so a README never links a file that is not there.
"""

import re
from collections.abc import Callable
from importlib.resources.abc import Traversable
from pathlib import Path

from .origin import github_repo, origin_url
from .pipeline.types import Instance

__all__ = [
    "GENERAL_LINE",
    "LENS_LINE",
    "LENS_PLACEHOLDER",
    "NAME_PLACEHOLDER",
    "REPO_PLACEHOLDER",
    "instance_readme",
    "lens",
    "readme",
]

NAME_PLACEHOLDER = "<instance name>"
REPO_PLACEHOLDER = "<owner>/<repo>"
LENS_PLACEHOLDER = "<what this dex reads for>"

# The two lines the README's lens placeholder becomes, wrapped as the
# template around them is.
LENS_LINE = (
    "What this dex reads for is its lens: [`lens.md`](./lens.md). Ask Claude to\n"
    "change it whenever you like."
)
GENERAL_LINE = (
    "This dex has no lens, so it is a general knowledge dex that reads anything\n"
    "shared into it. Ask Claude to give it one whenever you like."
)

_HEADING_RE = re.compile(r"(#{1,6})\s")


def lens(template: Traversable, name: str) -> str:
    """The seed lens for the instance called ``name``, its placeholder lines still to fill."""
    return _named(template, "lens.md", name)


def readme(template: Traversable, name: str) -> str:
    """The README ``dex-new`` writes: named, linking the lens it seeds, its repo left to fill."""
    return _readme(template, name, has_lens=True)


def instance_readme(
    template: Traversable,
    root: Path,
    *,
    origin: Callable[[Path], str | None] = origin_url,
) -> str:
    """The README for the existing instance at ``root``.

    The lens line links ``lens.md`` when the instance has the file, and
    otherwise names a general knowledge dex. The join prompt names the
    GitHub repository the ``origin`` remote points at. An instance with no
    origin, or an origin elsewhere, has no repository a second machine
    could clone, so the section holding the prompt goes whole, as setup
    deletes it for a local-only instance.

    Args:
        template: The template tree holding ``README.md``.
        root: The instance root, whose directory name is the instance's.
        origin: The git seam: the ``origin`` remote's URL, or ``None``.

    Returns:
        The README, ending in one newline.
    """
    text = _readme(template, root.name, has_lens=Instance(root=root).lens_path.exists())
    url = origin(root)
    repo = None if url is None else github_repo(url)
    if repo is None:
        return _without_section(text, REPO_PLACEHOLDER)
    return text.replace(REPO_PLACEHOLDER, repo)


def _readme(template: Traversable, name: str, *, has_lens: bool) -> str:
    line = LENS_LINE if has_lens else GENERAL_LINE
    return _named(template, "README.md", name).replace(LENS_PLACEHOLDER, line)


def _named(template: Traversable, rel: str, name: str) -> str:
    return (template / rel).read_text(encoding="utf-8").replace(NAME_PLACEHOLDER, name)


def _without_section(text: str, marker: str) -> str:
    """``text`` without the markdown section holding ``marker``.

    The section runs from the nearest heading above the marker to the next
    heading of the same or a higher level, or to the end of the text.
    """
    lines = text.splitlines(keepends=True)
    at = next((i for i, line in enumerate(lines) if marker in line), None)
    if at is None:
        return text
    start = max(i for i in range(at + 1) if _level(lines[i]))
    end = next(
        (i for i in range(start + 1, len(lines)) if 0 < _level(lines[i]) <= _level(lines[start])),
        len(lines),
    )
    return "".join(lines[:start] + lines[end:]).rstrip("\n") + "\n"


def _level(line: str) -> int:
    """A heading line's level, ``0`` for a line that is not a heading."""
    match = _HEADING_RE.match(line)
    return len(match.group(1)) if match else 0
