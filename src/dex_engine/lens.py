"""The lens check: whether ``lens.md`` states what the instance reads for.

The lens is the owner's free-form statement, and nothing in the engine
parses it: a session reads it whole, as one coherent statement, and every
judgment in a run reads content through it. What code can ask is whether a
statement is there at all, because a blank lens fails silently: no
judgment can tell that it is reading through nothing. Lint fails on the
answer, and sync's report carries it so it shows before any work starts.
"""

import re
from importlib.resources.abc import Traversable

from .pipeline.types import Instance

__all__ = ["lens_finding"]

_PLACEHOLDER_RE = re.compile(r"<[^<>]+>")


def lens_finding(instance: Instance, template: Traversable) -> str | None:
    """What is wrong with the instance's lens, or ``None`` when it states one.

    A lens is wrong when ``lens.md`` is missing, unreadable or empty, or
    when it still holds any of the seed's placeholder lines verbatim. The
    seed's headings are prompts, never a schema, so a lens that renamed or
    deleted them, or never had any, passes once no placeholder line is left.

    Args:
        instance: The instance.
        template: The template tree whose ``lens.md`` is the seed, the one
            source of the placeholder lines.

    Returns:
        The finding, naming the file and what is wrong with it, or ``None``.
    """
    try:
        text = instance.lens_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "`lens.md` is missing"
    except (OSError, UnicodeDecodeError) as e:
        return f"`lens.md` is unreadable ({e.__class__.__name__})"
    if not text.strip():
        return "`lens.md` is empty"
    held = {line.strip() for line in text.splitlines()}
    left = [line for line in _placeholders(template) if line in held]
    if left:
        return "`lens.md` still holds the seed's placeholder text: " + ", ".join(
            f"`{line}`" for line in left
        )
    return None


def _placeholders(template: Traversable) -> list[str]:
    """The seed's placeholder lines, in seed order: each line that is only ``<...>``."""
    seed = (template / "lens.md").read_text(encoding="utf-8")
    lines = (line.strip() for line in seed.splitlines())
    return [line for line in lines if _PLACEHOLDER_RE.fullmatch(line)]
