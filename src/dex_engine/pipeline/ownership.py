"""Which live corpus item claims a work unit.

A ledger entry's ``item`` is the attribution as of the day its line was
written. Renaming an item — same shortid, new slug — leaves every earlier
line naming an id with no file behind it, so asking whether that stored
STRING still has a corpus file answers a different question from the one
callers mean: a renamed item reads as purged, and the work reads as
orphaned when it is not.

The live question is asked of the corpus instead. An item's ``urls:`` and
``media:`` are what the runtime seeds work from, so hashing them exactly as
seeding does yields, for every work unit the corpus still claims, the id of
the item claiming it — whatever any ledger line says. A unit missing from
that map is claimed by no live corpus item at all, which is the finding
worth reporting.

Frontmatter claims only reach the units frontmatter names, though: a
harvested child, a media download and an extracted asset are never listed
in any ``urls:``/``media:``, so their only corpus answer travels through
their ``parent`` chain — which is :func:`unit_owners`' job, the one
resolution every ledger reader routes ownership through.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

from dex_engine import corpus

from .detect import canonical_url
from .types import LedgerEntry, SourceDriver
from .urls import work_hash

__all__ = ["corpus_claims", "corpus_owners", "unit_owners", "work_identity"]


def work_identity(url: str, drivers: Sequence[SourceDriver]) -> str:
    """The work hash the runtime keys ``url`` under.

    Mirrors seeding: the canonical form's hash, or — where the driver
    refuses the URL, as the bad-seed park does — the raw URL's hash, so a
    malformed capture URL still resolves to the identity it was seeded
    under.

    Args:
        url: A URL as written in corpus frontmatter.
        drivers: The driver registry that owns canonicalization.

    Returns:
        The work hash.
    """
    try:
        canonical = canonical_url(url, drivers)
    except ValueError:
        # Every refusal reaches here as a ValueError, whatever the driver
        # raised — `canonical_url` types it — so this fallback and the
        # bad-seed park key the unit identically.
        return work_hash(url)
    return work_hash(canonical)


def corpus_claims(root: Path, drivers: Sequence[SourceDriver]) -> dict[str, tuple[str, ...]]:
    """Map work hash -> EVERY live corpus item that claims it.

    One work unit is keyed by URL, so two items listing one URL share it,
    and a hash claimed by more than one live item is common in a real
    instance. Seeding hands the enrichment file to the first of them and
    the ledger line names only that one, but both items genuinely owe it: an
    outstanding unit holds every item that lists it out of digest, not just
    the one whose name the line happens to carry.

    Unreadable items are skipped silently: every caller of this map already
    reports malformed corpus files through its own pass, and one bad
    frontmatter must not decide that everything else is unclaimed.

    Args:
        root: The instance root.
        drivers: The driver registry that owns canonicalization.

    Returns:
        Work hash -> the claiming item ids, in id order.
    """
    claims: dict[str, list[str]] = {}
    corpus_dir = root / "corpus"
    if not corpus_dir.is_dir():
        return {}
    for path in sorted(corpus_dir.glob("*/*.md")):
        try:
            item = corpus.read_item(path)
        except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError):
            continue
        for url in item.urls:
            claims.setdefault(work_identity(url, drivers), []).append(item.id)
        for repo_path in item.media:
            claims.setdefault(work_hash(f"file:{repo_path}"), []).append(item.id)
    return {unit_hash: tuple(sorted(set(ids))) for unit_hash, ids in claims.items()}


def corpus_owners(root: Path, drivers: Sequence[SourceDriver]) -> dict[str, str]:
    """Map work hash -> the live corpus item that claims it.

    The single-claimant view of :func:`corpus_claims`, for the callers that
    want one name to put on a line — a report row, a re-queue, a purge's
    claim veto.

    Args:
        root: The instance root.
        drivers: The driver registry that owns canonicalization.

    Returns:
        Work hash -> owning item id. Where two items list one URL the
        first by id wins, as seeding's two-items dedupe does.
    """
    return {unit_hash: ids[0] for unit_hash, ids in corpus_claims(root, drivers).items() if ids}


def unit_owners(
    root: Path,
    entries: Mapping[str, LedgerEntry],
    drivers: Sequence[SourceDriver],
) -> dict[str, tuple[str, ...]]:
    """Which live corpus items each work unit belongs to — the one resolution.

    A ledger line's ``item`` is the attribution as of the day it was
    written, and migration 1 carries a stated item verbatim forever — so a
    unit of an item RENAMED since then names an id no corpus file answers
    to, and reading ownership off that string hands the work to a dead id.
    The corpus is the source of truth, so the corpus is asked first
    (:func:`corpus_claims`) — and every reader (lint's rows, the digest
    backstop, status, the run report) and every write
    (``run._Drain.owner_of``) resolves through this one map, which is why
    no line is ever rewritten to correct a stale one.

    Three answers, in the order migration 2 established:

    1. Every live item that lists the URL — plus the entry's own item where
       its corpus file exists, which answers first for a unit an owner has
       since removed from the frontmatter it was seeded from.
    2. Failing that, the parent's owners: a harvest-promoted child, a media
       download and an extracted asset are never listed in any frontmatter,
       so the only corpus answer they have is the one their parent gets —
       the ``parent`` chain is walked up to an entry whose hash IS
       frontmatter-claimed, and the claimant owns the whole chain.
    3. Failing that, the stored string as written. A unit no live item
       claims belongs to whoever the line says — which is lint's ghost-item
       finding to report, not this map's to silently reassign.

    Args:
        root: The instance root.
        entries: The ledger, hash -> latest entry.
        drivers: The driver registry that owns canonicalization.

    Returns:
        Work hash -> the owning item ids, in id order. Every hash in
        ``entries`` has an answer; none is ever empty.
    """
    claims = corpus_claims(root, drivers)
    corpus_dir = root / "corpus"
    live = {path.stem for path in corpus_dir.glob("*/*.md")} if corpus_dir.is_dir() else set()
    owners: dict[str, tuple[str, ...]] = {}

    def resolve(unit_hash: str, seen: frozenset[str]) -> tuple[str, ...]:
        cached = owners.get(unit_hash)
        if cached is not None:
            return cached
        entry = entries.get(unit_hash)
        if entry is None:
            return claims.get(unit_hash, ())
        claimants = set(claims.get(unit_hash, ()))
        if entry.item in live:
            claimants.add(entry.item)
        if claimants:
            resolved = tuple(sorted(claimants))
        elif entry.parent is not None and entry.parent not in seen:
            # Cycle-guarded: a hand-edited `parent` pointing back into its
            # own chain must cost a lookup, never the run.
            resolved = resolve(entry.parent, seen | {unit_hash}) or (entry.item,)
        else:
            resolved = (entry.item,)
        owners[unit_hash] = resolved
        return resolved

    for unit_hash in entries:
        resolve(unit_hash, frozenset())
    return owners
