"""The enrichment-file format: one module owning render and parse.

An enrichment file is machine-written markdown — a frontmatter fence of
``key: value`` lines over a body — and its render and parse are inverses:
the renderer JSON-quotes exactly the scalars a YAML 1.1 reader would
retype or restructure, and every reader takes those quotes off through the
one shared unquoting rule (:mod:`dex_engine.frontmatter`, which the
digest-side readers use too). The writer grew up in ``run.py`` and the
readers in ``transcribe.py``; the format is one contract, so both halves
live here.

Two readers over one field parser, and their unterminated-fence contracts
differ deliberately:

- :func:`read_enrichment` (fields + body) answers a fence that opens and
  never closes with NO fields, not with the whole file read as
  frontmatter — every caller decides on a field it looks up, and
  transcript prose parsed as fields answers those lookups with nonsense.
- :func:`read_enrichment_fields` (fields alone, body never read) RAISES
  on that same shape — a scan that exists to report an unreadable file
  has to tell it apart from a clean one; a drain that only has to avoid
  being fooled does not.
"""

import datetime
import json
import re
from pathlib import Path

from dex_engine import frontmatter

__all__ = [
    "mask_fetched",
    "read_enrichment",
    "read_enrichment_fields",
    "render_enrichment",
]

_YAML_UNSAFE = ":#[]{}&*!|>%@`\"'"

# Values a YAML 1.1 reader (Obsidian's among them) would retype away from
# the intended string: boolean/null words, int/float lookalikes in every
# 1.1 spelling (sign, underscores, hex/octal/binary, exponent, .inf/.nan),
# and date/timestamp lookalikes. Leading `-`/`?`/`,` are indicator
# characters that make the line invalid or restructure it. All are
# emitted JSON-quoted.
_YAML_KEYWORDS = frozenset({"true", "false", "yes", "no", "on", "off", "null", "~"})
_YAML_NUMBER_RE = re.compile(
    r"[-+]?(?:\.inf|\.nan|0x[0-9a-f_]+|0b[01_]+|0o?[0-7_]+"
    r"|[0-9][0-9_]*(?:\.[0-9_]*)?(?:e[-+]?[0-9]+)?"
    r"|\.[0-9][0-9_]*(?:e[-+]?[0-9]+)?)",
    re.IGNORECASE,
)
# The 1.1 timestamp resolver's shape, mirrored: a bare YYYY-MM-DD retypes
# to a date and a full timestamp to a datetime — and a shape-matching but
# calendar-invalid value (2026-08-99) makes those readers RAISE, so the
# match is on shape alone, never calendar validity.
_YAML_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}"
    r"(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?"
    r"(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?"
)


def render_enrichment(
    url: str, fetched: datetime.date, meta: dict[str, str | int | None], body: str
) -> str:
    """One enrichment file's full text: fenced frontmatter over the body.

    ``url`` and ``fetched`` lead; ``meta`` follows in insertion order with
    ``None``/empty values dropped. The parse side reads this back
    field-for-field.

    Args:
        url: The unit's canonical URL (or repo-path work key).
        fetched: The run's date stamp.
        meta: The driver/drain metadata for the frontmatter.
        body: The markdown body.

    Returns:
        The file content, trailing newline included.
    """
    lines = ["---", f"url: {_yaml_value(url)}", f"fetched: {fetched.isoformat()}"]
    for key, value in meta.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {_yaml_value(value)}")
    lines.extend(["---", "", body.strip(), ""])
    return "\n".join(lines)


def _yaml_value(value: str | int) -> str:
    if isinstance(value, int):
        return str(value)
    needs_quoting = (
        value != value.strip()
        or "\n" in value
        or "\t" in value  # a tab in a plain scalar invalidates the whole block
        or value[:1] in ("-", "?", ",")
        or any(ch in value for ch in _YAML_UNSAFE)
        or value.lower() in _YAML_KEYWORDS
        or _YAML_NUMBER_RE.fullmatch(value) is not None
        or _YAML_TIMESTAMP_RE.fullmatch(value) is not None
    )
    return json.dumps(value, ensure_ascii=False) if needs_quoting else value


def mask_fetched(content: str) -> str:
    """Drop the ``fetched:`` stamp so rerun byte-compares see real change only.

    Only the frontmatter head is masked — a body line that happens to start
    with ``fetched:`` is content and must count as change.
    """
    head, sep, rest = content.partition("\n---\n")
    masked = "\n".join(line for line in head.split("\n") if not line.startswith("fetched: "))
    return masked + sep + rest


def read_enrichment(path: Path) -> tuple[dict[str, str], str]:
    """Parse one enrichment file into (frontmatter fields, body).

    Args:
        path: The enrichment markdown file.

    A fence that opens and never closes yields no fields, not the whole
    file read as frontmatter — every caller decides on a field it looks up,
    and transcript prose parsed as fields answers those lookups with
    nonsense. ``read_enrichment_fields`` raises on the same shape instead,
    because a scan that exists to report an unreadable file has to tell it
    apart from a clean one; a drain that only has to avoid being fooled
    does not.

    Returns:
        The fields (JSON-quoted values unquoted) and the stripped body.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text.strip()
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        return {}, text.strip()
    return _frontmatter_fields(head.split("\n")), body.strip()


def read_enrichment_fields(path: Path) -> dict[str, str]:
    """Parse one enrichment file's frontmatter without reading its body.

    Bodies run to whole transcripts; a scan that only wants the
    frontmatter stops at the closing fence.

    Args:
        path: The enrichment markdown file.

    Returns:
        The fields (quoted values unquoted); empty for a file that opens
        with no frontmatter fence at all.

    Raises:
        ValueError: The file opens a fence and never closes it — the shape
            an interrupted write leaves behind. Empty fields would say
            "read fine, no markers", and the caller cannot tell the
            difference it has to report.
    """
    head: list[str] = []
    with path.open(encoding="utf-8") as f:
        if f.readline().rstrip("\n") != "---":
            return {}
        for line in f:
            if line.rstrip("\n") == "---":
                return _frontmatter_fields(head)
            head.append(line.rstrip("\n"))
    raise ValueError("unterminated frontmatter: no closing '---' fence")


def _frontmatter_fields(lines: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        fields[key.strip()] = frontmatter.unquote(raw.strip())
    return fields
