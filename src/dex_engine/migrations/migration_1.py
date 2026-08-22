"""Migration 1 — renames + status vocabulary.

The rewrite renamed two kinds (``tweet → x``, ``blog → web``), retired two
statuses (``nocaptions``/``toolong`` → ``waiting`` + ``needs: transcribe`` —
both immediately drainable now), and formalized the ledger schema.
This migration moves pre-rewrite instance state onto that vocabulary:

- corpus ``kinds:`` and ``enrichment:`` listings, through corpus.py's typed
  read/write — Claude-authored bodies pass through byte-exact, and ``kinds``
  lands sorted and deduped, the order ``normalize`` derives, so the first
  regeneration after the migration is a no-op instead of a second rewrite;
- enrichment filenames (``tweet-*.md`` → ``x-*.md``, ``blog-*.md`` →
  ``web-*.md``);
- the ledger, line by line, through a tolerant reader for the OLD line
  shape — never ``ledger.from_line``, which rejects pre-migration
  vocabulary by design. The old engine used ``error`` as an informal
  reason field on non-error statuses and once a ``note`` field; those
  values are ported into ``reason``, never destroyed. Old lines carry no
  ``item``/``engine``: the item is recovered from the output path, from the
  enrichment tree on disk (outputs are named ``<kind>-<hash[:6]>.md`` under
  ``enrichment/<item>/``, so the file the unit produced names its owner), or
  by hashing corpus URLs with the old engine's own canonicalization; the
  engine is stamped ``0.0.1`` — the only version the pre-rewrite engine
  ever shipped as, which is exactly what gives old ``error`` entries their
  retry-on-new-engine semantics;
- ``state/normalize-config.json`` → ``state/config.json``, dropping the
  ``name_map`` key on the way — never applied by any engine version, and
  source-specific to the Discord/Space backfill.

Files this migration cannot judge (a hand-healed corpus file, a config that
does not fit the new schema) are skipped-with-why for the session to repair.
A ledger line that still cannot be translated after all three attributions
is **dropped**, counted and named in the report. Dropping is safe because of
what the ledger is: the corpus is the source of truth and the ledger is
derived work state — seeding builds work units from corpus items' URLs, so
anything that still matters re-enters through the front door on the next
run. And the pre-migration ledger is committed in git history, so nothing is
destroyed either way. Leaving these lines somewhere for a human would be
worse than useless: the sanctioned correction verb (``enrich mark``) needs a
unit to heal, and a line with no attributable item is exactly the line it
cannot create.

The main ledger loads clean after this, so every verb keeps working.
Re-running is safe at any point: renames and translations are no-ops on
already-migrated state, the ledger rewrite is atomic, and a second apply
finds nothing left to drop.
"""

import contextlib
import dataclasses
import datetime
import hashlib
import json
import re
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from dex_engine import atomic, corpus
from dex_engine.pipeline.ledger import to_line
from dex_engine.pipeline.types import (
    Format,
    Kind,
    LedgerEntry,
    MigrationReport,
    Need,
    Skipped,
    Status,
)

__all__ = ["PRE_REWRITE_ENGINE", "RenamesAndVocabulary", "build"]

NUMBER = 1
INTENT = (
    "renames + status vocabulary: tweet→x / blog→web across corpus kinds, enrichment "
    "filenames, and the ledger; nocaptions/toolong → waiting + needs: transcribe; "
    "pre-migration error/note text ported into reason; normalize-config.json → config.json"
)

# The only version the pre-rewrite engine ever shipped as; stamped onto old
# lines (they recorded no engine), giving old `error` entries retry-on-new-
# engine semantics and giving migration 2 its pre-rewrite marker.
PRE_REWRITE_ENGINE = "0.0.1"

_KIND_RENAMES = {"tweet": "x", "blog": "web"}
_FILENAME_RENAMES = (("tweet-", "x-"), ("blog-", "web-"))
_RETIRED_STATUSES = frozenset({"nocaptions", "toolong"})

# Keys a pre-migration OR current-schema line may carry (`note` is the one
# wild extra). Anything else is a hand-healed shape this code cannot judge.
# Every current-schema key belongs here: a re-apply reads lines the running
# engine wrote, and a key missing from this list drops each of them.
_TOLERATED_KEYS = frozenset(
    {
        "hash",
        "url",
        "item",
        "kind",
        "format",
        "status",
        "needs",
        "attempts",
        "capped",
        "engine",
        "date",
        "at",
        "via",
        "parent",
        "depth",
        "rerun",
        "path",
        "title",
        "error",
        "reason",
        "note",
    }
)


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — translated lines keep their own dates
    now: Callable[[], datetime.datetime],  # noqa: ARG001 — and are never stamped with this run's instant
    engine_version: str,  # noqa: ARG001 — old lines are stamped PRE_REWRITE_ENGINE, deliberately
) -> "RenamesAndVocabulary":
    """Build migration 1 (uniform migration-module contract)."""
    return RenamesAndVocabulary()


class _UntranslatableError(Exception):
    """A line/file this migration cannot translate provably safely — skip it."""


class RenamesAndVocabulary:
    """Migration 1: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def apply(self, root: Path) -> MigrationReport:
        """Apply the renames against the instance at ``root``.

        Tolerates un-pulled repos: every touched file is optional.
        """
        actions: list[str] = []
        skipped: list[Skipped] = []
        collisions = _rename_enrichment_files(root, actions, skipped)
        _rewrite_corpus(root, actions, skipped, collisions=collisions)
        _rewrite_ledger(root, actions, collisions=collisions)
        _rename_config(root, actions, skipped)
        return MigrationReport(actions=actions, skipped=skipped, anomalies=[])


# ---------------------------------------------------------------------------
# Enrichment filenames and corpus frontmatter
# ---------------------------------------------------------------------------


def _rename_enrichment_files(
    root: Path, actions: list[str], skipped: list[Skipped]
) -> set[tuple[str, str]]:
    """Rename ``tweet-*``/``blog-*`` enrichment files; report collision skips.

    Returns:
        The ``(item dir, old basename)`` keys whose disk rename was refused
        (the new name already exists) — the corpus and ledger rewrites must
        leave references to those exact files untouched, keyed per directory
        because the same basename may exist under two items.
    """
    enrichment = root / "enrichment"
    collisions: set[tuple[str, str]] = set()
    if not enrichment.is_dir():
        return collisions
    renamed = 0
    for old_prefix, _ in _FILENAME_RENAMES:
        for path in sorted(enrichment.rglob(f"{old_prefix}*.md")):
            target = path.with_name(_rename_basename(path.name))
            if target.exists():
                collisions.add((path.parent.name, path.name))
                skipped.append(
                    Skipped(
                        what=str(path.relative_to(root)),
                        why=f"{target.name} already exists beside it — refusing to overwrite; "
                        f"corpus listings and ledger paths still say {path.name} — "
                        "merge the two by hand",
                    )
                )
                continue
            path.rename(target)
            renamed += 1
    if renamed:
        actions.append(f"enrichment: {renamed} file(s) renamed (tweet-*→x-*, blog-*→web-*)")
    return collisions


def _rewrite_corpus(
    root: Path,
    actions: list[str],
    skipped: list[Skipped],
    *,
    collisions: set[tuple[str, str]],
) -> None:
    corpus_dir = root / "corpus"
    if not corpus_dir.is_dir():
        return
    rewritten = 0
    for path in sorted(corpus_dir.rglob("*.md")):
        try:
            item = corpus.read_item(path)
        except corpus.CorpusSchemaError as e:
            skipped.append(
                Skipped(
                    what=str(path.relative_to(root)),
                    why=f"frontmatter does not parse — repair by hand ({e})",
                )
            )
            continue
        # sorted+deduped, exactly as normalize derives kinds — otherwise the
        # first regeneration after the migration rewrites these files again
        # for ordering alone, and the migration's own commit reads as noise.
        kinds = sorted({_KIND_RENAMES.get(kind, kind) for kind in item.kinds})
        enrichment = [
            name if (item.id, name) in collisions else _rename_basename(name)
            for name in item.enrichment
        ]
        if kinds == item.kinds and enrichment == item.enrichment:
            continue
        corpus.write_item(path, dataclasses.replace(item, kinds=kinds, enrichment=enrichment))
        rewritten += 1
    if rewritten:
        actions.append(
            f"corpus: {rewritten} item(s) rewritten — kinds and enrichment listings "
            "renamed, bodies untouched"
        )


def _rename_basename(name: str) -> str:
    for old_prefix, new_prefix in _FILENAME_RENAMES:
        if name.startswith(old_prefix):
            return new_prefix + name[len(old_prefix) :]
    return name


# ---------------------------------------------------------------------------
# The old engine's canonicalization — frozen copies. The pre-rewrite ledger's
# hashes were minted with these exact functions; fidelity to that code matters
# more than sharing code with the live pipeline, which canonicalizes
# differently on purpose.
# ---------------------------------------------------------------------------

_LEGACY_STRIP_PARAMS = frozenset(
    {
        "si",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "feature",
        "ref",
        "ref_src",
        "lang",
        "app",
    }
)


def _legacy_canonical(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    path, query = parts.path, parts.query
    if host == "youtu.be":
        host, query, path = "youtube.com", f"v={parts.path.lstrip('/')}", "/watch"
    pairs = [(k, v) for k, v in urllib.parse.parse_qsl(query) if k not in _LEGACY_STRIP_PARAMS]
    if host == "youtube.com":
        pairs = [(k, v) for k, v in pairs if k == "v"]
    return urllib.parse.urlunsplit(
        ("https", host, path.rstrip("/"), urllib.parse.urlencode(pairs), "")
    )


def _legacy_uhash(url: str) -> str:
    canonical = _legacy_canonical(url).encode()
    return hashlib.sha1(canonical).hexdigest()[:10]  # noqa: S324 — content key, not a security context


def _corpus_owners(root: Path) -> dict[str, str]:
    """Map legacy URL hash -> owning corpus item id, as the old whisper flow did.

    Old ledger lines carry no ``item``; for lines without an output path this
    corpus scan is the only attribution. Unparseable corpus files are already
    reported by the corpus pass — here they are silently ignored.
    """
    owners: dict[str, str] = {}
    corpus_dir = root / "corpus"
    if not corpus_dir.is_dir():
        return owners
    for path in sorted(corpus_dir.rglob("*.md")):
        with contextlib.suppress(corpus.CorpusSchemaError):
            item = corpus.read_item(path)
            for url in item.urls:
                try:
                    key = _legacy_uhash(url)
                except ValueError:
                    # One malformed hand-healed URL (urlsplit rejects e.g. a
                    # bad IPv6 literal) must never abort the whole migration;
                    # entries it would have owned surface as unattributable.
                    continue
                owners.setdefault(key, item.id)
    return owners


_OUTPUT_NAME = re.compile(r"^[a-z-]+-([0-9a-f]{6})\.md$")


def _output_owners(root: Path) -> dict[str, str]:
    """Map a unit hash's first six hex digits -> the item whose tree holds its output.

    Enrichment outputs are named ``<kind>-<hash[:6]>.md`` under
    ``enrichment/<item>/``, so a line that produced a file has its owner on
    disk even when the line itself never recorded one. Called after the
    ``tweet-*``/``blog-*`` rename, whose prefixes do not touch the hash.

    A prefix landing under two items is dropped rather than guessed: six hex
    digits collide eventually, and attributing a unit to the wrong item
    writes the next rerun's output into the wrong tree.
    """
    owners: dict[str, str] = {}
    ambiguous: set[str] = set()
    enrichment = root / "enrichment"
    if not enrichment.is_dir():
        return owners
    for path in sorted(enrichment.glob("*/*.md")):
        match = _OUTPUT_NAME.match(path.name)
        if match is None:
            continue
        key = match.group(1)
        if owners.setdefault(key, path.parent.name) != path.parent.name:
            ambiguous.add(key)
    for key in ambiguous:
        del owners[key]
    return owners


# ---------------------------------------------------------------------------
# Ledger translation
# ---------------------------------------------------------------------------


# How many dropped lines the report names individually before it summarizes
# the rest: enough to see what kind of thing went, short enough to read.
_DROP_REPORT_CAP = 5


def _rewrite_ledger(
    root: Path,
    actions: list[str],
    *,
    collisions: set[tuple[str, str]],
) -> None:
    path = root / "state" / "enrichment-ledger.jsonl"
    if not path.exists():
        return
    owners = _corpus_owners(root)
    outputs = _output_owners(root)
    original = path.read_text(encoding="utf-8")
    out_lines: list[str] = []
    dropped: list[str] = []
    # Note dedupe: superseded lines of one hash repeat the same translation
    # verbatim (a stray title survives every audit line), so identical notes
    # collapse to one, the multiplicity kept as a count — collapsed, never
    # dropped.
    note_counts: dict[str, int] = {}
    translated = 0
    for lineno, line in enumerate(original.split("\n"), start=1):
        if not line.strip():
            continue
        # Per-line notes buffer: flushed into the report only when the line
        # actually translates — a skipped line must not leave phantom actions.
        line_notes: list[str] = []
        try:
            new_line = to_line(
                _translate_record(
                    _load_record(line),
                    owners=owners,
                    outputs=outputs,
                    notes=line_notes,
                    collisions=collisions,
                )
            )
        except _UntranslatableError as e:
            dropped.append(f"line {lineno} {_identify(line)}: {e}")
            continue
        for note in line_notes:
            note_counts[note] = note_counts.get(note, 0) + 1
        if new_line != line:
            translated += 1
        out_lines.append(new_line)
    actions.extend(
        note if count == 1 else f"{note} (x{count})" for note, count in note_counts.items()
    )
    new_text = "".join(out_line + "\n" for out_line in out_lines)
    if new_text != original:
        atomic.write_text(path, new_text)
    if translated:
        actions.append(f"ledger: {translated} line(s) translated to the current schema")
    _report_dropped(dropped, actions)


def _report_dropped(dropped: list[str], actions: list[str]) -> None:
    """State what went and why — dropping silently is what makes it look like loss."""
    if not dropped:
        return
    actions.append(
        f"ledger: {len(dropped)} untranslatable line(s) dropped — corpus seeding "
        "re-raises anything that still matters, and the pre-migration ledger "
        "stays in git history"
    )
    actions.extend(f"ledger dropped {entry}" for entry in dropped[:_DROP_REPORT_CAP])
    if len(dropped) > _DROP_REPORT_CAP:
        actions.append(
            f"ledger: … and {len(dropped) - _DROP_REPORT_CAP} further dropped line(s), "
            "same treatment — read them in git history"
        )


def _identify(line: str) -> str:
    """Name a dropped line the way a reader can find it again: hash and URL."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return repr(line[:120])
    if not isinstance(raw, dict):
        return repr(line[:120])
    unit_hash = raw.get("hash")
    url = raw.get("url")
    named = " ".join(str(part) for part in (unit_hash, url) if isinstance(part, str))
    return named or repr(line[:120])


def _load_record(line: str) -> dict[str, object]:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as e:
        raise _UntranslatableError(f"not valid JSON ({e})") from e
    if not isinstance(raw, dict):
        raise _UntranslatableError("not a JSON object")
    unknown = sorted(set(raw) - _TOLERATED_KEYS)
    if unknown:
        raise _UntranslatableError(
            f"unknown field(s) {unknown} — a hand-healed shape this code cannot judge"
        )
    return raw


def _translate_record(
    raw: dict[str, object],
    *,
    owners: dict[str, str],
    outputs: dict[str, str],
    notes: list[str],
    collisions: set[tuple[str, str]],
) -> LedgerEntry:
    unit_hash = _expect_str(raw, "hash")
    status, needs = _translate_status(raw)
    raw_path = None if "path" not in raw else _expect_str(raw, "path")
    path_text = _translate_path(
        raw_path, status=status, unit_hash=unit_hash, notes=notes, collisions=collisions
    )
    error, reason = _translate_error_and_reason(
        raw, status=status, unit_hash=unit_hash, notes=notes
    )
    title = _translate_title(
        raw, status=status, path_text=path_text, unit_hash=unit_hash, notes=notes
    )
    try:
        return LedgerEntry(
            hash=unit_hash,
            url=_expect_str(raw, "url"),
            item=_translate_item(
                raw, raw_path=raw_path, unit_hash=unit_hash, owners=owners, outputs=outputs
            ),
            kind=_translate_kind(raw),
            format=None if "format" not in raw else _as_format(_expect_str(raw, "format")),
            status=status,
            needs=needs,
            attempts=None if "attempts" not in raw else _expect_int(raw, "attempts"),
            capped=_expect_bool(raw, "capped") if "capped" in raw else False,
            engine=_expect_str(raw, "engine") if "engine" in raw else PRE_REWRITE_ENGINE,
            date=_translate_date(raw),
            # Carried, never re-stamped: this run's instant would claim the
            # line was written now, and an unstamped line is already ordered
            # correctly (oldest) by the loader.
            at=_translate_at(raw),
            via=_translate_via(raw, unit_hash=unit_hash, notes=notes),
            parent=None if "parent" not in raw else _expect_str(raw, "parent"),
            depth=None if "depth" not in raw else _expect_int(raw, "depth"),
            rerun=_expect_bool(raw, "rerun") if "rerun" in raw else False,
            path=path_text,
            title=title,
            error=error,
            reason=reason,
        )
    except ValueError as e:
        raise _UntranslatableError(
            f"still violates the current schema after translation ({e})"
        ) from e


def _translate_kind(raw: dict[str, object]) -> Kind:
    text = _expect_str(raw, "kind")
    text = _KIND_RENAMES.get(text, text)
    try:
        return Kind(text)
    except ValueError as e:
        raise _UntranslatableError(f"unknown kind {raw.get('kind')!r}") from e


def _translate_status(raw: dict[str, object]) -> tuple[Status, Need | None]:
    text = _expect_str(raw, "status")
    if text in _RETIRED_STATUSES:
        # Both retired statuses meant "cannot transcribe" — whisper-local and
        # chunking removed those limits, so the cohort is drainable again.
        return Status.WAITING, Need.TRANSCRIBE
    try:
        status = Status(text)
    except ValueError as e:
        raise _UntranslatableError(f"unknown status {text!r}") from e
    needs = None if "needs" not in raw else _as_need(_expect_str(raw, "needs"))
    return status, needs


def _translate_date(raw: dict[str, object]) -> datetime.date:
    text = _expect_str(raw, "date")
    try:
        return datetime.date.fromisoformat(text)
    except ValueError as e:
        raise _UntranslatableError(f"unparseable date {text!r}") from e


def _translate_at(raw: dict[str, object]) -> datetime.datetime | None:
    if "at" not in raw:
        return None
    text = _expect_str(raw, "at")
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError as e:
        raise _UntranslatableError(f"unparseable write timestamp {text!r}") from e


def _translate_path(
    raw_path: str | None,
    *,
    status: Status,
    unit_hash: str,
    notes: list[str],
    collisions: set[tuple[str, str]],
) -> str | None:
    if raw_path is None:
        return None
    if status is not Status.DONE:
        notes.append(
            f"ledger {unit_hash}: stray path {raw_path!r} dropped from a "
            f"{status.value} line — outputs are success-only"
        )
        return None
    parts = raw_path.split("/")
    # A collision-skipped file keeps its old name on disk, so its ledger
    # path must keep pointing at it — renaming here would point the line at
    # the OTHER file's content.
    if len(parts) >= 3 and parts[0] == "enrichment" and (parts[1], parts[-1]) in collisions:  # noqa: PLR2004 — enrichment/<item>/<file>
        return raw_path
    parts[-1] = _rename_basename(parts[-1])
    return "/".join(parts)


def _translate_item(
    raw: dict[str, object],
    *,
    raw_path: str | None,
    unit_hash: str,
    owners: dict[str, str],
    outputs: dict[str, str],
) -> str:
    # The raw path attributes the item even when the path field itself is
    # dropped (outputs are done-only) — where the file landed is still
    # true provenance.
    if "item" in raw:
        return _expect_str(raw, "item")
    if raw_path is not None:
        parts = raw_path.split("/")
        if len(parts) >= 3 and parts[0] == "enrichment" and parts[1]:  # noqa: PLR2004 — enrichment/<item>/<file>
            return parts[1]
    # Same provenance, read off the tree instead of the line: an old line
    # that recorded no path may still have left its output on disk.
    owner = outputs.get(unit_hash[:6]) or owners.get(unit_hash)
    if owner is None:
        raise _UntranslatableError(
            "no item attribution: the line has no item, no output path, no enrichment "
            "file on disk carries its hash, and no corpus item's URLs hash to it"
        )
    return owner


def _translate_error_and_reason(
    raw: dict[str, object], *, status: Status, unit_hash: str, notes: list[str]
) -> tuple[str | None, str | None]:
    """Port the old informal ``error``-as-reason and ``note`` values.

    Never destroyed: on statuses where the new schema allows or requires a
    reason they land in ``reason``; on ``error`` they stay in ``error``; on
    ``done``/``queued`` — where the schema forbids a reason — the text is preserved
    in the migration report instead of the line, stated explicitly.
    """
    error_text = None if "error" not in raw else _expect_str(raw, "error")
    note_text = None if "note" not in raw else _expect_str(raw, "note")
    existing_reason = None if "reason" not in raw else _expect_str(raw, "reason")
    if status is Status.ERROR:
        error = error_text or "unrecorded (pre-migration error)"
        if note_text:
            error = f"{error}; note: {note_text}"
        return error, existing_reason
    pieces = [piece for piece in (existing_reason, error_text, note_text) if piece]
    reason = "; ".join(pieces) if pieces else None
    if status in (Status.DONE, Status.QUEUED):
        if reason is not None:
            notes.append(
                f"ledger {unit_hash}: pre-migration text {reason!r} preserved here and "
                f"dropped from the line — reason is forbidden on {status.value}"
            )
        return None, None
    if status in (Status.MANUAL, Status.SKIPPED) and reason is None:
        # The old engine recorded no reason for these; the schema requires one. The
        # honest translation records that no reason was stated.
        reason = "unstated (pre-migration)"
    return None, reason


def _translate_title(
    raw: dict[str, object],
    *,
    status: Status,
    path_text: str | None,
    unit_hash: str,
    notes: list[str],
) -> str | None:
    if "title" not in raw:
        return None
    text = _expect_str(raw, "title")
    if status is not Status.DONE or path_text is None:
        notes.append(
            f"ledger {unit_hash}: stray title {text!r} dropped — "
            "outputs (path/title) are done-only and travel together"
        )
        return None
    return text


# The current provenance vocabulary — frozen here like the legacy
# canonicalization above: this migration's contract is "translate to the
# schema shipping WITH it", so the copy cannot drift out from under it.
_CURRENT_VIA = frozenset({"harvest", "thread", "media", "sniff", "extract-asset"})
_CURRENT_VIA_MIGRATION = re.compile(r"^migration-[1-9][0-9]*$")


def _translate_via(raw: dict[str, object], *, unit_hash: str, notes: list[str]) -> str | None:
    if "via" not in raw:
        return None
    text = _expect_str(raw, "via")
    if text in _CURRENT_VIA or _CURRENT_VIA_MIGRATION.match(text):
        return text
    # The old engine stamped fetcher names here ('whisper', 'fxtwitter', …).
    # Any value outside the current vocabulary is pre-migration provenance:
    # dropped, never skipped-over — the enrichment file frontmatter records
    # the same provenance, so nothing is lost.
    notes.append(
        f"ledger {unit_hash}: via {text!r} dropped — pre-migration provenance "
        "vocabulary; the enrichment file frontmatter still records it"
    )
    return None


# ---------------------------------------------------------------------------
# Typed extraction from a raw line — mirrors ledger.py's helpers, but failures
# are _UntranslatableError (skip-with-why), never LedgerSchemaError (hard stop).
# ---------------------------------------------------------------------------


def _expect_str(raw: dict[str, object], key: str) -> str:
    if key not in raw:
        raise _UntranslatableError(f"missing field {key!r}")
    value = raw[key]
    if not isinstance(value, str):
        raise _UntranslatableError(f"field {key!r} must be a string, got {type(value).__name__}")
    return value


def _expect_int(raw: dict[str, object], key: str) -> int:
    value = raw[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise _UntranslatableError(f"field {key!r} must be an integer, got {type(value).__name__}")
    return value


def _expect_bool(raw: dict[str, object], key: str) -> bool:
    value = raw[key]
    if not isinstance(value, bool):
        raise _UntranslatableError(f"field {key!r} must be a boolean, got {type(value).__name__}")
    return value


def _as_format(text: str) -> Format:
    try:
        return Format(text)
    except ValueError as e:
        raise _UntranslatableError(f"unknown format {text!r}") from e


def _as_need(text: str) -> Need:
    try:
        return Need(text)
    except ValueError as e:
        raise _UntranslatableError(f"unknown needs {text!r}") from e


# ---------------------------------------------------------------------------
# Config rename
# ---------------------------------------------------------------------------


_NAME_MAP_NOTE = (
    "config: name_map removed — never applied by any engine version; "
    "source-specific to the Discord/Space backfill"
)


def _rename_config(root: Path, actions: list[str], skipped: list[Skipped]) -> None:
    from dex_engine.pipeline.types import Config  # noqa: PLC0415 — narrow import, used only here

    old = root / "state" / "normalize-config.json"
    new = root / "state" / "config.json"
    if not old.exists():
        return
    try:
        raw = json.loads(old.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        skipped.append(
            Skipped(
                what="state/normalize-config.json",
                why=f"not valid JSON ({e}) — repair it, then rename to config.json",
            )
        )
        return
    # name_map is dropped, not carried: no engine version ever applied it,
    # and the current config schema rejects unknown keys loudly — carrying
    # it forward would brick every command on the migrated instance.
    dropped_name_map = isinstance(raw, dict) and raw.pop("name_map", None) is not None
    try:
        Config.from_raw(raw, source="state/normalize-config.json")
    except ValueError as e:
        skipped.append(
            Skipped(
                what="state/normalize-config.json",
                why=f"content does not fit the new config schema ({e}) — repair it, "
                "then rename to config.json",
            )
        )
        return
    if new.exists():
        try:
            existing = json.loads(new.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        if existing == raw:
            old.unlink()
            actions.append(
                "config: removed normalize-config.json — config.json already holds "
                "identical content (a racing machine migrated first)"
            )
        else:
            skipped.append(
                Skipped(
                    what="state/normalize-config.json",
                    why="state/config.json already exists with different content — "
                    "merge the two by hand",
                )
            )
        return
    atomic.write_text(new, json.dumps(raw, indent=2) + "\n")
    old.unlink()
    actions.append("config: normalize-config.json → config.json")
    if dropped_name_map:
        actions.append(_NAME_MAP_NOTE)
