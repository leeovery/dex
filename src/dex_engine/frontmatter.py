"""Frontmatter scalars: THE one unquoting rule every reader shares.

Two hands write frontmatter in this tree. The pipeline writes it
canonically, quoting where it must; a session writes digests freehand, and
nothing tells it not to quote — ``signal: "high"`` is the same judgment as
``signal: high``. A reader that took the quotes off in one place and not
another would call a correct file broken, so every reader takes them off
here.

Imports nothing from the rest of the package, so any layer may use it.
"""

import contextlib
import json

__all__ = ["unquote"]


def unquote(value: str) -> str:
    """The scalar a frontmatter value means, with its quotes taken off.

    Double quotes are read as JSON — YAML's escapes inside them are the
    same ones. Single quotes are YAML's literal form, whose only escape is
    ``''`` for one quote. An unquoted value stands as written.

    Args:
        value: The text after the ``key:``, already stripped.

    Returns:
        The scalar, unquoted.
    """
    if len(value) > 1 and value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.startswith('"'):
        # On a malformed quote the raw text stands — a pointer beats a crash.
        with contextlib.suppress(json.JSONDecodeError):
            return str(json.loads(value))
    return value
