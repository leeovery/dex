"""Directive 2: the README rewritten from the engine's template, linking the lens.

An instance's README used to mirror its scope list. What the instance
reads for is ``lens.md`` now, after directive 1, and the template's lens
line links to it, or names a general knowledge dex when directive 1 left
no ``lens.md``. The session replaces the README once with the template
rendered for this instance, the text its materials carry, and names in the
commit whatever the old README held beyond it.
"""

from collections.abc import Callable
from importlib.resources.abc import Traversable
from pathlib import Path

from dex_engine import seeds
from dex_engine.origin import origin_url
from dex_engine.template import bundled_template

__all__ = ["INTENT", "check", "materials", "unmet"]

INTENT = "Rewrite README.md from the engine's template, linking the lens"

_REWRITE = "write it with `bin/dex directive show 2 --materials > README.md`"


def check(root: Path) -> list[str]:
    """What the instance at ``root`` still lacks, read against the running engine's template."""
    return unmet(root, bundled_template())


def materials(root: Path) -> str:
    """The README rendered for this instance: the text ``README.md`` must hold."""
    return seeds.instance_readme(bundled_template(), root)


def unmet(
    root: Path,
    template: Traversable,
    *,
    origin: Callable[[Path], str | None] = origin_url,
) -> list[str]:
    """Whether ``README.md`` is exactly the README rendered for this instance.

    Exactly, because a partial or reworded rewrite is not the template, and
    the materials give the session the very text this compares against.

    Args:
        root: The instance root.
        template: The template tree holding ``README.md``.
        origin: The git seam the render reads the ``origin`` remote through.

    Returns:
        The one unmet condition, or nothing once the README matches.
    """
    try:
        found = (root / "README.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"`README.md` is missing: {_REWRITE}"]
    except (OSError, UnicodeDecodeError) as e:
        return [f"`README.md` is unreadable ({e.__class__.__name__}): {_REWRITE}"]
    if found != seeds.instance_readme(template, root, origin=origin):
        differs = "`README.md` is not the engine's template rendered for this instance"
        return [f"{differs}: {_REWRITE} and change nothing after"]
    return []
