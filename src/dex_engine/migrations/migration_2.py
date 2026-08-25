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
the collapse. A moved hash takes its pointers with it: a child's ``parent``
naming an old hash is rewritten to the new one in the same pass, so no
chain is left dangling. **The unit's output file is one of those
pointers.** It is named ``<kind>-<hash6>.md``, so a re-key that moved only
the ledger stranded it under an identity nothing computes any more: the
rerun seeded below landed BESIDE it and the item carried two views of one
unit, both listed in engine-owned ``enrichment:`` frontmatter, and a
re-keyed youtube park lost the stored description the transcribe drain
reads back out of that file. So the file moves with the hash
(:func:`_rekey_outputs`) — as migration 1 moves the outputs its own
rewrite renamed — and the entry's stored ``path`` follows the file. The
rewrite is atomic; a second apply finds every hash already current and
re-keys nothing.

Not every entry is canonically keyed, and the ones that aren't are left
exactly as they are — re-keying them would move them AWAY from the
identity the runtime computes:

- ``via: media`` — media URLs are fetched verbatim, signed query params and
  all; canonicalizing one strips the signature and mints a key nothing
  will ever look up again;
- ``via: extract-asset`` — the work key is a repo path, not a URL;
  canonicalization would mangle it into ``https:enrichment/…``;
- any entry whose URL is not an absolute http(s) URL (a ``file:`` work key,
  a bare repo path) — same reason, generalized.

**Step 2 — rerun seed.** Two fixes shipped with the rewrite make old
stored enrichments deficient:

- **web**: the old extractor stripped hyperlinks; the rewrite keeps them
  (links are harvest input);
- **x**: thread walk-up did not exist; a stored single post may have been a
  thread, and the rerun walks up and re-harvests.

Membership is every pre-rewrite ``done`` web/x entry **whose work a live
corpus item still claims**: this migration runs directly after migration 1,
and the old engine had *neither* fix. That check is not bookkeeping —
seeding work no item claims would re-fetch content the owner purged
(``dex exclude`` deletes the item and its enrichment, on the record in
``state/exclusions.tsv``), re-mint an enrichment directory under a dead
item, and put an owner ruling back in the queue.

The claim is asked of the corpus, never of the entry's stored ``item``
string alone. An item RENAMED since the line was written — same shortid,
new slug — has a live corpus file under a new id, and its old id names
nothing; a stored-string check reads that as a purge and refuses a rerun
the owner never ruled out. So: an entry whose stored item still has a
corpus file belongs to it; otherwise, an entry whose URL is still listed
by a live corpus item belongs to THAT item and is seeded re-attributed to
it (the report counts the re-attributions). Only an entry no live item
claims is skipped-with-why — naming the exclusion where one is on record,
and otherwise stating what was checked rather than guessing a cause.

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
from dataclasses import dataclass, replace
from pathlib import Path

from dex_engine import atomic
from dex_engine.migrations import MigrationError
from dex_engine.pipeline.detect import canonical_url
from dex_engine.pipeline.enrichment import read_enrichment
from dex_engine.pipeline.ledger import (
    LedgerSchemaError,
    append,
    from_line,
    resolution_key,
    stamp,
    to_line,
)
from dex_engine.pipeline.ownership import corpus_owners
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
# Provenance whose work key is the fetch string verbatim, never a canonical
# URL — see the module docstring.
_VERBATIM_VIA = frozenset({"media", "extract-asset"})
_ABSOLUTE_HTTP = ("http://", "https://")


def build(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> "IdentityRekeyAndRerunSeed":
    """Build migration 2 with the injected clocks and running engine version.

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
    return IdentityRekeyAndRerunSeed(today=today, now=now, engine_version=engine_version)


class IdentityRekeyAndRerunSeed:
    """Migration 2: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def __init__(
        self,
        *,
        today: Callable[[], datetime.date],
        now: Callable[[], datetime.datetime],
        engine_version: str,
    ) -> None:
        """Seeds are stamped with the injected clocks and running engine."""
        self._today = today
        self._now = now
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
        entries = _rekey_identities(root, path, actions, skipped)
        latest = _latest_per_hash(entries, now=self._now())
        exclusions = _exclusions(root)
        cohort = [entry for entry in latest.values() if _in_cohort(entry, skipped=skipped)]
        # The corpus scan answers one question — which live item claims this
        # work — and only entries whose stored item is already gone need to
        # ask it, so a run with nothing missing never pays for it.
        owners = (
            corpus_owners(root, DRIVERS)
            if any(not (root / _item_path(entry.item)).exists() for entry in cohort)
            else {}
        )
        counts = {Kind.X: 0, Kind.WEB: 0}
        reattributed = 0
        for entry in cohort:
            item = _live_item(
                entry, root=root, owners=owners, exclusions=exclusions, skipped=skipped
            )
            if item is None:
                continue
            append(
                path,
                # Stamped, never hand-dated: `at` is the writer's own clock
                # everywhere in the engine, and a seed is a write like any
                # other (ledger.stamp sets date/at/engine together).
                stamp(
                    LedgerEntry(
                        hash=entry.hash,
                        url=entry.url,
                        item=item,
                        kind=entry.kind,
                        status=Status.QUEUED,
                        engine="seed",  # stamped below
                        date=datetime.date.min,
                        via="migration-2",
                        rerun=True,
                    ),
                    today=self._today,
                    now=self._now,
                    engine_version=self._engine_version,
                ),
            )
            counts[entry.kind] += 1
            if item != entry.item:
                reattributed += 1
        seeded = counts[Kind.X] + counts[Kind.WEB]
        if seeded:
            action = (
                f"seeded {seeded} rerun(s): {counts[Kind.X]} x (thread walk-up), "
                f"{counts[Kind.WEB]} web (link keeping)"
            )
            if reattributed:
                action += (
                    f"; {reattributed} re-attributed to the renamed item whose urls: "
                    "still lists them"
                )
            actions.append(action)
        return MigrationReport(actions=actions, skipped=skipped, anomalies=[])


@dataclass(frozen=True, slots=True, kw_only=True)
class _Line:
    """One ledger line as read: its bytes, its entry, and its current key."""

    text: str
    entry: LedgerEntry | None = None
    # (hash, canonical url) where the entry is canonically keyed; None where
    # its key is verbatim by design or canonicalization refused the URL
    key: tuple[str, str] | None = None


def _rekey_identities(
    root: Path, path: Path, actions: list[str], skipped: list[Skipped]
) -> list[LedgerEntry]:
    """Rewrite every entry whose identity moved; return readable entries in file order.

    Not ``ledger.load``: a hand-tampered line would abort the whole load,
    and this migration must still handle everything readable — unreadable
    lines are skipped-with-why and kept verbatim in the file. Lines whose
    identity is unchanged also keep their original bytes, so a second apply
    leaves the file untouched. Where re-keying makes two old identities
    share one hash, both keep their file positions: last-per-hash decides,
    exactly as it does for any superseded line.

    Two reads, one write: the whole file is keyed first, because a child's
    ``parent`` may name a hash whose line comes later, and every pointer to
    a moved hash moves with it. The unit's output file on disk is a pointer
    too and moves in the same pass (:func:`_rekey_outputs`), before the
    lines are written, so a stored ``path`` is rewritten only where the
    file it names actually moved.
    """
    original = path.read_text(encoding="utf-8")
    records: list[_Line] = []
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
            records.append(_Line(text=line))
            continue
        records.append(
            _Line(text=line, entry=entry, key=_current_key(entry, lineno=lineno, skipped=skipped))
        )
    identity = {
        record.entry.hash: record.key[0]
        for record in records
        if record.entry is not None and record.key is not None
    }
    renames = _rekey_outputs(root, records, actions, skipped)
    out_lines: list[str] = []
    entries: list[LedgerEntry] = []
    reparented = 0
    rekeyed = False
    for record in records:
        entry = record.entry
        if entry is None:
            out_lines.append(record.text)
            continue
        unit_hash, url = record.key if record.key is not None else (entry.hash, entry.url)
        parent = entry.parent if entry.parent is None else identity.get(entry.parent, entry.parent)
        output = renames.get(entry.path, entry.path) if entry.path is not None else None
        if unit_hash != entry.hash or parent != entry.parent or output != entry.path:
            if parent != entry.parent:
                reparented += 1
            entry = replace(entry, hash=unit_hash, url=url, parent=parent, path=output)
            out_lines.append(to_line(entry))
            rekeyed = True
        else:
            out_lines.append(record.text)
        entries.append(entry)
    if rekeyed:
        atomic.write_text(path, "".join(out_line + "\n" for out_line in out_lines))
        actions.append(_rekey_action(identity, reparented))
    return entries


def _rekey_outputs(
    root: Path, records: list[_Line], actions: list[str], skipped: list[Skipped]
) -> dict[str, str]:
    """Move each re-keyed unit's enrichment file onto its new identity.

    An output is named ``<kind>-<hash6>.md``, so a re-key that touches only
    the ledger leaves the file named for an identity the unit no longer
    has, and nothing in the engine can reach it again: the drain's
    superseded-output drop builds its candidate names from the NEW hash, so
    the rerun this migration seeds lands beside the stale file and the item
    carries two views of one unit — both listed in engine-owned
    ``enrichment:`` frontmatter, which is derived from the directory. Worse
    for a park: the transcribe drain reads a youtube park's stored
    description back out of ``<kind>-<hash6>.md``, so a re-keyed park loses
    the description the park wrote, silently. Migration 1 renames its
    outputs for exactly this reason — its rewrite moved the kind half of
    the name; this moves the hash half.

    Every kind prefix is tried, not only the entry's: a unit redetected
    since it was written has its output under the older kind. Each
    candidate must PROVE it belongs to this unit by the URL it records —
    ``hash6`` is six hex digits, and two units under one item sharing one
    is common enough that the drain's own drop guards against it. A target
    that already exists is refused and reported, never overwritten: that is
    the two old spellings of one x post landing on one identity, and each
    file is a view someone may still want.

    Args:
        root: The instance root.
        records: Every line as read, in file order.
        actions: Appended to when files moved.
        skipped: Appended to for a refused move.

    Returns:
        Ledger ``path`` values that moved, old -> new. Keyed by the whole
        stored path so a superseded line naming the same file follows it,
        and a refused move leaves every reference where it was.
    """
    renames: dict[str, str] = {}
    for record in records:
        entry, key = record.entry, record.key
        if entry is None or key is None or key[0] == entry.hash:
            continue
        item_dir = root / "enrichment" / entry.item
        for kind in Kind:
            stale = item_dir / f"{kind.value}-{entry.hash[:6]}.md"
            # A file the first line of this hash already moved is gone;
            # its stored path follows through `renames`.
            if not stale.is_file():
                continue
            try:
                fields, _body = read_enrichment(stale)
            except (OSError, UnicodeDecodeError):
                # One unreadable file must never abort the chain part-way
                # through: it simply cannot prove whose output it is.
                skipped.append(
                    Skipped(
                        what=f"enrichment/{entry.item}/{stale.name}",
                        why="could not be read, so it cannot prove which unit it belongs "
                        "to — left on the old identity; repair it and rename it by hand",
                    )
                )
                continue
            if fields.get("url") != entry.url:
                continue  # a hash6 neighbour's output, not this unit's
            target = stale.with_name(f"{kind.value}-{key[0][:6]}.md")
            if target.exists():
                skipped.append(
                    Skipped(
                        what=f"enrichment/{entry.item}/{stale.name}",
                        why=f"{target.name} already exists beside it — refusing to "
                        f"overwrite; {stale.name} is an older identity's view of the same "
                        "unit and is left in place, no longer named by the ledger — merge "
                        "the two by hand",
                    )
                )
                continue
            stale.rename(target)
            renames[f"enrichment/{entry.item}/{stale.name}"] = (
                f"enrichment/{entry.item}/{target.name}"
            )
    if renames:
        actions.append(
            f"enrichment: {len(renames)} output file(s) renamed onto the new hash "
            "(<kind>-<hash6>.md follows the identity, or the rerun lands beside it)"
        )
    return renames


def _current_key(
    entry: LedgerEntry, *, lineno: int, skipped: list[Skipped]
) -> tuple[str, str] | None:
    """The identity the engine computes for this entry today, or None to leave it.

    None means the line keeps its stored hash and URL: either its key is
    verbatim by design (see the module docstring), or canonicalization
    refused the URL — and one unreadable URL must never abort a migration
    chain part-way through, leaving state half-moved.
    """
    if entry.via in _VERBATIM_VIA or not entry.url.startswith(_ABSOLUTE_HTTP):
        return None
    try:
        canonical = canonical_url(entry.url, DRIVERS)
    except Exception as e:  # noqa: BLE001 — a driver's canonical is arbitrary code over owner data
        skipped.append(
            Skipped(
                what=f"ledger line {lineno}",
                why=f"canonicalizing {entry.url!r} failed ({type(e).__name__}: "
                f"{' '.join(str(e).split())}) — left on its stored hash; repair the URL "
                "with `enrich mark`, or accept that this unit keeps its old identity",
            )
        )
        return None
    return work_hash(canonical), canonical


def _rekey_action(identity: dict[str, str], reparented: int) -> str:
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
    if reparented:
        action += f"; {reparented} parent pointer(s) followed to the new hash"
    return action


def _latest_per_hash(
    entries: list[LedgerEntry], *, now: datetime.datetime
) -> dict[str, LedgerEntry]:
    """The live line per hash, resolved exactly as ``ledger.load`` resolves it.

    Through ``resolution_key``, never by file position alone: membership is
    read off the live line, and a re-application runs against state the
    engine has since written — where a hash's newest line is one another
    machine's union merge left mid-file, position would name a superseded
    line and reseed work already settled.
    """
    latest: dict[str, LedgerEntry] = {}
    winning: dict[str, tuple[datetime.datetime, int]] = {}
    for position, entry in enumerate(entries):
        key = resolution_key(entry, position, now=now)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        latest[entry.hash] = entry
        winning[entry.hash] = key
    return latest


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


def _item_path(item: str) -> str:
    return f"corpus/{item[:4]}/{item}.md"


def _in_cohort(entry: LedgerEntry, *, skipped: list[Skipped]) -> bool:
    """Is this entry deficient work the rewrite fixed — done, web/x, pre-rewrite?

    Membership by vintage alone; which live item the work belongs to is
    :func:`_live_item`'s question.
    """
    if entry.status is not Status.DONE or entry.kind not in _SEEDED_KINDS:
        return False
    try:
        return parse_version(entry.engine) == _PRE_REWRITE
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


def _live_item(
    entry: LedgerEntry,
    *,
    root: Path,
    owners: dict[str, str],
    exclusions: dict[str, str],
    skipped: list[Skipped],
) -> str | None:
    """The live corpus item the rerun's output belongs to, or None to refuse it.

    The stored ``item`` answers first, then the corpus's own claim on the
    work — an item renamed since the line was written still lists this URL
    under its new id, and that is a re-attribution, not a purge.
    """
    if (root / _item_path(entry.item)).exists():
        return entry.item
    owner = owners.get(entry.hash)
    if owner is not None:
        return owner
    item_path = _item_path(entry.item)
    if entry.item in exclusions:
        reason = exclusions[entry.item] or "no reason recorded"
        why = (
            f"{item_path} is gone and the item is excluded on the record "
            f"(state/exclusions.tsv: {reason}) — excluded, never reseeded"
        )
    else:
        why = (
            f"{item_path} does not exist, no state/exclusions.tsv record names the item, "
            f"and no live corpus item lists {entry.url} — nothing claims this work, so "
            "the rerun would have no item to write under; not reseeded. If the item was "
            "renamed and its urls: no longer carries this URL, requeue by hand with "
            "`enrich mark`"
        )
    skipped.append(Skipped(what=f"rerun seed for {entry.item}", why=why))
    return None
