"""Migration 13 — repair the media files the unchecked download path kept.

Before v0.1.6 a media download was named from its URL and never read: an
empty 200, a site's HTML shell served under an image URL, an API error
body, and AVIF bytes under a ``.png`` URL all landed as ``done`` media
units pointing at ``enrichment/<item>/media-<n>.<ext>``. The drain now
parks an empty or document body blocked and names the file from its
bytes — but a done unit is never re-fetched, so every instance still
carries the legacy files: the describe queue charges each for a
description, and sessions have closed rows by describing a file as
"zero bytes".

Membership: every live ``job: media`` line, ``done``, whose ``path``
names a regular file under the instance root. The leading bytes decide:

- **Empty, or a document** (``sniff_document``: HTML, XML, JSON) — the
  unit requeues as ``{queued, rerun, via: migration-13}``, hash/url/
  kind/job/parent/depth preserved and ``item`` the live owner, and the
  file is deleted. Seeded BEFORE the delete: an apply interrupted
  between the two leaves a queued line over a file the drain overwrites
  or sweeps, never a done line over nothing. The re-fetch runs under
  the fixed rules, so a URL that still answers with no media parks
  blocked instead of landing again.
- **Bytes naming a format other than the file's suffix**
  (``sniff_media_ext``, signatures only; a suffix is read case-blind and
  through the one alias the field spells, ``jpeg`` for ``jpg``) — the
  file is renamed in place to ``media-<n>.<sniffed>`` and a done line
  identical to the live one but for ``path``, ``item`` (the live owner)
  and ``via: migration-13`` is appended. Renamed BEFORE the append: an
  apply interrupted between the two leaves a done line naming a file
  that no longer exists, which the health check's missing-output finding
  reports and ``enrich mark <url> done --path <new>`` heals; the
  re-apply itself finds no member there, because the live line's
  ``path`` names a file that is gone, and a line over no file is never a
  member.
- Anything else — bytes carrying no signature this engine knows, or a
  suffix that already names the format — is left alone.

``media-<n>.md``, the session's description of the slot, is never
touched. Where one stands beside a repaired file the unit's report line
says so: a requeued unit's description was written against the
discarded bytes, and because the describe count still reads as met,
nothing else will ever name it.

Which live item a line writes under follows migration 9's rule: the
stored ``item`` answers where its corpus file still exists; a renamed
item is found through the corpus's own claim on the PARENT unit's URL
(a media URL is never listed in ``urls:``; the post is); an item nothing
claims is skipped-with-why, naming the ``state/exclusions.tsv`` entry
where one exists. Only a file that needs repair asks — a healthy file
of a purged item is no finding of this migration's.

Idempotent: a requeued unit's live line is no longer ``done``, and a
renamed file's suffix matches its bytes, so a second apply finds no
members.
"""

import datetime
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from dex_engine.pipeline.detect import sniff_document, sniff_media_ext
from dex_engine.pipeline.ledger import (
    LedgerSchemaError,
    append,
    from_line,
    resolution_key,
    stamp,
)
from dex_engine.pipeline.ownership import corpus_owners
from dex_engine.pipeline.registry import default_drivers
from dex_engine.pipeline.types import (
    Job,
    LedgerEntry,
    MigrationReport,
    Skipped,
    Status,
)
from dex_engine.pipeline.urls import resolve_repo_path

__all__ = ["MediaBytesRepair", "build"]

NUMBER = 13
INTENT = (
    "repair the media files the unchecked download path kept: every done media unit whose "
    "file is empty or a page requeues as {queued, rerun, via: migration-13} and loses the "
    "file; every file whose bytes name another format is renamed to it and its done line "
    "re-pointed"
)

_VIA = "migration-13"

# More than either sniffer reads (512 bytes), so no media file is read whole.
_LEAD_BYTES = 4096

# The sniffer names a format one way (`jpg`) and a URL-named legacy file
# may spell it another; a spelling is not a wrong format, and a rename
# costs the item a digest re-emit. jpeg is the one alias the field
# carries — heic and heif are distinct brands, never aliases.
_SUFFIX_ALIASES = {"jpeg": "jpg"}


def build(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> "MediaBytesRepair":
    """Build migration 13; written lines are stamped with the injected clocks and engine."""
    return MediaBytesRepair(today=today, now=now, engine_version=engine_version)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Member:
    """A done media unit, the file its line names, and that file's leading bytes."""

    entry: LedgerEntry
    repo_path: str
    file: Path
    lead: bytes


@dataclass(frozen=True, slots=True, kw_only=True)
class _Requeue:
    """The file holds no media — ``held`` says what it holds instead."""

    held: str


@dataclass(frozen=True, slots=True, kw_only=True)
class _Rename:
    """The bytes name a format the file's suffix does not."""

    ext: str


class MediaBytesRepair:
    """Migration 13: see the module docstring."""

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
        """Requeue every done media unit holding no media; rename every misnamed file.

        Args:
            root: The instance root.

        Returns:
            The report: a summary action with both counts, one action per
            repaired unit stating what its file held and what the repair
            leaves for the session, and a skip for every repair no live
            item claims.
        """
        skipped: list[Skipped] = []
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport()
        latest = _latest_per_hash(path, skipped, now=self._now())
        repairs = [
            (member, verdict)
            for entry in latest.values()
            if (member := _member(root, entry)) is not None
            and (verdict := _verdict(member)) is not None
        ]
        if not repairs:
            return MigrationReport(skipped=skipped)
        # The corpus scan answers one question — which live item claims a
        # renamed member's work — and only a member whose stored item file
        # is gone needs to ask it.
        owners = (
            corpus_owners(root, default_drivers())
            if any(not (root / _item_path(member.entry.item)).exists() for member, _ in repairs)
            else {}
        )
        exclusions = _exclusions(root)
        actions: list[str] = []
        requeued = renamed = 0
        for member, verdict in repairs:
            item = _live_item(member.entry, root=root, owners=owners)
            if item is None:
                skipped.append(_unclaimed(member, exclusions))
                continue
            match verdict:
                case _Requeue(held=held):
                    actions.append(self._requeue(path, member, item, held))
                    requeued += 1
                case _Rename(ext=ext):
                    actions.append(self._rename(path, member, item, ext))
                    renamed += 1
        if not actions:
            return MigrationReport(skipped=skipped)
        summary = (
            f"requeued {requeued} media unit(s) whose file held no media and renamed {renamed} "
            "whose bytes named another format — the pre-0.1.6 download path kept whatever a "
            "media URL answered with, unread"
        )
        return MigrationReport(actions=[summary, *actions], skipped=skipped)

    def _requeue(self, path: Path, member: _Member, item: str, held: str) -> str:
        """Seed the unit's requeue, then drop the file — never the other way round."""
        append(path, self._stamped(_requeue_seed(member.entry, item)))
        member.file.unlink()
        return _requeue_action(member, item, held)

    def _rename(self, path: Path, member: _Member, item: str, ext: str) -> str:
        """Rename the file to what its bytes are, then re-point the done line."""
        member.file.rename(member.file.with_suffix(f".{ext}"))
        repointed = replace(
            member.entry, item=item, path=_repointed(member.repo_path, ext), via=_VIA
        )
        append(path, self._stamped(repointed))
        return _rename_action(member, item, ext)

    def _stamped(self, entry: LedgerEntry) -> LedgerEntry:
        return stamp(entry, today=self._today, now=self._now, engine_version=self._engine_version)


def _member(root: Path, entry: LedgerEntry) -> _Member | None:
    """The done media unit with the file its line names, or None where it is no member."""
    if entry.job is not Job.MEDIA or entry.status is not Status.DONE or entry.path is None:
        return None
    if resolve_repo_path(root, entry.path) is None:
        return None
    file = root / entry.path
    if not file.is_file():
        return None
    with file.open("rb") as handle:
        lead = handle.read(_LEAD_BYTES)
    return _Member(entry=entry, repo_path=entry.path, file=file, lead=lead)


def _verdict(member: _Member) -> _Requeue | _Rename | None:
    """What the bytes call for: a re-fetch, a rename, or nothing at all."""
    if not member.lead:
        return _Requeue(held="no bytes at all")
    document = sniff_document(member.lead)
    if document is not None:
        return _Requeue(held=f"{document}, not media bytes")
    ext = sniff_media_ext(member.lead, signatures_only=True)
    suffix = member.file.suffix.removeprefix(".").lower()
    if ext is None or _SUFFIX_ALIASES.get(suffix, suffix) == ext:
        return None
    return _Rename(ext=ext)


def _requeue_seed(entry: LedgerEntry, item: str) -> LedgerEntry:
    """The unit's requeue line: its own identity and lineage, queued again."""
    return LedgerEntry(
        hash=entry.hash,
        url=entry.url,
        item=item,
        kind=entry.kind,
        status=Status.QUEUED,
        engine="seed",  # stamped in apply
        date=datetime.date.min,
        job=Job.MEDIA,
        parent=entry.parent,
        depth=entry.depth,
        via=_VIA,
        rerun=True,
    )


def _repointed(repo_path: str, ext: str) -> str:
    """The ledger spelling of the renamed file."""
    return str(Path(repo_path).with_suffix(f".{ext}"))


def _requeue_action(member: _Member, item: str, held: str) -> str:
    """What the re-fetch does, and what it leaves the session where a description stands."""
    text = (
        f"{item}: {member.repo_path} held {held} (written by engine {member.entry.engine}); "
        "requeued as {queued, rerun, via: migration-13} and the file deleted — the drain "
        "re-fetches it under the fixed rules, and parks it blocked where the URL still "
        "answers with no media"
    )
    description = _description(member)
    if description is None:
        return text
    return (
        f"{text}; {description} describes the discarded bytes, and the describe count still "
        "reads as met so nothing else will ever name it — rewrite or remove it once the "
        "re-fetch lands or parks"
    )


def _rename_action(member: _Member, item: str, ext: str) -> str:
    """What the rename changed, and what still names the old filename."""
    text = (
        f"{item}: {member.repo_path} holds {ext.upper()} bytes; renamed to "
        f"{_repointed(member.repo_path, ext)} and the done line re-pointed — the item's "
        "digest, where one stands, lists the old path, so the health check names it stale "
        "and its media drifted until `enrich item digest` re-emits it"
    )
    description = _description(member)
    if description is None:
        return text
    return f"{text}; {description} may still name the old filename on its first line"


def _description(member: _Member) -> str | None:
    """The slot's ``media-<n>.md``, repo-relative, where a session has written one."""
    if not member.file.with_suffix(".md").is_file():
        return None
    return str(Path(member.repo_path).with_suffix(".md"))


def _latest_per_hash(
    path: Path, skipped: list[Skipped], *, now: datetime.datetime
) -> dict[str, LedgerEntry]:
    """Every hash's live line, read tolerantly, resolved as ``load`` resolves.

    Line by line rather than through ``ledger.load``: one line this
    migration has no business touching must not stop it healing the ones
    it does.
    """
    latest: dict[str, LedgerEntry] = {}
    winning: dict[str, tuple[datetime.datetime, int]] = {}
    for position, line in enumerate(path.read_text(encoding="utf-8").split("\n")):
        if not line.strip():
            continue
        try:
            entry = from_line(line)
        except (LedgerSchemaError, ValueError):
            skipped.append(
                Skipped(
                    what=f"ledger line {position + 1}",
                    why="does not parse — left untouched; the media repair skipped it",
                )
            )
            continue
        key = resolution_key(entry, position, now=now)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        latest[entry.hash] = entry
        winning[entry.hash] = key
    return latest


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


def _item_path(item: str) -> str:
    return f"corpus/{item[:4]}/{item}.md"


def _live_item(entry: LedgerEntry, *, root: Path, owners: dict[str, str]) -> str | None:
    """The live corpus item the line writes under, or None to refuse it.

    The stored ``item`` answers first. A renamed item is found through the
    corpus's claim on the PARENT unit's URL — the post is what ``urls:``
    lists; a signed media URL never is, so the unit's own hash finds
    nothing there by design.
    """
    if (root / _item_path(entry.item)).exists():
        return entry.item
    return owners.get(entry.parent) if entry.parent is not None else None


def _unclaimed(member: _Member, exclusions: dict[str, str]) -> Skipped:
    entry = member.entry
    item_path = _item_path(entry.item)
    if entry.item in exclusions:
        reason = exclusions[entry.item] or "no reason recorded"
        why = (
            f"{item_path} is gone and the item is excluded on the record "
            f"(state/exclusions.tsv: {reason}) — excluded, never repaired"
        )
    else:
        why = (
            f"{item_path} does not exist, no state/exclusions.tsv record names the item, "
            f"and no live corpus item lists the parent of {entry.url} — nothing claims this "
            "work, so the repair would have no item to write under; not repaired. If the "
            "item was renamed and its urls: no longer carries the post, heal by hand with "
            "`enrich mark`"
        )
    return Skipped(what=f"media repair for {member.repo_path}", why=why)
