"""Markdown composition primitives. Zero dex vocabulary.

Reports are markdown, and markdown has no geometry: the reader's client
soft-wraps, so nothing here measures a width, pads a column, wraps a line,
or shortens a value. An item id, a URL and a path reach the output whole,
which is the one guarantee every surface is built on.

Composition that is fully determined by data is computed here, in code,
once, and emitted verbatim by the caller — never re-derived
character-by-character by the model.
"""

from collections.abc import Iterable

__all__ = [
    "DETAIL_GLYPH",
    "bold",
    "bullet",
    "code",
    "detail",
    "document",
    "heading",
    "inline",
    "plural",
]

# A detail hanging off the entry above it. One glyph, so a reader — and a
# grep — can tell an entry's own line from what elaborates it.
DETAIL_GLYPH = "↳"

_INDENT = "  "
_MAX_HEADING_LEVEL = 6


def _line(text: str, what: str) -> str:
    """One single-line fragment, validated.

    A newline smuggled into a bullet would silently invent list entries, so
    it is refused where it enters rather than diagnosed from the output.
    """
    if not text or "\n" in text:
        raise ValueError(f"{what}: text must be a non-empty single line, got {text!r}")
    return text


def bold(text: str) -> str:
    """``text`` as a markdown identifier: bold, whole, never shortened."""
    return f"**{_line(text, 'bold')}**"


def code(text: str) -> str:
    """``text`` as a literal the reader can copy, type, or match."""
    return f"`{_line(text, 'code')}`"


def heading(text: str, *, level: int = 2) -> str:
    """An ATX heading. ``##`` names the report, ``###`` names a section."""
    if not 1 <= level <= _MAX_HEADING_LEVEL:
        raise ValueError(f"heading: level must be 1-{_MAX_HEADING_LEVEL}, got {level}")
    return "#" * level + " " + _line(text, "heading")


def bullet(text: str, *, depth: int = 0) -> str:
    """A list entry, indented two columns per nesting level."""
    if depth < 0:
        raise ValueError(f"bullet: depth must be >= 0, got {depth}")
    return _INDENT * depth + "- " + _line(text, "bullet")


def detail(text: str, *, depth: int = 0) -> str:
    """A detail line hanging off the entry above it."""
    if depth < 0:
        raise ValueError(f"detail: depth must be >= 0, got {depth}")
    return _INDENT * (depth + 1) + DETAIL_GLYPH + " " + _line(text, "detail")


def inline(parts: Iterable[str]) -> str:
    """Short fragments as one line, separated by the middle dot.

    The stand-in for a table of small counts: two columns of numbers read
    better as ``a 4 · b 1`` than as anything aligned.
    """
    kept = [_line(part, "inline") for part in parts if part]
    if not kept:
        raise ValueError("inline: at least one non-empty part is required")
    return " · ".join(kept)


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    """``count`` with its noun agreed — a heading states its scale in English."""
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"


def document(blocks: Iterable[str]) -> str:
    """Join blocks into one newline-terminated markdown document.

    A block is a line or a multi-line fragment; an empty block is a
    paragraph break. Runs of blank lines collapse to one and the document
    neither opens nor closes on a blank, so a surface may append a
    separator unconditionally and never think about it again.
    """
    lines: list[str] = []
    for block in blocks:
        lines.extend(line.rstrip() for line in block.split("\n"))
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + "\n" if out else ""
