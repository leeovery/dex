"""Layout kernel, mid-migration: markdown composition beside the old geometry.

Zero dex vocabulary. Layout that is fully determined by data is
computed here, in code, once, and emitted verbatim by the caller — never
re-derived character-by-character by the model. THE wrap-budget bug lives
here and only here: a wrap budget is always the width minus every prefix
column, never the bare width.

Ported in spirit from the agentic-workflows render kernel.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

__all__ = [
    "DEFAULT_WIDTH",
    "DETAIL_GLYPH",
    "TreeNode",
    "bold",
    "bullet",
    "code",
    "detail",
    "document",
    "fill_to",
    "heading",
    "inline",
    "kv_block",
    "plural",
    "table",
    "tree",
    "wrap",
    "wrap_with_prefix",
]


DEFAULT_WIDTH = 72


def fill_to(head: str, fill_char: str, width: int) -> str:
    """Fill ``head`` out to ``width`` with ``fill_char``.

    A head already at or past the width is returned unchanged — never
    truncated, never a negative repeat.
    """
    if len(fill_char) != 1:
        raise ValueError(f"fill_to: fill_char must be a single character, got {fill_char!r}")
    deficit = width - len(head)
    return head + fill_char * deficit if deficit > 0 else head


def wrap(text: str, budget: int) -> list[str]:
    """Greedy word-wrap ``text`` into segments no wider than ``budget`` columns.

    A word longer than the budget is hard-split, so a long unbroken token can
    never overflow. Segments carry no trailing spaces; empty text yields one
    empty segment.

    Args:
        text: The text to wrap; runs of whitespace collapse to single spaces.
        budget: Maximum segment width, a positive integer.

    Returns:
        The wrapped segments, at least one.

    Raises:
        ValueError: ``budget`` is not a positive integer.
    """
    if budget < 1:
        raise ValueError(f"wrap: budget must be a positive integer, got {budget}")
    lines: list[str] = []
    line = ""
    for token in text.split():
        word = token
        while len(word) > budget:
            # Hard-split an oversized token across as many lines as needed.
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:budget])
            word = word[budget:]
        if not line:
            line = word
        elif len(line) + 1 + len(word) <= budget:
            line += " " + word
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [""]


def wrap_with_prefix(
    text: str,
    *,
    width: int = DEFAULT_WIDTH,
    prefix: str = "",
    hang: int = 0,
) -> list[str]:
    """Wrap ``text`` under ``prefix``, keeping prefix + text within ``width``.

    ``prefix`` is the gutter/indent string applied to every line. ``hang``
    indents continuation lines by that many extra columns, so a paragraph
    opening with a marker wraps under its text rather than under the marker.
    The budget subtracts the hang from every line, not just continuations —
    one unused column on the first line buys arithmetic that cannot overflow.

    Raises:
        ValueError: ``hang`` is negative, or the prefix and hang leave no
            room within the width.
    """
    if hang < 0:
        # A negative hang would INCREASE the wrap budget past the width — the
        # one overflow this module exists to make impossible.
        raise ValueError(f"wrap_with_prefix: hang must be >= 0, got {hang}")
    budget = width - len(prefix) - hang
    if budget < 1:
        raise ValueError(
            f"wrap_with_prefix: prefix ({len(prefix)}) + hang ({hang}) "
            f"leave no room within width {width}"
        )
    continuation = prefix + " " * hang
    return [(prefix if i == 0 else continuation) + seg for i, seg in enumerate(wrap(text, budget))]


def table(
    rows: list[list[str]],
    *,
    header: list[str] | None = None,
    aligns: list[str] | None = None,
    gap: int = 2,
    indent: int = 0,
) -> str:
    """Render rows as columns padded to one shared width per column.

    Args:
        rows: Cell rows; every row needs the same column count. Cells must be
            single-line.
        header: Optional header row, underlined with a rule per column.
        aligns: Per-column ``"l"`` / ``"r"``; defaults to all left.
        gap: Columns of space between table columns.
        indent: Columns of space before every line.

    Returns:
        The rendered table, newline-terminated, no trailing spaces.

    Raises:
        ValueError: No rows, ragged rows, a multi-line cell, a bad align, or
            a negative gap/indent.
    """
    if gap < 0 or indent < 0:
        raise ValueError(f"table: gap and indent must be >= 0, got gap={gap}, indent={indent}")
    if not rows:
        raise ValueError("table: rows must be non-empty")
    columns = len(rows[0])
    all_rows = ([header] if header else []) + rows
    for row in all_rows:
        if len(row) != columns:
            raise ValueError(f"table: ragged row {row!r} — expected {columns} column(s)")
        for cell in row:
            if "\n" in cell:
                raise ValueError(f"table: cells must be single-line, got {cell!r}")
    col_aligns = aligns or ["l"] * columns
    if len(col_aligns) != columns or any(a not in ("l", "r") for a in col_aligns):
        raise ValueError(f"table: aligns must be {columns} of 'l'/'r', got {col_aligns!r}")
    widths = [max(len(row[c]) for row in all_rows) for c in range(columns)]

    def render_row(row: list[str]) -> str:
        cells = [
            cell.rjust(widths[c]) if col_aligns[c] == "r" else cell.ljust(widths[c])
            for c, cell in enumerate(row)
        ]
        return (" " * indent + (" " * gap).join(cells)).rstrip()

    lines = []
    if header:
        lines.append(render_row(header))
        lines.append(render_row(["─" * w for w in widths]))
    lines.extend(render_row(row) for row in rows)
    return "\n".join(lines) + "\n"


def kv_block(
    pairs: list[tuple[str, str]],
    *,
    width: int = DEFAULT_WIDTH,
    gap: int = 2,
    indent: int = 0,
) -> str:
    """Render key-value pairs with keys padded to one shared column.

    Values word-wrap within ``width``; continuation lines align under the
    value column. An empty value renders the bare key.

    Raises:
        ValueError: No pairs, a multi-line key, a negative gap/indent, or a
            key column that leaves no room for values within the width.
    """
    if gap < 0 or indent < 0:
        raise ValueError(f"kv_block: gap and indent must be >= 0, got gap={gap}, indent={indent}")
    if not pairs:
        raise ValueError("kv_block: pairs must be non-empty")
    if any("\n" in key for key, _ in pairs):
        raise ValueError("kv_block: keys must be single-line")
    key_width = max(len(key) for key, _ in pairs)
    budget = width - indent - key_width - gap
    if budget < 1:
        widest = max(pairs, key=lambda pair: len(pair[0]))[0]
        # Checked for every pair — an empty value must not smuggle an
        # over-wide key column past the width guarantee.
        raise ValueError(
            f"kv_block: key {widest!r} plus indent/gap leave no room for values "
            f"within width {width}"
        )
    head_width = indent + key_width + gap
    lines: list[str] = []
    for key, value in pairs:
        head = " " * indent + key.ljust(key_width) + " " * gap
        if not value.strip():
            lines.append(head.rstrip())
            continue
        wrapped = wrap(value, budget)
        lines.append((head + wrapped[0]).rstrip())
        lines.extend((" " * head_width + seg).rstrip() for seg in wrapped[1:])
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True, kw_only=True)
class TreeNode:
    """One node of a simple tree: a single-line title plus children."""

    title: str
    children: list["TreeNode"] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.title or "\n" in self.title:
            raise ValueError(f"TreeNode: title must be a non-empty single line, got {self.title!r}")


def tree(nodes: list[TreeNode], *, indent: int = 0) -> str:
    """Render nodes as a continuous-gutter tree.

    Branch glyphs ``├─``/``└─``, a ``│`` gutter running unbroken at every
    depth, the last sibling dropping the gutter so nothing dangles below
    ``└─``.

    Raises:
        ValueError: ``nodes`` is empty, or ``indent`` is negative.
    """
    if indent < 0:
        raise ValueError(f"tree: indent must be >= 0, got {indent}")
    if not nodes:
        raise ValueError("tree: nodes must be non-empty")
    lines: list[str] = []
    _tree_lines(nodes, " " * indent, lines)
    return "\n".join(lines) + "\n"


def _tree_lines(nodes: list[TreeNode], prefix: str, out: list[str]) -> None:
    for i, node in enumerate(nodes):
        last = i == len(nodes) - 1
        out.append(prefix + ("└─ " if last else "├─ ") + node.title)
        if node.children:
            _tree_lines(node.children, prefix + ("   " if last else "│  "), out)


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
