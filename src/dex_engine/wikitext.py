"""Wikilinks: THE one ``[[link]]`` extraction every reader shares.

A ``[[name]]`` resolves by its name; the ``[[name|shown as this]]`` form a
page may be written in carries display text after the pipe, which resolves
nothing. Two layers read links out of page bodies — the serve layer's page
reads and the map compiler's graph — and an extraction that read the alias
form differently in one of them would put an edge in the graph the page
read does not show.

Imports nothing from the rest of the package, so any layer may use it.
"""

import re

__all__ = ["wikilinks"]

_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def wikilinks(body: str) -> list[str]:
    """The pages a body links to, in the order it links them, each named once.

    The name only: a ``[[name|shown as this]]`` link resolves by its name,
    and the alias is text for a human reader. A link with no name is not one.

    Args:
        body: The markdown text to read links out of.

    Returns:
        The linked names, first occurrence order, deduplicated.
    """
    names = (match[1].partition("|")[0].strip() for match in _WIKILINK_RE.finditer(body))
    return list(dict.fromkeys(name for name in names if name))
