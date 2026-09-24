"""The lens check: whether ``lens.md`` states what the instance reads for.

The lens is the owner's free-form statement, and nothing in the engine
parses it: a session reads it whole, as one coherent statement, and every
judgment in a run reads content through it. An instance with no lens at all,
``lens.md`` missing or empty, is a general knowledge dex that reads for
anything, which is a legitimate way to run one and never a fault. A
``lens.md`` still holding the seed's placeholder lines reads the same way,
because a placeholder line is never read as a lens, and so does one that
will not read as text. Those two are worth a note, since the owner may
think the file says something: lint and the sync report carry it, and
neither ever fails on it.
"""

import re
from dataclasses import dataclass
from importlib.resources.abc import Traversable

from .pipeline.types import Instance

__all__ = ["LensReading", "read_lens"]

_PLACEHOLDER_RE = re.compile(r"<[^<>]+>")

_GENERAL = "this dex reads as general knowledge"


@dataclass(frozen=True, slots=True, kw_only=True)
class LensReading:
    """What ``lens.md`` holds, as each of its readers needs it.

    Attributes:
        text: The lens, stripped, when ``lens.md`` states one; ``None`` when
            the instance reads as general knowledge.
        finding: What keeps ``lens.md`` from stating a lens (missing,
            empty, unreadable, or placeholder lines left), or ``None`` when
            it states one.
        note: The informational note on a ``lens.md`` that is there and
            still reads as general knowledge (placeholder lines left, or
            unreadable), or ``None``. Missing or empty is no note at all.
    """

    text: str | None
    finding: str | None
    note: str | None


def read_lens(instance: Instance, template: Traversable) -> LensReading:
    """Read the instance's ``lens.md`` against the seed's placeholder lines.

    The seed's headings are prompts, never a schema, so a lens that renamed
    or deleted them, or never had any, states a lens once no placeholder
    line is left.

    Args:
        instance: The instance.
        template: The template tree whose ``lens.md`` is the seed, the one
            source of the placeholder lines.

    Returns:
        The reading: the lens when there is one, otherwise the finding, and
        the note when the file is there but reads as general knowledge.
    """
    try:
        text = instance.lens_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return LensReading(text=None, finding="`lens.md` is missing", note=None)
    except (OSError, UnicodeDecodeError) as e:
        finding = f"`lens.md` is unreadable ({e.__class__.__name__})"
        return _noted(finding, "until it can be read (or the file deleted)")
    if not text.strip():
        return LensReading(text=None, finding="`lens.md` is empty", note=None)
    held = {line.strip() for line in text.splitlines()}
    left = [line for line in _placeholders(template) if line in held]
    if left:
        named = ", ".join(f"`{line}`" for line in left)
        finding = f"`lens.md` still holds the seed's placeholder text ({named})"
        return _noted(finding, "until the placeholders are replaced (or the file deleted)")
    return LensReading(text=text.strip(), finding=None, note=None)


def _noted(finding: str, until: str) -> LensReading:
    """A ``lens.md`` that is there and reads as general knowledge ``until`` it changes."""
    return LensReading(text=None, finding=finding, note=f"{finding}: {until}, {_GENERAL}")


def _placeholders(template: Traversable) -> list[str]:
    """The seed's placeholder lines, in seed order: each line that is only ``<...>``."""
    seed = (template / "lens.md").read_text(encoding="utf-8")
    lines = (line.strip() for line in seed.splitlines())
    return [line for line in lines if _PLACEHOLDER_RE.fullmatch(line)]
