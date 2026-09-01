"""dex-exclude: purge out-of-scope corpus items, permanently and on the record.

``bin/dex exclude <exclusions.json>`` — the file is a JSON list of
``{"id": ..., "reason": ...}`` records, written by the scope-filter pass.

Each exclusion appends to ``state/exclusions.tsv`` (consulted by
``normalize.py`` so excluded clusters are never regenerated), removes
``corpus/<year>/<id>.md``, removes ``enrichment/<id>/``, removes
``state/digests/<id>.md``, and removes the item's ledger entries — the
item is gone, so seeding will never raise that work again and the lines
would otherwise linger forever. The digest goes with the rest: it is a
permanent fact index over content this instance has just ruled out of
scope, and nothing else ever deletes one, so leaving it behind keeps a
permanently excluded item feeding query and wiki forever.

Except the work another live corpus item still claims. A work unit is
keyed by URL, not by item, so two items listing one URL share one ledger
entry that names only one of them; purging on the name alone would delete
a history the survivor still owns. The claim is asked of the one ownership
resolution every ledger reader routes through
(:func:`dex_engine.pipeline.ownership.unit_owners`), not of frontmatter
alone: a child promoted under the excluded item appears in no ``urls:``/
``media:``, so its only corpus answer travels its ``parent`` chain — where
the chain ends at work a survivor lists, the child is the survivor's too.
The summary states both counts — this
runs in bulk from the scope-filter pass, so that line is the owner's only
signal. A corpus file that cannot be read claims nothing, which would make
that veto fail open toward deletion, so a batch that meets one purges no
ledger entries at all and the summary says why.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import corpus, instance_map
from .pipeline import ledger
from .pipeline.ownership import unit_owners
from .pipeline.registry import default_drivers
from .pipeline.types import Instance, LedgerEntry, Status
from .pipeline.urls import resolve_repo_path
from .version import engine_version

__all__ = ["build_parser", "main", "run_exclude"]

_DEFAULT_REASON = "out of scope"


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


def run_exclude(
    instance: Instance,
    entries: list[dict[str, object]],
    *,
    today: Callable[[], datetime.date] = _today,
    now: Callable[[], datetime.datetime] = _utc_now,
    version: Callable[[], str] = engine_version,
) -> str:
    """Exclude the given items: record why, delete item, enrichment, digest and ledger entries.

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
            or has a non-string ``reason``. The whole batch is refused
            before anything is written or deleted. Also raised when the
            purge landed and the map then failed to recompile — the
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
    """The purge itself: record, remove, sweep the ledger, re-queue strands."""
    validated = _deduplicated([_validated(instance, entry) for entry in entries])
    removed, missing, digests = _record_and_remove(instance, validated)
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
    collapsed = len(entries) - len(validated)
    counted = f"{len(validated)}"
    if collapsed:
        counted += f" ({collapsed} duplicate id(s) collapsed)"
    summary = (
        f"excluded {counted}: removed {removed} items ({missing} already gone), {digests} digests, "
    )
    if not instance.ledger_path.exists():
        return summary + _purge_counts(0, 0)
    unreadable = _unreadable_corpus_items(instance.root)
    if unreadable:
        # The claim veto decides a deletion, and a corpus file that cannot
        # be read cannot say which work it claims: purging anyway would
        # take a live item's history and report a clean drop. So nothing is
        # purged until the file is readable — the item and its enrichment
        # still go, and the re-run purges.
        return (
            summary
            + _purge_counts(0, 0)
            + f"; {unreadable} corpus file(s) could not be read, so no ledger entries "
            "were purged — repair them (dex-lint names them) and re-run"
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


def _record_and_remove(instance: Instance, validated: list[_Exclusion]) -> tuple[int, int, int]:
    """Record each exclusion and remove what it owns; return the three counts.

    The TSV record lands first for every entry, because it is what makes a
    re-run idempotent: an interruption after it leaves an id recorded whose
    deletions the re-run finishes.
    """
    exclusions = instance.state_dir / "exclusions.tsv"
    existing = _on_record(exclusions)
    removed = missing = digests = 0
    exclusions.parent.mkdir(parents=True, exist_ok=True)
    with exclusions.open("a", encoding="utf-8") as f:
        for exclusion in validated:
            if exclusion.item_id not in existing:
                f.write(f"{exclusion.item_id}\t{exclusion.reason}\n")
            if exclusion.item_path.exists():
                exclusion.item_path.unlink()
                removed += 1
            else:
                missing += 1
            shutil.rmtree(exclusion.enrichment_path, ignore_errors=True)
            if exclusion.digest_path.exists():
                exclusion.digest_path.unlink()
                digests += 1
    return removed, missing, digests


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


def _unreadable_corpus_items(root: Path) -> int:
    """How many corpus files cannot be read at all.

    The corpus claims under ``unit_owners`` skip them silently
    (``corpus_claims``), rightly for its other callers — but here its map
    is a delete decision, and it fails OPEN: a hash claimed only by an item
    whose frontmatter will not parse reads as claimed by nobody, and the
    hash's whole history goes, reported as a clean drop. Counted separately
    (a second parse, on a command that runs rarely and in bulk) rather than
    by changing what that map means to every caller.
    """
    corpus_dir = root / "corpus"
    if not corpus_dir.is_dir():
        return 0
    return sum(1 for path in sorted(corpus_dir.glob("*/*.md")) if not _readable(path))


def _readable(path: Path) -> bool:
    try:
        corpus.read_item(path)
    except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError):
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree: dex-exclude <exclusions.json>."""
    parser = argparse.ArgumentParser(
        prog="dex-exclude",
        description="Permanently exclude out-of-scope corpus items "
        '(JSON list of {"id", "reason"}); survives re-normalization.',
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
        summary = run_exclude(instance, _load_entries(args.file))
    except (OSError, ValueError) as e:
        sys.exit(f"dex-exclude: {e}")
    sys.stdout.write(summary + "\n")


if __name__ == "__main__":
    main()
