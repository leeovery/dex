"""Migration 2 — identity re-key, then rerun seed.

Two steps over one ledger read, in this order.

**Step 1 — identity re-key.** The rewrite moved canonical identity for two
kinds: x posts are keyed by status id (every share spelling of one post
collapses to ``x.com/i/status/<id>``), and youtube's single-video shapes
(``/live/``, ``/shorts/``, ``/embed/``, youtu.be) collapse to
``watch?v=<id>``. A pre-rewrite entry's stored hash is therefore no longer
the hash the engine computes for its URL — and left alone, the first run's
corpus seeding would mint fresh queued units under the new hashes and
re-fetch work already done, or worse, work deliberately parked: a
``manual`` verdict resurrected as a fresh fetch. So EVERY readable entry is
re-keyed first: recompute ``work_hash(canonical_url(entry.url))`` and,
where it differs from the stored hash, rewrite the line in place under the
new hash + canonical URL with every other field preserved — statuses are
sacred; a ``manual`` verdict stays ``manual`` under its new identity.
Where two old entries collapse to one new hash (the x.com and twitter.com
spellings of one post), file order is kept, so the ledger's own
last-per-hash rule decides — the later line wins — and the report names
the collapse. The rewrite is atomic; a second apply finds every hash
already current and re-keys nothing.

**Step 2 — rerun seed.** Two fixes shipped with the rewrite make old
stored enrichments deficient:

- **web**: the old extractor stripped hyperlinks; the rewrite keeps them
  (links are harvest input);
- **x**: thread walk-up did not exist; a stored single post may have been a
  thread, and the rerun walks up and re-harvests.

Membership is every pre-rewrite ``done`` web/x entry **whose item still
exists**: this migration runs directly after migration 1, and the old
engine had *neither* fix. The item check is not bookkeeping — an entry
whose ``corpus/<id[:4]>/<id>.md`` is gone names work the owner purged
(``dex exclude`` deletes the item and its enrichment, on the record in
``state/exclusions.tsv``), and seeding it would re-fetch content ruled out
of scope, re-mint an enrichment directory under a dead item, and put an
owner ruling back in the queue. Those entries are skipped-with-why, naming
the exclusion where one is on record.

Pre-rewrite is precise, not fuzzy:
migration 1 stamps un-versioned old lines ``engine: 0.0.1``, the only
version the pre-rewrite engine ever shipped as; entries written by the
rewritten engine always carry a newer version. Only ``done`` entries are
seeded — old ``error`` entries already retry under the new-engine rule, and
``manual`` entries stay parked for judgment.

The seed is queue seeding with provenance, one of the two permitted acts:
the existing URL-keyed work unit re-enters as ``{status: queued, rerun:
true, via: migration-2}`` — a real URL through the front door; the pipeline
does the actual work with current code. The re-key pass already ran, so a
qualifying entry's hash and URL ARE the current identity: the seed appends
under them directly, the next run's corpus seeding dedupes against it, and
the drain fetches once.

Idempotent both ways: the re-key pass moves nothing on a second apply, and
a seeded hash's latest line is the queued seed — no longer ``done``, so it
never seeds twice.
"""

import datetime
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from dex_engine import atomic
from dex_engine.migrations import MigrationError
from dex_engine.pipeline.detect import canonical_url
from dex_engine.pipeline.ledger import LedgerSchemaError, append, from_line, to_line
from dex_engine.pipeline.registry import DRIVERS
from dex_engine.pipeline.types import (
    Kind,
    LedgerEntry,
    MigrationReport,
    Skipped,
    Status,
    parse_version,
)
from dex_engine.pipeline.urls import work_hash

__all__ = ["IdentityRekeyAndRerunSeed", "build"]

NUMBER = 2
INTENT = (
    "identity re-key + rerun seed: every ledger entry moves to its current canonical "
    "identity (x id-keying, youtube shape collapse) with status preserved; then every "
    "pre-rewrite done web/x entry requeues as {queued, rerun, via: migration-2} — the "
    "old engine (0.0.1) had neither the link-keeping fix (web) nor thread walk-up (x)"
)

_PRE_REWRITE = (0, 0, 1)
_SEEDED_KINDS = frozenset({Kind.WEB, Kind.X})


def build(
    *,
    today: Callable[[], datetime.date],
    engine_version: str,
) -> "IdentityRekeyAndRerunSeed":
    """Build migration 2 with the injected clock and running engine version.

    Raises:
        MigrationError: The running engine identifies as 0.0.1 — the
            pre-rewrite marker. Seeds stamped with it would satisfy this
            migration's own membership test and corrupt it: a re-run would
            reseed the seeds. The rewrite ships as >= 0.1.0 by construction
            (pyproject); hitting this guard means a mis-built engine.
    """
    if parse_version(engine_version) == _PRE_REWRITE:
        raise MigrationError(
            f"migration 2 refuses to run as engine {engine_version!r}: that version is "
            "the pre-rewrite marker (migration 1's stamp) — seeds stamped with it "
            "would corrupt their own membership test; a rewrite engine is >= 0.1.0"
        )
    return IdentityRekeyAndRerunSeed(today=today, engine_version=engine_version)


class IdentityRekeyAndRerunSeed:
    """Migration 2: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def __init__(self, *, today: Callable[[], datetime.date], engine_version: str) -> None:
        """Seeds are stamped with the injected clock and running engine."""
        self._today = today
        self._engine_version = engine_version

    def apply(self, root: Path) -> MigrationReport:
        """Re-key the ledger at ``root`` to current identities, then seed reruns.

        Tolerates un-pulled repos (no ledger → nothing to do) and lines
        migration 1 could not translate (skipped here too, with the same
        repair pointer — they cannot poison the readable lines).
        """
        actions: list[str] = []
        skipped: list[Skipped] = []
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport(actions=actions, skipped=skipped, anomalies=[])
        entries = _rekey_identities(path, actions, skipped)
        latest: dict[str, LedgerEntry] = {}
        for entry in entries:
            latest[entry.hash] = entry
        exclusions = _exclusions(root)
        counts = {Kind.X: 0, Kind.WEB: 0}
        for entry in latest.values():
            if not _qualifies(entry, root=root, exclusions=exclusions, skipped=skipped):
                continue
            append(
                path,
                LedgerEntry(
                    hash=entry.hash,
                    url=entry.url,
                    item=entry.item,
                    kind=entry.kind,
                    status=Status.QUEUED,
                    engine=self._engine_version,
                    date=self._today(),
                    via="migration-2",
                    rerun=True,
                ),
            )
            counts[entry.kind] += 1
        seeded = counts[Kind.X] + counts[Kind.WEB]
        if seeded:
            actions.append(
                f"seeded {seeded} rerun(s): {counts[Kind.X]} x (thread walk-up), "
                f"{counts[Kind.WEB]} web (link keeping)"
            )
        return MigrationReport(actions=actions, skipped=skipped, anomalies=[])


def _rekey_identities(
    path: Path, actions: list[str], skipped: list[Skipped]
) -> list[LedgerEntry]:
    """Rewrite every entry whose identity moved; return readable entries in file order.

    Not ``ledger.load``: a hand-tampered line would abort the whole load,
    and this migration must still handle everything readable — unreadable
    lines are skipped-with-why and kept verbatim in the file. Lines whose
    identity is unchanged also keep their original bytes, so a second apply
    leaves the file untouched. Where re-keying makes two old identities
    share one hash, both keep their file positions: last-per-hash decides,
    exactly as it does for any superseded line.
    """
    original = path.read_text(encoding="utf-8")
    out_lines: list[str] = []
    entries: list[LedgerEntry] = []
    identity: dict[str, str] = {}
    rekeyed = False
    for lineno, line in enumerate(original.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            entry = from_line(line)
        except LedgerSchemaError as e:
            skipped.append(
                Skipped(
                    what=f"ledger line {lineno}",
                    why=f"unreadable under the current schema ({e}) — repair it per "
                    "migration 1's report; not re-keyed or considered for seeding",
                )
            )
            out_lines.append(line)
            continue
        canonical = canonical_url(entry.url, DRIVERS)
        unit_hash = work_hash(canonical)
        identity[entry.hash] = unit_hash
        if unit_hash != entry.hash:
            entry = replace(entry, hash=unit_hash, url=canonical)
            out_lines.append(to_line(entry))
            rekeyed = True
        else:
            out_lines.append(line)
        entries.append(entry)
    if rekeyed:
        atomic.write_text(path, "".join(out_line + "\n" for out_line in out_lines))
        moved = sum(1 for old, new in identity.items() if old != new)
        landings: dict[str, set[str]] = {}
        for old, new in identity.items():
            landings.setdefault(new, set()).add(old)
        collapsed = sum(len(olds) - 1 for olds in landings.values() if len(olds) > 1)
        action = (
            f"re-keyed {moved} entr{'y' if moved == 1 else 'ies'} to current "
            "identities (x id-keying, youtube shape collapse)"
        )
        if collapsed:
            action += (
                f"; {collapsed} collapsed duplicate(s) — old spellings of one unit, "
                "settled by last-per-hash"
            )
        actions.append(action)
    return entries


def _exclusions(root: Path) -> dict[str, str]:
    """``state/exclusions.tsv`` as item id -> stated reason.

    Missing file, blank lines and reason-less rows are all tolerated: this
    is a corroborating record, not an authority — the corpus file's absence
    is what decides, and the reason only makes the report readable.
    """
    path = root / "state" / "exclusions.tsv"
    if not path.exists():
        return {}
    reasons: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        item, _, reason = line.partition("\t")
        reasons[item.strip()] = reason.strip()
    return reasons


def _qualifies(
    entry: LedgerEntry, *, root: Path, exclusions: dict[str, str], skipped: list[Skipped]
) -> bool:
    if entry.status is not Status.DONE or entry.kind not in _SEEDED_KINDS:
        return False
    try:
        if parse_version(entry.engine) != _PRE_REWRITE:
            return False
    except ValueError:
        skipped.append(
            Skipped(
                what=f"ledger entry {entry.hash}",
                why=f"done {entry.kind.value} entry with unparseable engine "
                f"{entry.engine!r} — cannot judge pre-rewrite membership; requeue by "
                "hand if its enrichment predates the rewrite",
            )
        )
        return False
    item_path = f"corpus/{entry.item[:4]}/{entry.item}.md"
    if (root / item_path).exists():
        return True
    if entry.item in exclusions:
        reason = exclusions[entry.item] or "no reason recorded"
        why = (
            f"{item_path} is gone and the item is excluded on the record "
            f"(state/exclusions.tsv: {reason}) — excluded, never reseeded"
        )
    else:
        why = (
            f"{item_path} does not exist and no state/exclusions.tsv record names the "
            "item — it was removed by hand or a purge was interrupted; not reseeded, "
            "because there is no item for the rerun's output to belong to"
        )
    skipped.append(Skipped(what=f"rerun seed for {entry.item}", why=why))
    return False
