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
every refusal of a unit's audio with "audio acquisition failed: ". The
drain reads the park the fetch wrote, ``<kind>-<hash6>.md`` under the item
that owns the unit, so a transcribe job whose park does not stand under
its live item, as when that item is no longer the one it was parked for,
is ``queued`` instead, and the driver writes a fresh one. The unit's kind,
format, job, lineage, ``via`` and http-shared license are kept, as the
owner's own requeue keeps them: ``via`` is the provenance of a child or a
rerun, never a migration's mark, and the migrations log records that this
one ran. Attempts, reason, path and title are cleared; it is not a rerun,
since nothing landed. It is dated and attributed to this apply.

Its stamp places it in time. Sync runs before the pull, so a second
machine applies this to a ledger that has not seen the first machine's
runs, and a re-queue stamped at its own apply's instant would outrank the
landing or the refusal those runs recorded since: a landed page would be
fetched again. So a give-up stamped at or before this apply's instant is
followed by a microsecond, and the re-queue takes effect as of the
give-up, outranking that line and nothing written since. A give-up written
before ``at`` existed leaves every line of its unit unstamped, since any
stamped line would outrank it, so any stamp beats them all, and the
re-queue takes the earliest honest one: midnight UTC of the give-up's own
date, or this apply's instant should that date lie ahead of it. A give-up
stamped later than this apply's instant, inside the ledger's future-skew
allowance, came from a clock running ahead of this machine's: no line is
written for it, since one placed after it would outrank this machine's
next writes and one stamped now would lose to it. The unit stays manual
and is named skipped, with the instant, minutes away at most, from which
the owner's ``enrich fetch`` gives it a fresh start. Past the allowance,
the ledger already reads the give-up as unstamped, so the re-queue is
stamped at this apply's instant and wins now, until the clock reaches the
give-up's stamp: the ledger's own exposure to a fast clock.

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

from dex_engine.pipeline.ledger import (
    FUTURE_SKEW_ALLOWANCE,
    append,
    from_line,
    resolution_key,
    stamp,
)
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

# The least a write instant can move on, to place a line just after another.
_TICK = datetime.timedelta(microseconds=1)

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
            a skip for every member no live item claims or whose give-up a
            faster clock stamped, and one for every ledger line that does not
            parse.
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
        requeued: list[LedgerEntry] = []
        for member in members:
            item = _live_item(member, root=root, owners=owners)
            if item is None:
                skipped.append(_unclaimed(member.entry, exclusions))
                continue
            ahead = _ahead(member.entry, moment)
            if ahead is not None:
                skipped.append(_outranked(member.entry, item, ahead))
                continue
            fresh = _fresh(member.entry, item, transcribe=_resumable(member, item, root))
            line = self._stamped(fresh, _placed(member.entry, moment))
            append(path, line)
            requeued.append(line)
        if not requeued:
            return MigrationReport(skipped=skipped)
        return MigrationReport(actions=[_summary(requeued)], skipped=skipped)

    def _stamped(self, entry: LedgerEntry, at: datetime.datetime) -> LedgerEntry:
        return stamp(entry, today=self._today, now=lambda: at, engine_version=self._engine_version)


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


def _ahead(given_up: LedgerEntry, moment: datetime.datetime) -> datetime.datetime | None:
    """The give-up's stamp where a clock ahead of ``moment`` wrote it inside the allowance."""
    at = given_up.at
    if at is not None and moment < at <= moment + FUTURE_SKEW_ALLOWANCE:
        return at
    return None


def _placed(given_up: LedgerEntry, moment: datetime.datetime) -> datetime.datetime:
    """The re-queue's stamp, for a give-up :func:`_ahead` does not hold back."""
    at = given_up.at
    if at is None:
        midnight = datetime.datetime.combine(given_up.date, datetime.time(), tzinfo=datetime.UTC)
        return min(midnight, moment)
    if at <= moment:
        return at.astimezone(datetime.UTC) + _TICK
    return moment  # past the allowance, where the ledger reads the give-up as unstamped


def _resumable(member: _Member, item: str, root: Path) -> bool:
    """Whether the transcribe drain can take the job back: its park stands under the live item."""
    entry = member.entry
    park = root / "enrichment" / item / f"{entry.kind.value}-{entry.hash[:6]}.md"
    return member.transcribe and park.is_file()


def _fresh(entry: LedgerEntry, item: str, *, transcribe: bool) -> LedgerEntry:
    """The unit back where its tries began, for the live item."""
    return LedgerEntry(
        hash=entry.hash,
        url=entry.url,
        item=item,
        kind=entry.kind,
        format=entry.format,
        status=Status.WAITING if transcribe else Status.QUEUED,
        needs=Need.TRANSCRIBE if transcribe else None,
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


def _label(line: LedgerEntry) -> str:
    """What the report counts a re-queue as: its job, else its kind, a transcription apart."""
    if line.job is not None:
        return line.job.value
    return f"{line.kind.value} transcription" if line.needs is Need.TRANSCRIBE else line.kind.value


def _summary(requeued: list[LedgerEntry]) -> str:
    counts = Counter(_label(line) for line in requeued)
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
    # Up to the next whole second, so the message names a readable time
    # that is never before the stamp.
    utc = stamped.astimezone(datetime.UTC)
    passes = utc.replace(microsecond=0)
    if passes < utc:
        passes += datetime.timedelta(seconds=1)
    return Skipped(
        what=f"given-up unit {given_up.url} of {item}",
        why=(
            "the line that gave it up was stamped by a clock running ahead of this machine's, "
            "so no re-queue was written and the unit stays manual; once this machine's clock "
            f"passes {passes:%Y-%m-%d %H:%M:%S} UTC, "
            f"`bin/dex enrich fetch {item} {given_up.url}` requeues it in place and tries it again"
        ),
    )
