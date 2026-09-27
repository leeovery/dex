"""The enrichment-file format: one module owning render and parse.

An enrichment file is machine-written markdown — a frontmatter fence of
``key: value`` lines over a body — and its render and parse are inverses:
the renderer JSON-quotes exactly the scalars a YAML 1.1 reader would
retype or restructure, and every reader takes those quotes off through the
one shared unquoting rule (:mod:`dex_engine.frontmatter`, which the
digest-side readers use too). The writer grew up in ``run.py`` and the
readers in ``transcribe.py``; the format is one contract, so both halves
live here. The transcript-bearing kinds' body sections belong to the same
contract — whether a body holds a transcript is said by the ``via`` field,
so the section headings and the frontmatter cannot be read apart — and
their composition and split live here too, shared by the youtube driver
and the transcribe drain. So does the one line a media description opens
with, naming the file it covers: the describe verb writes it and reads it
back, and the hand-over that moves a reading off a byte-identical copy
re-spells it.

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
from dataclasses import dataclass
from pathlib import Path

from dex_engine import atomic, frontmatter

__all__ = [
    "CAPTIONS_VIA",
    "DESCRIPTION_HEADING",
    "TRANSCRIPT_HEADING",
    "TRANSCRIPT_PROVENANCE",
    "TRANSCRIPT_SOURCES",
    "Handover",
    "described_file",
    "description_header",
    "description_section",
    "description_text",
    "descriptions_of",
    "hand_over_descriptions",
    "holds_transcript",
    "mask_fetched",
    "podcast_body",
    "post_body",
    "pre_transcript",
    "read_enrichment",
    "read_enrichment_fields",
    "render_enrichment",
    "split_transcript",
    "transcript_provenance",
    "youtube_body",
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


# ---------------------------------------------------------------------------
# The body sections of transcript-bearing files. The youtube driver writes
# the description section on its parks and its captions route; the
# transcribe drain appends what it transcribed to what the park already
# wrote, splitting the stored body on the transcript heading. Writer and
# re-reader share one composition here so they can never disagree on where
# the notes end.
# ---------------------------------------------------------------------------

DESCRIPTION_HEADING = "## Description"
TRANSCRIPT_HEADING = "## Transcript"

# The frontmatter key the transcriber stamps (the run layer writes
# via/model onto every transcript it composes). Neither park writes it, so
# it is the one fact on disk that says whether a body already holds a
# transcript — read against the transcript sources below, because other
# kinds' files carry the same key as fetch provenance.
_TRANSCRIBED_FIELD = "via"
# The stamps the drain appends to every transcript it composes, in the
# order it appends them — the transcript's provenance, never the fetch's.
TRANSCRIPT_PROVENANCE = (_TRANSCRIBED_FIELD, "model")

# The stamp youtube's captions route writes. The frontmatter, not the
# heading, says whether a body holds a transcript: the drain reads `via`
# back to find where the notes end, so a captions transcript that carried
# no `via` was read as description end to end and a later whisper drain
# appended a SECOND transcript under the first — old caption text and new
# whisper text in one file.
CAPTIONS_VIA = "captions"

# Every `via` written beside a transcript section, and only those: the
# captions route's, and each transcription provider's name, which the drain
# stamps on the transcript it composes. Elsewhere `via` is fetch provenance
# — fxtwitter, wayback, an extractor's name — and a fetched post whose text
# carries a "## Transcript" heading must never read as transcribed.
TRANSCRIPT_SOURCES = frozenset({CAPTIONS_VIA, "whisper-local", "whisper-api"})


def description_text(body: str) -> str:
    """The description a :func:`description_section` holds, or ``body`` itself without one."""
    if body.startswith(DESCRIPTION_HEADING):
        return body[len(DESCRIPTION_HEADING) :].strip()
    return body


def description_section(description: str) -> str:
    """The description as its own labelled section, or "" when there is none.

    A park writes this on its own: the description is content already
    fetched, and a video that goes private during a transcription backlog
    would otherwise take it with it. The transcript is appended to this
    same section later, never written over it.
    """
    return f"{DESCRIPTION_HEADING}\n\n{description}" if description else ""


def youtube_body(description: str, transcript: str) -> str:
    """Description + transcript sections — one youtube body shape, either route.

    The transcript is always its own labelled section, description or not:
    a re-drain splits the stored body on that heading, and a bare
    transcript would come back as "description" and be duplicated under
    itself.
    """
    if description:
        return f"{DESCRIPTION_HEADING}\n\n{description}\n\n{TRANSCRIPT_HEADING}\n\n{transcript}"
    return f"{TRANSCRIPT_HEADING}\n\n{transcript}"


def podcast_body(show_notes: str, transcript: str) -> str:
    """Show notes (from the feed) followed by the transcript section."""
    return _notes_then_transcript(show_notes, transcript)


def post_body(text: str, transcript: str) -> str:
    """An instagram or x post's attributed text followed by the transcript section."""
    return _notes_then_transcript(text, transcript)


def _notes_then_transcript(notes: str, transcript: str) -> str:
    """The park's own prose, then the transcript as its own labelled section.

    Labelled even when there is no prose in front of it: a re-drain splits
    the stored body on that heading, and a bare transcript would come back
    as notes and be duplicated under itself.
    """
    if notes:
        return f"{notes}\n\n{TRANSCRIPT_HEADING}\n\n{transcript}"
    return f"{TRANSCRIPT_HEADING}\n\n{transcript}"


def holds_transcript(fields: dict[str, str], body: str) -> bool:
    """Whether a stored body carries a transcript section a transcriber composed.

    A body only holds a transcript section if the drain composed it, and
    the frontmatter is what says so. A park's body is notes end to end,
    however many "## Transcript" lines a description or a publisher's show
    notes happen to contain: reading it by the heading alone truncated the
    notes at the author's own line, and the drain then wrote that
    truncation back to disk, losing the tail for good.

    A drained no-notes episode's body STARTS with the transcript heading,
    where a newline-anchored search would miss it.
    """
    return fields.get(_TRANSCRIBED_FIELD) in TRANSCRIPT_SOURCES and (
        _opens_with_transcript(body) or f"\n{TRANSCRIPT_HEADING}\n" in body
    )


def split_transcript(fields: dict[str, str], body: str) -> tuple[str, str] | None:
    """A stored body's (notes, transcript) halves, or None when it holds no transcript.

    Only a body that :func:`holds_transcript` is split. The split takes the
    LAST section, because the transcript is what the drain appended last.
    """
    if not holds_transcript(fields, body):
        return None
    if _opens_with_transcript(body):
        return "", body[len(TRANSCRIPT_HEADING) :].strip()
    notes, _, transcript = body.rpartition(f"\n{TRANSCRIPT_HEADING}\n")
    return notes.rstrip(), transcript.strip()


def pre_transcript(fields: dict[str, str], body: str) -> str:
    """The show-notes half of a park/output body — everything before the transcript."""
    split = split_transcript(fields, body)
    return body if split is None else split[0]


def transcript_provenance(fields: dict[str, str]) -> dict[str, str | int | None]:
    """The stamps a transcript's frontmatter carries of how it was made."""
    return {key: fields[key] for key in TRANSCRIPT_PROVENANCE if key in fields}


def _opens_with_transcript(body: str) -> bool:
    return body == TRANSCRIPT_HEADING or body.startswith(f"{TRANSCRIPT_HEADING}\n")


# ---------------------------------------------------------------------------
# A media description's first line: the one tie between a description and
# the file it covers.
# ---------------------------------------------------------------------------

# The name a description's first line carries. Written by the describe verb
# as the whole line; a description written before that verb existed opens
# the same way and runs on.
_DESCRIBED_RE = re.compile(r"^Describes\s+`([^`]+)`")
# That name alone, re-spelled when a reading moves to another file.
_BACKTICKED_RE = re.compile(r"`[^`]+`")
_DOWNLOAD_SLOT_RE = re.compile(r"media-(\d+)\.")


def description_header(of: str) -> str:
    """The first line of a description of ``of``."""
    return f"Describes `{of}`"


def described_file(path: Path) -> str | None:
    """The file a description's first line names, or None where it names none.

    The one tie between a description and the file it covers, and the
    reason a slot number proves nothing: a capture's media carries no slot
    at all. The line is read for that name rather than matched whole,
    because a description written before the describe verb existed carries
    its own prose after it.
    """
    try:
        line = path.read_text(encoding="utf-8").partition("\n")[0]
    except (OSError, UnicodeDecodeError):
        return None
    match = _DESCRIBED_RE.match(line)
    return None if match is None else match.group(1)


def descriptions_of(item_dir: Path, name: str) -> list[Path]:
    """Every description in ``item_dir`` whose first line names its file ``name``.

    Read in both spellings the field carries: the describe verb writes the
    bare name, and a description written before the verb existed wrote the
    repo path.
    """
    spellings = {name, f"enrichment/{item_dir.name}/{name}"}
    return [
        path for path in sorted(item_dir.glob("media-*.md")) if described_file(path) in spellings
    ]


@dataclass(frozen=True, slots=True, kw_only=True)
class Handover:
    """What became of a retired file's descriptions: the one moved, and how many went."""

    moved: Path | None
    dropped: int


def hand_over_descriptions(item_dir: Path, *, retired: str, kept: str) -> Handover:
    """Settle the descriptions of ``retired``, leaving because ``kept`` holds its very bytes.

    One picture, so a reading of either file reads both. Where ``kept`` has
    a description of its own, every description of ``retired`` goes. Where
    it has none, the first moves over and the rest go: renamed to the kept
    slot's ``media-<n>.md`` where that name is free, re-pointed where it
    stands otherwise, and in its first line only the backticked name
    changes, because the rest is the session's prose.

    Only for byte-identical files: a description belongs to the bytes it
    read, and moved to other bytes it would satisfy their describe row with
    a reading of a different picture.

    The moved description is written before its old name is removed: an
    interruption between the two leaves the kept file described and the
    retired file's reading standing, which the next hand-over deletes.
    """
    descriptions = descriptions_of(item_dir, retired)
    if not descriptions or descriptions_of(item_dir, kept):
        for description in descriptions:
            description.unlink()
        return Handover(moved=None, dropped=len(descriptions))
    first, *rest = descriptions
    moved = _move_description(first, kept)
    for description in rest:
        description.unlink()
    return Handover(moved=moved, dropped=len(rest))


def _move_description(description: Path, kept: str) -> Path:
    """Re-point ``description`` to ``kept``, under the kept slot's name where it is free."""
    slot = _DOWNLOAD_SLOT_RE.match(kept)
    target = description if slot is None else description.with_name(f"media-{slot[1]}.md")
    if target != description and target.exists():
        target = description
    first, sep, rest = description.read_text(encoding="utf-8").partition("\n")
    atomic.write_text(target, _BACKTICKED_RE.sub(f"`{kept}`", first, count=1) + sep + rest)
    if target != description:
        description.unlink()
    return target
