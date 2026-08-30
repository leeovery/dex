"""The bundled instance template: the ONE place that knows where it lives.

`instance/` is force-included into the wheel at `dex_engine/instance`, so the
template a command reads always belongs to the engine version running it —
which is what makes a tag-pinned sync deterministic.

It is not on disk in a source checkout: the tree keeps `instance/` at the
repo root and only the build moves it. Every command that reads it therefore
takes a template override, and the tests pass one.
"""

from importlib import resources
from importlib.resources.abc import Traversable

__all__ = ["bundled_template"]


def bundled_template() -> Traversable:
    """The running engine's own ``instance/`` tree."""
    return resources.files("dex_engine") / "instance"
