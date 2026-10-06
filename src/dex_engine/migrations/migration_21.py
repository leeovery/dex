"""Migration 21 — give the units an earlier attempt cap gave up on a fresh start.

A blocked unit is one whose fetch met a refusal that usually clears: a
403, a 429, a 5xx. Before the backoff, the engine retried it on every run
and gave it up at its fifth failure, recording it ``manual`` with the
reason "still blocked after 5 attempts — " and the last refusal. Scheduled
runs can be hourly, so a rate limit spent all five tries within hours of
the first refusal, each one meeting the source mid-throttle. And
``manual`` is terminal: no run drains it again, so raising the cap does
nothing for a unit already given up. This migration gives each such unit
the lifecycle it would have had: every attempt the cap now allows, with a
wait after each failure.

**Members** are live lines ``manual`` whose reason is that escalation,
counting fewer attempts than the cap now allows, of units that never
landed: no line of the unit records an output, and the live line is not
a rerun. A unit holding stored content keeps it, and nothing re-fetches it.

**The line written** is the unit back where its tries began, for the live
item: ``queued``, or ``waiting`` with ``needs: transcribe`` for a unit the
transcribe drain gave up on, so its retry goes back through that drain and
never the driver. The escalation dropped the ``needs`` that routed it, and
a compact drops the blocked lines that still carried it, so the
escalation's own reason tells the two apart: the transcribe drain opened
every refusal of a unit's audio with "audio acquisition failed: ". Its
kind, format, job, lineage, ``via`` and http-shared license are kept, as
the owner's own requeue keeps them: ``via`` is the provenance of a child
or a rerun, never a migration's mark, and the migrations log records that
this one ran. Attempts, reason, path and title are cleared; it is not a
rerun, since nothing landed. It is dated, stamped and attributed to this
apply, as every line the engine writes is.

A given-up line a faster clock stamped ahead of this apply's instant,
inside the ledger's future-skew allowance, outranks the line written for
it, so the apply reads the ledger back and names each unit whose re-queue
did not take as skipped, with its route, rather than counting it. One
stamped past the allowance loses to the re-queue until the clock reaches
its stamp, the ledger's own exposure to a fast clock.

Every member is tried again on the next run that can take it, so an
instance holding many sends a burst to the sources behind them. A source
still refusing parks its unit blocked again, and the backoff spaces its
tries from there.

Which live item owns a unit: the stored ``item`` where its corpus file
still exists, else the live item that claims the unit. A unit nothing live
claims is skipped with why, naming the ``state/exclusions.tsv`` entry where
one exists, and stays manual.

Idempotent: a re-queued unit's live line is ``queued`` or ``waiting``,
which no member is, and a unit given up from now on spent every attempt
the cap allows.
"""

import datetime
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from dex_engine.pipeline.ledger import append, from_line, resolution_key, stamp
from dex_engine.pipeline.ownership import unit_owners
from dex_engine.pipeline.registry import default_drivers
from dex_engine.pipeline.types import LedgerEntry, MigrationReport, Need, Skipped, Status

__all__ = ["GivenUpBlockedRequeue", "build"]

NUMBER = 21
INTENT = (
    "re-queue every unit an earlier engine gave up on as still blocked, short of the "
    "attempts the cap now allows, that never landed: queued again, or waiting again for "
    "its transcription, it takes the blocked lifecycle afresh with a wait after each failure"
)

# What every engine before the backoff wrote when it gave a blocked unit
# up: the attempts it took, then the refusal its last one met, which the
# transcribe drain opened with the second string when the refusal was of
# the unit's audio. Frozen: what matters is what those engines wrote.
_GIVEN_UP_RE = re.compile(r"^still blocked after (\d+) attempts — ")
_AUDIO_REFUSED = "audio acquisition failed: "

# The cap this migration's engine shipped. Frozen with the strings: a
# later cap gives up on its own units, and whether those deserve another
# start is a later migration's question, not a re-apply of this one.
_CAP = 8


@dataclass(frozen=True, slots=True, kw_only=True)
class _Member:
    """A unit an earlier cap gave up on, read once for all that is asked of it."""

    entry: LedgerEntry
    # the transcribe drain gave it up: the last refusal it met was of its audio
    transcribe: bool
    # its stored item's corpus file still exists
    stored: bool


def build(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> "GivenUpBlockedRequeue":
    """Build migration 21; written lines are stamped with the injected clocks and engine."""
    return GivenUpBlockedRequeue(today=today, now=now, engine_version=engine_version)


class GivenUpBlockedRequeue:
    """Migration 21: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def __init__(
        self,
        *,
        today: Callable[[], datetime.date],
        now: Callable[[], datetime.datetime],
        engine_version: str,
    ) -> None:
        """Written lines are stamped with the injected clocks and running engine."""
        self._today = today
        self._now = now
        self._engine_version = engine_version

    def apply(self, root: Path) -> MigrationReport:
        """Re-queue every unit an earlier cap gave up on that never landed.

        Args:
            root: The instance root.

        Returns:
            The report: one summary action counting the units re-queued,
            a skip for every member no live item claims or whose re-queue
            did not take, and one for every ledger line that does not parse.
        """
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport()
        skipped: list[Skipped] = []
        records = list(_records(path, skipped))
        moment = self._now()
        latest = _latest(records, now=moment)
        landed = {entry.hash for _, entry in records if entry.path is not None}
        candidates = (_member(entry, root) for entry in latest.values() if entry.hash not in landed)
        members = [member for member in candidates if member is not None]
        if not members:
            return MigrationReport(skipped=skipped)
        # The corpus resolution answers one question — which live item owns
        # a unit whose stored item is gone — and only such a unit asks it.
        owners = (
            unit_owners(root, latest, default_drivers())
            if not all(member.stored for member in members)
            else {}
        )
        exclusions = _exclusions(root)
        written: list[tuple[_Member, LedgerEntry]] = []
        for member in members:
            item = _live_item(member, root=root, owners=owners)
            if item is None:
                skipped.append(_unclaimed(member.entry, exclusions))
                continue
            line = self._stamped(_fresh(member, item), moment)
            append(path, line)
            written.append((member, line))
        requeued = _taken(path, written, now=moment, skipped=skipped)
        if not requeued:
            return MigrationReport(skipped=skipped)
        return MigrationReport(actions=[_summary(requeued)], skipped=skipped)

    def _stamped(self, entry: LedgerEntry, moment: datetime.datetime) -> LedgerEntry:
        return stamp(
            entry, today=self._today, now=lambda: moment, engine_version=self._engine_version
        )


def _member(entry: LedgerEntry, root: Path) -> _Member | None:
    """The unit as a member where its live line is an earlier cap's escalation of it, else None."""
    if entry.status is not Status.MANUAL or entry.rerun:
        return None
    reason = entry.reason or ""
    match = _GIVEN_UP_RE.match(reason)
    if match is None or int(match.group(1)) >= _CAP:
        return None
    return _Member(
        entry=entry,
        transcribe=reason[match.end() :].startswith(_AUDIO_REFUSED),
        stored=_item_file(root, entry.item).exists(),
    )


def _taken(
    path: Path,
    written: list[tuple[_Member, LedgerEntry]],
    *,
    now: datetime.datetime,
    skipped: list[Skipped],
) -> list[_Member]:
    """The members whose line is live, read back at ``now``; the rest named in ``skipped``."""
    live = _latest(_records(path, []), now=now)  # its unreadable lines are named already
    taken: list[_Member] = []
    for member, line in written:
        winner = live[line.hash]
        if winner == line:
            taken.append(member)
        else:
            # Only a line stamped later than ``now`` outranks one appended last at ``now``.
            skipped.append(_outranked(member.entry, line.item, winner.at or now))
    return taken


def _fresh(member: _Member, item: str) -> LedgerEntry:
    """The unit back where its tries began, for the live item."""
    entry = member.entry
    return LedgerEntry(
        hash=entry.hash,
        url=entry.url,
        item=item,
        kind=entry.kind,
        format=entry.format,
        status=Status.WAITING if member.transcribe else Status.QUEUED,
        needs=Need.TRANSCRIBE if member.transcribe else None,
        http_shared=entry.http_shared,
        engine="seed",  # stamped in apply
        date=datetime.date.min,
        job=entry.job,
        via=entry.via,
        # A harvested link or a media download carries lineage of its own —
        # parent and depth travel together, so both ride the line.
        parent=entry.parent,
        depth=entry.depth,
    )


def _label(member: _Member) -> str:
    """What the report counts a unit as: its job, else its kind, a transcription apart."""
    entry = member.entry
    if entry.job is not None:
        return entry.job.value
    return f"{entry.kind.value} transcription" if member.transcribe else entry.kind.value


def _summary(requeued: list[_Member]) -> str:
    counts = Counter(_label(member) for member in requeued)
    per = ", ".join(f"{counts[label]} {label}" for label in sorted(counts))
    return (
        f"re-queued {len(requeued)} unit(s) an earlier engine gave up on as still blocked, "
        f"none of which ever landed ({per}): the next run that can take each tries it again, "
        "and the backoff spaces out the attempts of any refused again"
    )


def _records(path: Path, skipped: list[Skipped]) -> Iterator[tuple[int, LedgerEntry]]:
    """Every ledger line that parses, with its position; the rest named in ``skipped``.

    Line by line rather than through ``ledger.load``: one line this
    migration has no business touching must not stop it re-queueing the
    units it does.
    """
    for position, line in enumerate(path.read_text(encoding="utf-8").split("\n")):
        if not line.strip():
            continue
        try:
            entry = from_line(line)
        except ValueError:  # LedgerSchemaError is one
            skipped.append(
                Skipped(
                    what=f"ledger line {position + 1}",
                    why="does not parse — left untouched; migration 21 skipped it",
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


def _live_item(member: _Member, *, root: Path, owners: Mapping[str, tuple[str, ...]]) -> str | None:
    """The live corpus item that owns the unit, or None where nothing live claims it."""
    if member.stored:
        return member.entry.item
    for owner in owners.get(member.entry.hash, ()):
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


def _unclaimed(entry: LedgerEntry, exclusions: Mapping[str, str]) -> Skipped:
    item_path = f"corpus/{entry.item[:4]}/{entry.item}.md"
    if entry.item in exclusions:
        reason = exclusions[entry.item] or "no reason recorded"
        why = (
            f"{item_path} is gone and the item is excluded on the record "
            f"(state/exclusions.tsv: {reason}) — the unit stays manual as it stands"
        )
    else:
        why = (
            f"{item_path} does not exist, no state/exclusions.tsv record names the item, and "
            f"no live corpus item claims {entry.url} — nothing owns the unit, so it stays "
            "manual as it stands"
        )
    return Skipped(what=f"given-up unit {entry.url} of {entry.item}", why=why)


def _outranked(given_up: LedgerEntry, item: str, stamped: datetime.datetime) -> Skipped:
    return Skipped(
        what=f"given-up unit {given_up.url} of {item}",
        why=(
            "the line that gave it up was stamped by a clock running ahead of this machine's "
            "and outranks the re-queue, so the unit stays manual; once this machine's clock "
            f"passes {stamped.astimezone(datetime.UTC):%Y-%m-%d %H:%M:%S} UTC, "
            f"`bin/dex enrich fetch {item} {given_up.url}` requeues it in place and tries it again"
        ),
    )
