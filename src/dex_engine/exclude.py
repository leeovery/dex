"""dex-exclude: drop corpus items that yield nothing through the lens, on the record.

``bin/dex exclude <exclusions.json>`` — the file is a JSON list of
``{"id": ..., "reason": ...}`` records, written by the lens verdict: the
judgment, made once an item's content has landed, that reading it through
this instance's lens yields nothing.

Each exclusion appends to ``state/exclusions.tsv`` (consulted by
``normalize.py`` so excluded clusters are never regenerated), removes the
media files the item's corpus frontmatter lists under ``media/`` and the
directory that held them once it is empty, removes
``corpus/<year>/<id>.md``, ``enrichment/<id>/`` and
``state/digests/<id>.md``, and purges the item's records from
``state/passes.jsonl`` and its ledger entries — the item is gone, so
seeding will never raise that work again and the lines would otherwise
linger forever. The digest goes with the rest: it is a permanent fact
index over content this instance has just ruled yields nothing, and
nothing else ever deletes one, so leaving it behind keeps a permanently
excluded item feeding query and wiki forever.

Except the work another live corpus item still claims. A work unit is
keyed by URL, not by item, so two items listing one URL share one ledger
entry that names only one of them; purging on the name alone would delete
a history the survivor still owns. The claim is asked of the one ownership
resolution every ledger reader routes through
(:func:`dex_engine.pipeline.ownership.unit_owners`), not of frontmatter
alone: a child promoted under the excluded item appears in no ``urls:``/
``media:``, so its only corpus answer travels its ``parent`` chain — where
the chain ends at work a survivor lists, the child is the survivor's too.
The summary states both counts, because a pull's chatter is dropped in
bulk and that line is the owner's only signal. A corpus file that cannot
be read claims nothing, which would make that veto fail open toward
deletion, so a batch that meets one purges no ledger entries at all and
the summary says why.

Media is claimed the same way, by the corpus. ``dex inbox`` keys a
capture's directory by a hash of its file's name, so two captures of one
``screenshot.png`` list the same file, and a file any surviving item lists
is kept. Only an item's corpus file says which media it carries, so the
media is read before that file goes, and a batch whose media cannot be
settled is refused whole: one of its own corpus files that cannot be read,
or a survivor's while the batch carries media, would leave either a file
no later run could ever find or a deletion from under an item that lists
it. The ledger veto can wait for a re-run because the ledger keeps the
lines; nothing keeps the media list.

Pass records go by the item's id, and by its trailing shortid where no
live item carries it, which is how the pass readers resolve a record
written before a rename.

The purge judges every line against the whole exclusions record, never
this batch's ids alone. A kept line still names the item it was seeded
under — lines are never rewritten in place; reads resolve ownership live —
so when the survivor is excluded in a later batch, the stored string
matches no id in that batch: judged against the batch alone the hash was
never vetoed and never purged, unpurgeable by any batch forever, and the
next run refetched it under the excluded id. Judged against the record,
whichever batch removes the last claimant sweeps the line, and any later
batch sweeps residue an interrupted purge left.

A kept entry whose output was filed under the purged item goes back to
``queued`` in the same breath: the line survives on the survivor's claim,
but the enrichment left with the item that produced it, and seeding's
already-a-unit short-circuit means nothing would ever fetch it again.

The exclusions file is LLM-authored and this deletes recursively, so every
entry in the batch is validated before anything is written or removed: an
id is a corpus item id, never a path, every value is checked for its type,
and the batch is refused whole. A half-applied batch is worse than none —
its first entry is recorded in ``state/exclusions.tsv`` permanently, with
its ledger purge unrun and the entries after it silently lost.

One id twice in a batch is not a refusal — the operation is idempotent, so
the second copy asks for what the first already did. The copies collapse to
one entry (:func:`_deduplicated`) and the summary states how many, because
applied twice they wrote the permanent record twice and reported the second
pass as an item already gone.

A successful purge recompiles ``state/map.json`` and re-renders
``wiki/index.md`` — the corpus and digests it deletes are compile inputs.
A recompile failure rides the summary into
the command's error exit (:func:`dex_engine.instance_map.recompile`)
rather than un-reporting deletions that already happened.
"""

import argparse
import dataclasses
import datetime
import json
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import atomic, corpus, instance_map
from .directives import refuse_while_pending
from .pipeline import ledger
from .pipeline.ownership import unit_owners
from .pipeline.registry import default_drivers
from .pipeline.types import Instance, LedgerEntry, Status
from .pipeline.urls import resolve_repo_path
from .version import engine_version

__all__ = ["build_parser", "main", "run_exclude"]

_DEFAULT_REASON = "yields nothing through this instance's lens"


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.UTC).date()


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


# An item id names one file in the corpus tree — the normalizer mints
# `<date>-<slug>-<shortid>`, a single path component. Anything with a
# separator in it is not an id, and `corpus_dir / item_id[:4] /
# f"{item_id}.md"` would follow it out of the instance: an absolute path
# replaces the root outright, `..` climbs above it.
_ITEM_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*")


@dataclass(frozen=True, slots=True, kw_only=True)
class _Exclusion:
    """One validated entry: the item id, its reason, and the paths it removes."""

    item_id: str
    reason: str
    item_path: Path
    enrichment_path: Path
    digest_path: Path


@dataclass(frozen=True, slots=True, kw_only=True)
class _Survey:
    """One read of the corpus, made before anything is removed.

    ``media`` holds each batch item's files under ``media/`` and
    ``claimed`` every such file a surviving item lists, both resolved. The
    corpus files no reader could parse are split by whether this batch
    removes them.
    """

    media: dict[str, tuple[Path, ...]]
    claimed: frozenset[Path]
    unreadable_batch: tuple[Path, ...]
    unreadable_survivors: tuple[Path, ...]


@dataclass(slots=True, kw_only=True)
class _Removed:
    """What the purge took, counted for the summary."""

    items: int = 0
    missing: int = 0
    digests: int = 0
    media: int = 0
    passes: int = 0


def run_exclude(
    instance: Instance,
    entries: list[dict[str, object]],
    *,
    today: Callable[[], datetime.date] = _today,
    now: Callable[[], datetime.datetime] = _utc_now,
    version: Callable[[], str] = engine_version,
) -> str:
    """Exclude the given items: record why, then purge everything each one left behind.

    Args:
        instance: The instance.
        entries: ``{"id": ..., "reason": ...}`` records (reason optional).
        today: Injected date clock, for the re-queue lines below.
        now: Injected instant clock, UTC-aware — the write timestamp.
        version: The running engine's version, read lazily: only a purge
            that strands a survivor's landing writes a ledger line at all.

    Returns:
        The one-line summary.

    Raises:
        ValueError: An entry is not an object, has no ``id``, has an ``id``
            that is not a corpus item id or resolves outside the instance,
            or has a non-string ``reason``; or a corpus file that cannot be
            read leaves the batch's media unsettled. The whole batch is
            refused before anything is written or deleted. Also raised when
            the purge landed and the map then failed to recompile — the
            message carries the summary, so the deletions that happened
            are never un-reported.
    """
    summary = _excluded(instance, entries, today=today, now=now, version=version)
    # The purge changed the map's inputs — the corpus, the digests — so
    # the last act is the recompile, on every summary path.
    instance_map.recompile(instance, summary)
    return f"{summary}; map and index recompiled"


def _excluded(
    instance: Instance,
    entries: list[dict[str, object]],
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    version: Callable[[], str],
) -> str:
    """The purge itself: record, remove, sweep the passes and the ledger, re-queue strands."""
    validated = _deduplicated([_validated(instance, entry) for entry in entries])
    survey = _survey(instance, validated)
    _refuse_unsettled_media(instance, survey)
    removed = _record_and_remove(instance, validated, survey)
    # One rewrite for the whole batch, after the TSV record lands: an
    # interruption before it leaves the entries in place, and the re-run
    # (which the TSV makes idempotent) purges them.
    # Scanned after the deletions above, so a purged item cannot claim its
    # own work — what remains is what the surviving corpus still lists.
    # The purge judges lines against the WHOLE record, not this batch's ids
    # alone: a shared unit's line kept on a survivor's claim still names the
    # item it was seeded under, so the batch that later excludes the
    # survivor holds no id matching the stored string — the batch's ids
    # alone left that hash unjudged forever, and the next run refetched it.
    on_record = {exclusion.item_id for exclusion in validated} | _on_record(
        instance.state_dir / "exclusions.tsv"
    )
    removed.passes = _drop_pass_records(instance, on_record)
    summary = _removal_counts(
        asked=len(entries), excluded=len(validated), removed=removed, survey=survey
    )
    if not instance.ledger_path.exists():
        return summary + _purge_counts(0, 0)
    if survey.unreadable_survivors:
        # The claim veto decides a deletion, and a corpus file that cannot
        # be read cannot say which work it claims: purging anyway would
        # take a live item's history and report a clean drop. So nothing is
        # purged until the file is readable — the item and its enrichment
        # still go, and the re-run purges.
        return (
            summary
            + _purge_counts(0, 0)
            + f"; {len(survey.unreadable_survivors)} corpus file(s) could not be read "
            f"({_listed(instance, survey.unreadable_survivors)}), so no ledger entries were "
            "purged — repair them and re-run"
        )
    claimed = _surviving_claims(instance, now=now)
    dropped, kept = ledger.drop_items(instance.ledger_path, on_record, claimed=claimed, now=now)
    summary += _purge_counts(dropped, kept)

    def stamp(entry: LedgerEntry) -> LedgerEntry:
        # `version` is read here and nowhere else: only a purge that
        # strands a survivor's landing writes a ledger line at all.
        return ledger.stamp(entry, today=today, now=now, engine_version=version())

    requeued = _requeue_stranded_landings(instance, on_record, claimed, now=now, stamp=stamp)
    if requeued:
        summary += f"; {requeued} re-queued (enrichment went with the item that produced it)"
    return summary


def _record_and_remove(
    instance: Instance, validated: list[_Exclusion], survey: _Survey
) -> _Removed:
    """Record each exclusion and remove what it owns; return the counts.

    The TSV record lands first for every entry, because it is what makes a
    re-run idempotent: an interruption after it leaves an id recorded whose
    deletions the re-run finishes. Each item's media goes before its corpus
    file, the only record of which media it carries, so an interruption
    between the two still leaves the re-run a list to finish from.
    """
    exclusions = instance.state_dir / "exclusions.tsv"
    existing = _on_record(exclusions)
    exclusions.parent.mkdir(parents=True, exist_ok=True)
    with exclusions.open("a", encoding="utf-8") as f:
        for exclusion in validated:
            if exclusion.item_id not in existing:
                f.write(f"{exclusion.item_id}\t{exclusion.reason}\n")
    removed = _Removed()
    media_root = _media_root(instance.root)
    for exclusion in validated:
        files = survey.media.get(exclusion.item_id, ())
        removed.media += _remove_media(files, survey.claimed, media_root)
        if exclusion.item_path.exists():
            exclusion.item_path.unlink()
            removed.items += 1
        else:
            removed.missing += 1
        shutil.rmtree(exclusion.enrichment_path, ignore_errors=True)
        if exclusion.digest_path.exists():
            exclusion.digest_path.unlink()
            removed.digests += 1
    return removed


def _survey(instance: Instance, validated: list[_Exclusion]) -> _Survey:
    """Read every corpus file once, before the batch removes any of them.

    A batch item's media is read here because its corpus file is the only
    record of it, and the survivors' claims because a file one of them
    lists stays. Survivors are told from the batch by resolved path, the
    form :func:`_validated` resolved each item's path to.

    A file that cannot be read is listed, never skipped. The corpus claims
    under ``unit_owners`` skip one silently, rightly for their other
    callers, but here every claim decides a deletion, and a claim no reader
    can see fails open: the shared unit, or the shared media file, reads as
    claimed by nobody and goes, reported as a clean drop.
    """
    batch = {exclusion.item_path: exclusion.item_id for exclusion in validated}
    media_root = _media_root(instance.root)
    media: dict[str, tuple[Path, ...]] = {}
    claimed: set[Path] = set()
    unreadable_batch: list[Path] = []
    unreadable_survivors: list[Path] = []
    for path in sorted(instance.corpus_dir.glob("*/*.md")):
        owner = batch.get(path.resolve())
        try:
            item = corpus.read_item(path)
        except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError):
            (unreadable_survivors if owner is None else unreadable_batch).append(path)
            continue
        files = _media_files(instance.root, media_root, item.media)
        if owner is None:
            claimed.update(files)
        else:
            media[owner] = files
    return _Survey(
        media=media,
        claimed=frozenset(claimed),
        unreadable_batch=tuple(unreadable_batch),
        unreadable_survivors=tuple(unreadable_survivors),
    )


def _media_root(root: Path) -> Path:
    return (root / "media").resolve()


def _media_files(root: Path, media_root: Path, stated: list[str]) -> tuple[Path, ...]:
    """The stated media paths that resolve to a file inside ``media/``.

    Frontmatter is owner-editable data, never a path to trust: a stated
    path that climbs out of ``media/``, or names ``media/`` itself, is
    nothing this purge may delete.
    """
    resolved = (resolve_repo_path(root, repo_path) for repo_path in stated)
    return tuple(
        path
        for path in resolved
        if path is not None and path != media_root and path.is_relative_to(media_root)
    )


def _refuse_unsettled_media(instance: Instance, survey: _Survey) -> None:
    """Refuse the batch when a corpus file that cannot be read leaves its media unsettled.

    A batch item's own unreadable file hides which media it carries, and a
    survivor's hides which of the batch's media it lists. Neither can wait
    for a re-run the way the ledger veto does: the batch's corpus files go
    in this run, and nothing else records which media they carried.

    Raises:
        ValueError: Naming each unreadable file; nothing has been written.
    """
    carries_media = any(survey.media.values())
    unsettled = [
        *survey.unreadable_batch,
        *(survey.unreadable_survivors if carries_media else ()),
    ]
    if unsettled:
        raise ValueError(
            f"nothing was excluded: {len(unsettled)} corpus file(s) cannot be read "
            f"({_listed(instance, unsettled)}), and only a readable corpus says which "
            "media this batch may delete; repair them and re-run"
        )


def _remove_media(files: tuple[Path, ...], claimed: frozenset[Path], media_root: Path) -> int:
    """Delete an item's media files no survivor lists; return how many went.

    Each file's directory goes too once nothing is left in it, which is
    also how a re-run finishes a directory an interruption left empty.
    """
    removed = 0
    for path in files:
        if path in claimed:
            continue
        if path.is_file():
            path.unlink()
            removed += 1
        directory = path.parent
        if directory != media_root and directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return removed


def _drop_pass_records(instance: Instance, on_record: set[str]) -> int:
    """Drop the pass records of every item excluded on the record; return how many went.

    A record names its item as of the day it was written, and the pass
    readers resolve a dead id by its trailing shortid, which a rename
    keeps. So a record goes when it names an excluded id outright, or a
    dead id whose shortid is an excluded item's and no live item carries,
    which is that item's record from before a rename. A record naming a
    live id stays, and so does a line this cannot read: it names no item
    this can judge.

    Judged against the whole record, like the ledger purge, so any batch
    sweeps the records an earlier purge left behind.
    """
    path = instance.passes_path
    if not path.exists():
        return 0
    live = {item.stem for item in instance.corpus_dir.glob("*/*.md")}
    orphaned = {_shortid(item) for item in on_record} - {_shortid(item) for item in live}
    return atomic.drop_lines(
        path, lambda line: _names_excluded(_pass_item(line), live, on_record, orphaned)
    )


def _names_excluded(
    item: str | None, live: set[str], on_record: set[str], orphaned: set[str]
) -> bool:
    """Whether a pass record's item is an excluded item: see :func:`_drop_pass_records`."""
    if item is None or item in live:
        return False
    return item in on_record or _shortid(item) in orphaned


def _pass_item(line: str) -> str | None:
    """The item a pass record names, or ``None`` for a line that names none."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    item = record.get("item") if isinstance(record, dict) else None
    return item if isinstance(item, str) else None


def _shortid(item_id: str) -> str:
    return item_id.rsplit("-", 1)[-1]


def _listed(instance: Instance, paths: Sequence[Path]) -> str:
    return ", ".join(str(path.relative_to(instance.root)) for path in paths)


def _removal_counts(*, asked: int, excluded: int, removed: _Removed, survey: _Survey) -> str:
    """The summary's opening: the batch, then what the removal pass took."""
    counted = f"{excluded}"
    if asked > excluded:
        counted += f" ({asked - excluded} duplicate id(s) collapsed)"
    media = f"{removed.media} media files"
    shared = {path for files in survey.media.values() for path in files} & survey.claimed
    if shared:
        media += f" ({len(shared)} kept, listed by another live corpus item)"
    return (
        f"excluded {counted}: removed {removed.items} items ({removed.missing} already gone), "
        f"{removed.digests} digests, {media}, {removed.passes} pass records, "
    )


def _on_record(exclusions: Path) -> set[str]:
    """Every item id in ``state/exclusions.tsv`` — the full record, all batches.

    Read after :func:`_record_and_remove` appends, this is the batch's ids
    united with every prior ruling's: the set the ledger purge judges
    stored ``item`` strings against, because a kept line goes on naming an
    id excluded batches ago.
    """
    if not exclusions.exists():
        return set()
    return {
        line.split("\t")[0]
        for line in exclusions.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def _surviving_claims(
    instance: Instance,
    *,
    now: Callable[[], datetime.datetime],
) -> dict[str, tuple[str, ...]]:
    """Work hash -> the live items the one ownership resolution hands it to.

    The claim veto's map, asked of :func:`unit_owners` — the resolution
    every ledger reader routes ownership through — rather than of
    frontmatter claims alone. A child promoted under the excluded item is
    listed in no ``urls:``/``media:``, so its only corpus answer travels
    its ``parent`` chain; reading the veto off frontmatter purged such a
    child even where the chain resolves to a live co-claimant — work
    another item still owns, deleted with its history.

    Called after the batch's deletions, so the whole resolution answers
    against the post-exclusion corpus: a purged item is not live, cannot
    claim its own work, and cannot keep a child's chain alive.
    ``unit_owners`` falls back to the stored string where nothing live
    claims a unit, so the answer is filtered to the items with a corpus
    file — a fallback id proves no claim, and counting it as one would
    veto every purge. Each hash's resolution is homogeneous (a chain
    inherits live claimants whole or dies whole), so the filter keeps
    either the full answer or none of it.

    The entries are read tolerantly (:func:`ledger.latest_readable`) — the
    same view ``drop_items`` judges — so one hand-tampered line neither
    aborts the purge nor skews which hashes the veto covers.

    Args:
        instance: The instance.
        now: Injected instant clock, UTC-aware — the resolution reads the
            ledger against the same present the purge does.

    Returns:
        Work hash -> the surviving claimant ids, in id order; hashes no
        live item claims are absent.
    """
    entries = ledger.latest_readable(instance.ledger_path, now=now)
    owners = unit_owners(instance.root, entries, default_drivers())
    corpus_dir = instance.root / "corpus"
    live = {path.stem for path in corpus_dir.glob("*/*.md")} if corpus_dir.is_dir() else set()
    claims: dict[str, tuple[str, ...]] = {}
    for unit_hash, ids in owners.items():
        survivors = tuple(item_id for item_id in ids if item_id in live)
        if survivors:
            claims[unit_hash] = survivors
    return claims


def _requeue_stranded_landings(
    instance: Instance,
    on_record: set[str],
    claimed: Mapping[str, tuple[str, ...]],
    *,
    now: Callable[[], datetime.datetime],
    stamp: Callable[[LedgerEntry], LedgerEntry],
) -> int:
    """Re-queue every kept landing whose output this purge just deleted.

    A work unit is keyed by URL, so a hash two items shared survives on the
    survivor's claim — but its output lived in ``enrichment/<purged>/``,
    which went with the item that produced it. That leaves the survivor's
    URL ``done`` with nothing on disk, and seeding's already-a-unit
    short-circuit means no run ever fetches it again: the item owes
    nothing, so it derives ``enriched``, and the digest and query layers
    have no file to read. The line goes back to ``queued`` here, where the
    deletion is a fact this command knows, rather than being inferred later
    from a path that is missing for innocent reasons too (a rename moves
    the enrichment directory as well, and re-fetching on that would put a
    ``job: asset`` unit's repo path into the fetch queue, where no
    transport can take it).

    The line names the item that claims the work, not the purged one it was
    written under, for the same reason every other write does. The re-queue
    stamps this engine and today: "the output is gone, fetch it again" is a
    verdict THIS command reached, not one carried from the line it
    supersedes.

    Args:
        instance: The instance.
        on_record: Every item id excluded on the record — this batch and
            prior ones, whose directories are already gone. The whole
            record, so a landing a prior interrupted purge stranded is
            healed by whichever batch runs next, the same way the ledger
            purge sweeps against the record.
        claimed: Work hash -> the live items that still claim it
            (:func:`_surviving_claims`).
        now: Injected instant clock, UTC-aware — the ledger resolves each
            hash against the same present the purge did.
        stamp: The writer seam — :func:`ledger.stamp` with this command's
            clocks and engine bound.

    Returns:
        How many landings were re-queued.
    """
    try:
        kept = ledger.load(instance.ledger_path, now=now)
    except (OSError, UnicodeDecodeError, ledger.LedgerSchemaError):
        # A line this layer cannot read leaves the re-queue unrun rather
        # than the purge half-reported; the stranded landing then shows on
        # the health check as a done output gone from disk, and the next
        # verb names the unreadable line loudly.
        return 0
    deleted = {Path("enrichment") / item_id for item_id in on_record}
    stranded = [
        entry
        for entry in kept.values()
        if entry.status is Status.DONE
        and entry.path is not None
        and Path(entry.path).parent in deleted
        and not (instance.root / entry.path).exists()
    ]
    if not stranded:
        return 0
    for entry in stranded:
        # The stored string wins wherever it is still one of the claimants
        # (a unit two live items share must not migrate between them);
        # otherwise the first survivor by id, as seeding's dedupe orders.
        survivors = claimed.get(entry.hash, ())
        item = entry.item if entry.item in survivors or not survivors else survivors[0]
        ledger.append(
            instance.ledger_path,
            stamp(
                dataclasses.replace(
                    entry,
                    item=item,
                    status=Status.QUEUED,
                    path=None,
                    title=None,
                )
            ),
        )
    return len(stranded)


def _deduplicated(validated: list[_Exclusion]) -> list[_Exclusion]:
    """One entry per id, the first occurrence winning.

    The exclusions file is LLM-authored and runs in bulk, so the same id
    twice is a realistic input — and it is not an error: excluding an item
    is idempotent by construction (the TSV record is exactly what makes a
    re-run safe), so the second copy asks for what the first already did.
    Applied twice it wrote the id to ``state/exclusions.tsv`` twice — a
    permanent record — and counted the second pass as ``already gone``,
    reporting an item nothing had removed on the batch's only signal line.

    A second reason for an id is dropped with it; the batch states how many
    copies it collapsed so the count is never silently short.
    """
    by_id: dict[str, _Exclusion] = {}
    for exclusion in validated:
        by_id.setdefault(exclusion.item_id, exclusion)
    return list(by_id.values())


def _purge_counts(dropped: int, kept: int) -> str:
    return (
        f"{dropped} ledger entries dropped, {kept} kept (work another live corpus item "
        "still claims)"
    )


def _validated(instance: Instance, entry: object) -> _Exclusion:
    """One entry's types checked and its paths resolved inside the instance.

    Every value is LLM-authored, so none is trusted for its type: the batch
    runs in bulk and the operation is a recursive delete, so a wrong shape
    must be a refusal naming the entry, not a ``TypeError`` half way down
    the list. The id is checked for shape AND its paths resolved under the
    root: the shape catches an id that is a path, the resolution catches a
    corpus file, enrichment directory or digest symlinked out of the
    instance.

    Raises:
        ValueError: Any of those, naming the offending entry.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"exclusion entry is not an object: {entry!r}")
    item_id = entry.get("id")
    if not item_id:
        raise ValueError(f"exclusion entry has no id: {entry!r}")
    if not isinstance(item_id, str) or not _ITEM_ID_RE.fullmatch(item_id):
        raise ValueError(
            f"exclusion id is not a corpus item id: {item_id!r} (in {entry!r}) — an id "
            "names one file in the corpus tree, never a path"
        )
    raw_reason = entry.get("reason", _DEFAULT_REASON)
    if not isinstance(raw_reason, str):
        raise ValueError(
            f"exclusion reason for {item_id} must be a string, got "
            f"{type(raw_reason).__name__}: {entry!r}"
        )
    item_path = resolve_repo_path(instance.root, f"corpus/{item_id[:4]}/{item_id}.md")
    enrichment_path = resolve_repo_path(instance.root, f"enrichment/{item_id}")
    digest_path = resolve_repo_path(instance.root, f"state/digests/{item_id}.md")
    if item_path is None or enrichment_path is None or digest_path is None:
        raise ValueError(
            f"exclusion id {item_id!r} resolves outside the instance — an operation "
            "writes only inside this instance's root, so the batch is refused"
        )
    return _Exclusion(
        item_id=item_id,
        # The reason is LLM-authored free text: collapse every whitespace
        # run (tabs and newlines included) so the TSV stays one record per
        # line, tab-delimited, by construction.
        reason=" ".join(raw_reason.split()) or _DEFAULT_REASON,
        item_path=item_path,
        enrichment_path=enrichment_path,
        digest_path=digest_path,
    )


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-exclude <exclusions.json>."""
    parser = argparse.ArgumentParser(
        prog="dex-exclude",
        description="Permanently drop corpus items that yield nothing through this "
        "instance's lens, with their media, enrichment, digest, pass records and "
        'ledger entries (JSON list of {"id", "reason"}); survives re-normalization.',
    )
    parser.add_argument("file", type=Path, help="the exclusions JSON file")
    return parser


def _load_entries(path: Path) -> list[dict[str, object]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        raise ValueError(f"{path}: expected a JSON list of objects")
    return raw


def main(argv: list[str] | None = None) -> None:
    """Parse, build the Instance, exclude, print the summary."""
    args = build_parser().parse_args(argv)
    instance = Instance(root=Path.cwd())
    try:
        refuse_while_pending(instance.root)
        summary = run_exclude(instance, _load_entries(args.file))
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(f"dex-exclude: {e}")
    sys.stdout.write(summary + "\n")


if __name__ == "__main__":
    main()
