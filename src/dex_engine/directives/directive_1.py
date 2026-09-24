"""Directive 1: the owner's CLAUDE.md, rehomed now that the engine owns the file.

An instance's CLAUDE.md used to be the owner's: a title, the list of what
the instance covers, and for some the facts a Discord pull needs. Sync now
replaces it with the engine's once git history holds it, and the session
reads the owner's version back from that history and gives every part a
home: what the instance reads for becomes ``lens.md``, the Discord server
and channels become the ``discord`` key of ``state/config.json``, and the
rest is named in the commit that removes it.
"""

from importlib.resources.abc import Traversable
from pathlib import Path

from dex_engine import seeds
from dex_engine.lens import lens_finding
from dex_engine.pipeline.types import Config, Instance
from dex_engine.template import bundled_template

__all__ = ["INTENT", "check", "materials", "unmet"]

INTENT = "Rehome the owner's CLAUDE.md: its scope becomes lens.md, its Discord facts become config"


def check(root: Path) -> list[str]:
    """What the instance at ``root`` still lacks, read against the running engine's template."""
    return unmet(root, bundled_template())


def materials(root: Path) -> str:
    """The seed lens with this instance's name, the layout the session starts from."""
    return seeds.lens(bundled_template(), root.name)


def unmet(root: Path, template: Traversable) -> list[str]:
    """Every condition not yet met: a lens that states nothing, a config that does not parse.

    Args:
        root: The instance root.
        template: The template tree whose ``lens.md`` is the seed.

    Returns:
        One message per unmet condition, empty once the directive is done.
    """
    instance = Instance(root=root)
    findings = (lens_finding(instance, template), _config_finding(instance))
    return [finding for finding in findings if finding is not None]


def _config_finding(instance: Instance) -> str | None:
    try:
        Config.load(instance.config_path)
    except (OSError, ValueError) as e:
        return f"`state/config.json` does not parse: {e}"
    return None
