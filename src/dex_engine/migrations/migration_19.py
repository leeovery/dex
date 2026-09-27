"""Migration 19 — cancel the heal reruns still pending over a stored copy that stands.

Migrations 15 to 18 seeded a rerun of every unit whose stored copy an
older engine might have read wrong: github links the old dispatch misread
(15), x posts whose video was never heard (16), x articles landed without
their entities (17), and every page the article seam extracted (18). On a
large instance that queued well over a thousand reruns, drained at the
rerun cap after fresh work, each re-fetching months-old content from the
live web. Some re-reads came back worse than the copy they replaced: code
blocks flattened, notebooks stored as raw JSON, the prose after a code
block lost. The stored copy is the content the owner saved and digested,
so a rerun still pending over one is closed on it instead.

**Members** are live lines that are heal reruns not yet run: ``rerun``
set, ``via`` one of migration-15 to migration-18, and status ``queued``,
``blocked`` or ``error``. A ``waiting`` rerun is left alone, and so is a
blocked retry of one, which still carries its ``needs``: the post was
already re-fetched and written over the stored one, and what remains is
its video's transcript, which only adds to the post now that a landing
deletes nothing. A member is cancelled only where its latest landing's
file stands: at the path the landing recorded, else, for an item renamed
since, under the same file name in the directory of the live item that
owns the unit, either way inside the instance. A rerun with no landing
standing — a github page whose misread output migration 15 deleted
before seeding, a unit that never landed — stays pending, because its
rerun is the only way it gets content.

**The line written** is the landing again, for the live item, at the path
where the file stands now: its kind, format, lineage, http-shared license
and title, ``done``, ``via: migration-19``, no longer a rerun, with ``at``
this apply's instant so it outranks the seed. It departs from ``stamp`` by
keeping the landing's ``date`` and ``engine``. The date is the day the
item's enrichment last changed, which the digest-staleness backstop
compares with the digest's pass, and nothing changed, so the item owes no
digest. The engine is the one that wrote the stored content, which the
record keeps saying.

A migration 15 rerun of an attachment link is closed on a hand heal, since
the old route never read an attachment and only a session could have
landed one: the reading the session wrote stands, and the report counts
them.

Which live item owns a unit follows migration 2's rule, asked of the
corpus's own resolution: the stored ``item`` where its corpus file still
exists, else the live item that claims the unit. A unit nothing live
claims is skipped with why, naming the ``state/exclusions.tsv`` entry
where one exists, and its rerun stays pending.

Idempotent: a cancelled unit's live line is ``done``, which no member is,
and a rerun left pending is judged again the same way.
"""

import datetime
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path, PurePosixPath

from dex_engine.pipeline.ledger import LedgerSchemaError, append, from_line, resolution_key
from dex_engine.pipeline.ownership import unit_owners
from dex_engine.pipeline.registry import default_drivers
from dex_engine.pipeline.types import LedgerEntry, MigrationReport, Skipped, Status
from dex_engine.pipeline.urls import host_of, resolve_repo_path

__all__ = ["HealRerunCancel", "build"]

NUMBER = 19
INTENT = (
    "cancel the heal reruns migrations 15 to 18 queued: each one still queued, blocked or "
    "in error over a stored copy that stands is recorded done at that copy again, dated "
    "and attributed as it landed, and one with no copy standing stays pending"
)

_VIA = "migration-19"
_HEALS = ("migration-15", "migration-16", "migration-17", "migration-18")
_PENDING = frozenset({Status.QUEUED, Status.BLOCKED, Status.ERROR})

# The path segment migration 15 read an attachment link by.
_ATTACHMENTS = "user-attachments"


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — the shared build signature; a cancel keeps its landing's date
    now: Callable[[], datetime.datetime],
    engine_version: str,  # noqa: ARG001 — and its landing's engine
) -> "HealRerunCancel":
    """Build migration 19; written lines carry the injected write instant."""
    return HealRerunCancel(now=now)


class HealRerunCancel:
    """Migration 19: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def __init__(self, *, now: Callable[[], datetime.datetime]) -> None:
        """Written lines carry the injected write instant."""
        self._now = now

    def apply(self, root: Path) -> MigrationReport:
        """Record every pending heal rerun done at the stored copy that stands.

        Args:
            root: The instance root.

        Returns:
            The report: one summary action with the reruns cancelled per
            migration that seeded them and those left pending, a skip for
            every rerun no live item claims, and one for every ledger line
            that does not parse.
        """
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport()
        skipped: list[Skipped] = []
        moment = self._now()
        records = list(_records(path, skipped))
        latest = _latest(records, now=moment)
        landings = _latest(((n, e) for n, e in records if e.path is not None), now=moment)
        pending = [entry for entry in latest.values() if _pending_heal(entry)]
        if not pending:
            return MigrationReport(skipped=skipped)
        # The corpus resolution answers one question — which live item owns
        # a unit whose stored item is gone — and only such a unit asks it.
        owners = (
            unit_owners(root, latest, default_drivers())
            if any(not _item_file(root, entry.item).exists() for entry in pending)
            else {}
        )
        exclusions = _exclusions(root)
        cancelled: list[LedgerEntry] = []
        left = 0
        for seed in pending:
            landing = landings.get(seed.hash)
            if landing is None or landing.path is None:
                left += 1
                continue
            item = _live_item(seed, root=root, owners=owners)
            if item is None:
                skipped.append(_unclaimed(seed, exclusions))
                continue
            standing = _standing(root, landing.path, item)
            if standing is None:
                left += 1
                continue
            append(path, _cancel(landing, item=item, path=standing, at=self._now()))
            cancelled.append(seed)
        if not cancelled and not left:
            return MigrationReport(skipped=skipped)
        return MigrationReport(actions=[_summary(cancelled, left)], skipped=skipped)


def _pending_heal(entry: LedgerEntry) -> bool:
    return entry.rerun and entry.via in _HEALS and entry.status in _PENDING and entry.needs is None


def _standing(root: Path, recorded: str, item: str) -> str | None:
    """Where the landing's file stands: its recorded path, else its name under the live item.

    A rename moves the item's enrichment directory and keeps each file's
    name, so a landing recorded under the old id stands under the new one.
    Both paths are data a ledger line spells, so both are held inside the
    instance root before either is trusted.
    """
    renamed = f"enrichment/{item}/{PurePosixPath(recorded).name}"
    for repo_path in dict.fromkeys((recorded, renamed)):
        file = resolve_repo_path(root, repo_path)
        if file is not None and file.is_file():
            return repo_path
    return None


def _cancel(landing: LedgerEntry, *, item: str, path: str, at: datetime.datetime) -> LedgerEntry:
    """The landing recorded again, by this migration, at the path where its file stands."""
    return LedgerEntry(
        hash=landing.hash,
        url=landing.url,
        item=item,
        kind=landing.kind,
        format=landing.format,
        status=Status.DONE,
        http_shared=landing.http_shared,
        engine=landing.engine,
        date=landing.date,
        at=at,
        job=landing.job,
        via=_VIA,
        parent=landing.parent,
        depth=landing.depth,
        path=path,
        title=landing.title,
    )


def _is_attachment(url: str) -> bool:
    if host_of(url) != "github.com":
        return False
    segments = [segment for segment in urllib.parse.urlsplit(url).path.split("/") if segment]
    return bool(segments) and segments[0].lower() == _ATTACHMENTS


def _summary(cancelled: list[LedgerEntry], left: int) -> str:
    clauses: list[str] = []
    if cancelled:
        seeded = Counter(seed.via for seed in cancelled)
        per = ", ".join(
            f"{seeded[via]} from {via.replace('-', ' ')}" for via in _HEALS if seeded[via]
        )
        clauses.append(
            f"cancelled {len(cancelled)} heal rerun(s) whose stored copy stands ({per}): "
            "each is recorded done at that copy again, dated and attributed as it landed, so "
            "nothing re-fetches it and no digest is owed"
        )
        healed = sum(
            1 for seed in cancelled if seed.via == "migration-15" and _is_attachment(seed.url)
        )
        if healed:
            clauses.append(
                f"{healed} of migration 15's were attachment links a session had healed by "
                "hand; that reading stands"
            )
    if left:
        clauses.append(
            f"left {left} heal rerun(s) pending with no stored copy standing, to drain as before"
        )
    return "; ".join(clauses)


def _records(path: Path, skipped: list[Skipped]) -> Iterator[tuple[int, LedgerEntry]]:
    """Every ledger line that parses, with its position; the rest named in ``skipped``.

    Line by line rather than through ``ledger.load``: one line this
    migration has no business touching must not stop it cancelling the
    reruns it does.
    """
    for position, line in enumerate(path.read_text(encoding="utf-8").split("\n")):
        if not line.strip():
            continue
        try:
            entry = from_line(line)
        except (LedgerSchemaError, ValueError):
            skipped.append(
                Skipped(
                    what=f"ledger line {position + 1}",
                    why="does not parse — left untouched; migration 19 skipped it",
                )
            )
            continue
        yield position, entry


def _latest(
    records: Iterable[tuple[int, LedgerEntry]], *, now: datetime.datetime
) -> dict[str, LedgerEntry]:
    """The latest of ``records`` per hash, resolved as ``ledger.load`` resolves them."""
    latest: dict[str, LedgerEntry] = {}
    winning: dict[str, tuple[datetime.datetime, int]] = {}
    for position, entry in records:
        key = resolution_key(entry, position, now=now)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        latest[entry.hash] = entry
        winning[entry.hash] = key
    return latest


def _item_file(root: Path, item: str) -> Path:
    return root / "corpus" / item[:4] / f"{item}.md"


def _live_item(
    entry: LedgerEntry, *, root: Path, owners: Mapping[str, tuple[str, ...]]
) -> str | None:
    """The live corpus item that owns the unit, or None where nothing live claims it."""
    if _item_file(root, entry.item).exists():
        return entry.item
    for owner in owners.get(entry.hash, ()):
        if _item_file(root, owner).exists():
            return owner
    return None


def _exclusions(root: Path) -> dict[str, str]:
    """``state/exclusions.tsv`` as item id -> stated reason, tolerantly read."""
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


def _unclaimed(seed: LedgerEntry, exclusions: Mapping[str, str]) -> Skipped:
    item_path = f"corpus/{seed.item[:4]}/{seed.item}.md"
    if seed.item in exclusions:
        reason = exclusions[seed.item] or "no reason recorded"
        why = (
            f"{item_path} is gone and the item is excluded on the record "
            f"(state/exclusions.tsv: {reason}) — its rerun was left as it stands"
        )
    else:
        why = (
            f"{item_path} does not exist, no state/exclusions.tsv record names the item, and "
            f"no live corpus item claims {seed.url} — nothing owns the stored copy, so the "
            "rerun was left pending. If the item was renamed and no longer lists the link, "
            f"keep the stored copy by hand with `enrich mark {seed.url} done --path <its path>`"
        )
    return Skipped(what=f"{seed.via} rerun for {seed.item}", why=why)
