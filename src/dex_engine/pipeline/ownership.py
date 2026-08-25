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
"""

from collections.abc import Sequence
from pathlib import Path

from dex_engine import corpus

from .detect import canonical_url
from .types import SourceDriver
from .urls import work_hash

__all__ = ["corpus_owners", "work_identity"]


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
    except Exception:  # noqa: BLE001 — a driver's canonical is arbitrary code over owner data
        return work_hash(url)
    return work_hash(canonical)


def corpus_owners(root: Path, drivers: Sequence[SourceDriver]) -> dict[str, str]:
    """Map work hash -> the live corpus item that claims it.

    Unreadable items are skipped silently: every caller of this map already
    reports malformed corpus files through its own pass, and one bad
    frontmatter must not decide that everything else is unclaimed.

    Args:
        root: The instance root.
        drivers: The driver registry that owns canonicalization.

    Returns:
        Work hash -> owning item id. Where two items list one URL the
        first by id wins, as seeding's two-items dedupe does.
    """
    owners: dict[str, str] = {}
    corpus_dir = root / "corpus"
    if not corpus_dir.is_dir():
        return owners
    for path in sorted(corpus_dir.glob("*/*.md")):
        try:
            item = corpus.read_item(path)
        except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError):
            continue
        for url in item.urls:
            owners.setdefault(work_identity(url, drivers), item.id)
        for repo_path in item.media:
            owners.setdefault(work_hash(f"file:{repo_path}"), item.id)
    return owners
