"""Migration 20 — give back from git history what engine 0.2.2 degraded.

Engine 0.2.2 did two kinds of damage its own ledger names, and the
instance's git history holds what stood before each. This migration reads
that history back through read-only git and puts it where it was, byte
for byte. It fetches nothing and guesses nothing.

**Videos a transcript retired.** A transcript landing on an x post retired
the media unit that had downloaded the post's video: it deleted the file
and every description whose first line named it, and closed the unit
``skipped`` with the reason "superseded — the transcript of its post
stands for this video". A transcript cannot stand for a video: a silent
screen recording was heard as hallucinated text, and the file with its
description was the only record of what the video showed.

Each such unit gets back the file its latest landing recorded, read as
migration 7 reads a file: from the newest revision holding it (HEAD,
while its deletion is uncommitted, else the parent of the commit that
deleted it), through the working tree's filters, so an LFS video comes
back as the video, or as its committed pointer where no store this
clone reaches holds it. A compact drops a closed unit's landing line, so
a landing the ledger no longer holds is found in the ledger's own
committed history, in the commits one pickaxe query finds mentioning the
unit. Every recorded path is read where the file stands now: by its name
under the directory of the live item that owns the unit, since a rename
moves the directory and a path recorded before it names the dead id.

The file goes back into the slot its landing recorded only where no
other unit's file or recorded path holds that slot: a compact freed the
slot with the landing line, and a download may have taken it since,
under any extension. One slot with two owners loses a file to the next
re-download's slot sweep, so the file then takes the lowest free slot
and the line records that path. A file already standing where the
restore would write counts as this unit's own only when it is the very
bytes history holds and no other unit's path names it.

The descriptions put back are exactly those 0.2.2 deleted, found by the
test it applied, frozen here: a ``media-*.md`` in the file's directory at
that revision whose first line opens ``Describes `<name>` `` on the
file's bare name or its repo path. A description naming the file only
below its first line was never deleted, and one gone now was removed by
something else, perhaps a session on purpose, so it stays gone. Each
comes back byte for byte where nothing stands at its name; one standing
there with other bytes is left and reported. A file restored into
another slot takes the description named for its old slot with it, to
the new slot's ``media-<n>.md``, and every description of it has only the
name in its first line re-spelled, as migration 14 re-spells one.

The unit is then recorded done at its path, with its landing's date and
engine and ``via: migration-20``: the file is the very bytes that landed,
so the item's enrichment is as it was then. Its digest may not be: one
written since was drawn while the file was gone, and the landing's date
keeps the digest-staleness backstop from seeing that, so where the item
has a digest the report asks for it to be written again. Where history
cannot answer — no repository, a shallow clone, a file never committed —
nothing is restored, and the report names the file, why, and the
session's route.

**Pages a 0.2.2 re-read degraded.** Migration 18 queued a rerun of every
page the article seam extracted, and some re-reads came back worse than
the copy they replaced: code blocks flattened, notebooks stored as raw
JSON, the prose after a code block lost. The members are the web and
paper page units whose live line is a ``done`` landing by engine 0.2.2
with ``via: migration-18``. What stood before the re-read is in the
commit that synced 0.2.2, the first one whose ``state/migrations.jsonl``
records migration 18, found by one pickaxe query. A page is read there at
the path where its file stands now, else under the same file name in the
directory its landing at that commit recorded, since an item renamed
after the sync held it under its old id there.

A count decides, never a reading: fence lines (a line opening ```` ``` ````
or ``~~~`` after optional indent) and table rows (a line opening ``|``
after optional indent). Where the current copy has fewer of either, the
copy at the sync commit is restored whole, and the unit is recorded done
at its path with that landing's engine and title and ``via:
migration-20``, dated today, the day its enrichment changed back. Where
the item has a digest, the report asks for it to be written again from
the restored copy: it may have been drawn from the re-read's, and the
backstop compares days, so a digest written the same day as the restore
would never list. A page a session already restored by hand holds that
copy, so its counts match and it stands; so does every re-read that lost
no fence and no row.

Idempotent: a restored video's unit is ``done``, and a restored page's
live line is no longer a migration-18 landing by 0.2.2. Every write in a
restore comes before the line that closes it, so an interrupted apply
finds the unit again with its files already standing, as its own.
"""

import datetime
import itertools
import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from dex_engine import atomic
from dex_engine.gitread import checkout_bytes, git_output
from dex_engine.pipeline.ledger import LedgerSchemaError, append, from_line, resolution_key
from dex_engine.pipeline.ownership import unit_owners
from dex_engine.pipeline.registry import default_drivers
from dex_engine.pipeline.run import is_media_file
from dex_engine.pipeline.types import Job, Kind, LedgerEntry, MigrationReport, Skipped, Status
from dex_engine.pipeline.urls import resolve_repo_path

__all__ = ["RestoreWhatTheReleaseDegraded", "build"]

NUMBER = 20
INTENT = (
    "give back from git history what engine 0.2.2 degraded: every video a landed transcript "
    "deleted comes back with the descriptions deleted with it, its media unit done again, "
    "and every page whose 0.2.2 re-read lost code fences or table rows gets the copy the "
    "0.2.2 sync commit held, to be digested again"
)

_VIA = "migration-20"
_LEDGER = "state/enrichment-ledger.jsonl"
_MIGRATIONS_LOG = "state/migrations.jsonl"

# The reason 0.2.2 closed a media unit with once its post's transcript landed.
_RETIRED = "superseded — the transcript of its post stands for this video"

# 0.2.2's test for a description of a file it deleted: the name its first
# line opens on, read as its describe verb reads one. Frozen: what matters
# is what that engine deleted, whatever the verb reads later.
_DESCRIBED_RE = re.compile(r"^Describes\s+`([^`]+)`")
_BACKTICKED_RE = re.compile(r"`[^`]+`")

# The engine whose migration-18 re-reads replaced stored pages, and the
# record its sync left in the migrations log, spelled as the log writes it.
_REREAD_ENGINE = "0.2.2"
_REREAD_VIA = "migration-18"
_REREAD_LOGGED = '"number": 18,'
_REREAD_NUMBER = 18
_PAGES = frozenset({Kind.WEB, Kind.PAPER})

_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)", re.MULTILINE)
_ROW_RE = re.compile(r"^[ \t]*\|", re.MULTILINE)

# A unit closed without content holds nothing, whatever an earlier line of
# it recorded, so it claims no slot.
_CLOSED = frozenset({Status.DEAD, Status.SKIPPED})

_SLOT_RE = re.compile(r"^media-(\d+)\.")
_LFS_POINTER = b"version https://git-lfs"


def build(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,  # noqa: ARG001 — the shared build signature; every line keeps the engine of its content
) -> "RestoreWhatTheReleaseDegraded":
    """Build migration 20; written lines carry the injected clocks."""
    return RestoreWhatTheReleaseDegraded(today=today, now=now)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Ledger:
    """The ledger as this migration reads it: live lines, and each unit's latest landing."""

    path: Path
    now: datetime.datetime
    latest: dict[str, LedgerEntry]
    landings: dict[str, LedgerEntry]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Source:
    """A retired file as the newest revision holding it has it, and the line that landed it."""

    history_path: str
    revision: str
    content: bytes
    landing: LedgerEntry


@dataclass(frozen=True, slots=True, kw_only=True)
class _Beside:
    """The descriptions standing beside a restored file, and those another file kept out."""

    standing: list[str] = field(default_factory=list)
    kept_out: list[str] = field(default_factory=list)


class RestoreWhatTheReleaseDegraded:
    """Migration 20: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def __init__(
        self,
        *,
        today: Callable[[], datetime.date],
        now: Callable[[], datetime.datetime],
    ) -> None:
        """Written lines carry the injected clocks."""
        self._today = today
        self._now = now

    def apply(self, root: Path) -> MigrationReport:
        """Restore every retired video and every page a 0.2.2 re-read degraded.

        Args:
            root: The instance root.

        Returns:
            The report: one action per restored video and page; a skip for
            every one history could not give back, and for every ledger
            line that does not parse.
        """
        path = root / _LEDGER
        if not path.exists():
            return MigrationReport()
        skipped: list[Skipped] = []
        records = list(_records(path.read_text(encoding="utf-8"), skipped))
        now = self._now()
        ledger = _Ledger(
            path=path,
            now=now,
            latest=_latest(records, now=now),
            landings=_latest(((n, e) for n, e in records if e.path is not None), now=now),
        )
        places = _Places(root, ledger)
        history = _History(root)
        actions = [
            *self._restore_videos(ledger, places, history, skipped),
            *self._restore_pages(ledger, places, history, skipped),
        ]
        return MigrationReport(actions=actions, skipped=skipped)

    def _restore_videos(
        self, ledger: _Ledger, places: "_Places", history: "_History", skipped: list[Skipped]
    ) -> list[str]:
        """Give every retired media unit its file and descriptions back, and close it done."""
        retired = [
            entry
            for entry in ledger.latest.values()
            if entry.job is Job.MEDIA
            and entry.status is Status.SKIPPED
            and entry.reason == _RETIRED
        ]
        if not retired:
            return []
        landings = {e.hash: ledger.landings[e.hash] for e in retired if e.hash in ledger.landings}
        compacted = {entry.hash for entry in retired} - landings.keys()
        landings |= history.landings(compacted, now=ledger.now)
        places.expect(landings.values())
        actions: list[str] = []
        for unit in retired:
            outcome = self._restore_video(ledger, places, history, unit, landings.get(unit.hash))
            if isinstance(outcome, Skipped):
                skipped.append(outcome)
                continue
            action, repair = outcome
            actions.append(action)
            if repair is not None:
                skipped.append(repair)
        return actions

    def _restore_video(
        self,
        ledger: _Ledger,
        places: "_Places",
        history: "_History",
        unit: LedgerEntry,
        landing: LedgerEntry | None,
    ) -> tuple[str, Skipped | None] | Skipped:
        """Restore one unit's file and descriptions, then record it done where the file stands."""
        found = _source(history, places, unit, landing)
        if isinstance(found, Skipped):
            return found
        target = places.restore_target(unit, found)
        if target is None:
            return _unrestored(unit, found.landing.path, "another file stands at its path now")
        if not target.exists():
            atomic.write_bytes(target, found.content)
        beside = _put_descriptions(history, found, target)
        rel = places.relative(target)
        append(ledger.path, _done(found.landing, item=target.parent.name, path=rel, at=self._now()))
        post = ledger.latest.get(unit.parent) if unit.parent is not None else None
        match beside.standing:
            case []:
                gone = f"{target.name} was"
            case [_]:
                gone = f"{target.name} and its description were"
            case _:
                gone = f"{target.name} and its descriptions were"
        repair = _redigest(
            places,
            target.parent.name,
            f"with {rel} back — its digest may have been drawn while {gone} gone",
        )
        return _restored_video(places, post, found, target, beside), repair

    def _restore_pages(
        self, ledger: _Ledger, places: "_Places", history: "_History", skipped: list[Skipped]
    ) -> list[str]:
        """Give every page a 0.2.2 re-read left with fewer fences or rows its earlier copy."""
        reread = [entry for entry in ledger.latest.values() if _reread(entry)]
        if not reread:
            return []
        what = f"the {len(reread)} page(s) a 0.2.2 re-read replaced"
        if not history.readable:
            skipped.append(Skipped(what=what, why=f"{_NO_GIT}; none was compared"))
            return []
        sync = history.sync_commit()
        if sync is None:
            skipped.append(Skipped(what=what, why=f"{history.no_sync()}; none was compared"))
            return []
        before = history.landings_at(sync, {entry.hash for entry in reread}, now=ledger.now)
        synced = _Sync(commit=sync, before=before)
        actions: list[str] = []
        for page in reread:
            outcome = self._restore_page(ledger, places, history, synced, page)
            if isinstance(outcome, Skipped):
                skipped.append(outcome)
            elif outcome is not None:
                action, repair = outcome
                actions.append(action)
                if repair is not None:
                    skipped.append(repair)
        return actions

    def _restore_page(
        self,
        ledger: _Ledger,
        places: "_Places",
        history: "_History",
        sync: "_Sync",
        page: LedgerEntry,
    ) -> tuple[str, Skipped | None] | Skipped | None:
        """Restore one page when its re-read has fewer fence lines or table rows; None when not."""
        found = _copies(places, history, sync, page)
        if isinstance(found, str):
            return Skipped(
                what=f"{page.item}: {page.path} of {page.url}", why=f"{found}; {_PAGE_UNTOUCHED}"
            )
        then, now = _counts(found.content), _counts(found.current.read_bytes())
        if now[0] >= then[0] and now[1] >= then[1]:
            return None
        atomic.write_bytes(found.current, found.content)
        restored = _page_line(page, found.earlier, item=found.item, path=found.rel)
        append(ledger.path, replace(restored, date=self._today(), at=self._now()))
        action = (
            f"{found.item}: restored {found.rel} from the 0.2.2 sync commit {sync.commit[:12]} — "
            f"the re-read held {now[0]} fence line(s) and {now[1]} table row(s) where the copy "
            f"before it held {then[0]} and {then[1]}"
        )
        repair = _redigest(
            places,
            found.item,
            "— its digest may have been drawn from the copy the 0.2.2 re-read stored — write "
            f"it again from the restored {found.rel}",
        )
        return action, repair


@dataclass(frozen=True, slots=True, kw_only=True)
class _Sync:
    """The commit that synced 0.2.2, and each re-read page's landing in the ledger it holds."""

    commit: str
    before: dict[str, LedgerEntry]


@dataclass(frozen=True, slots=True, kw_only=True)
class _Copies:
    """A re-read page's file as it stands, and the copy the sync commit held of it."""

    item: str
    current: Path
    rel: str
    earlier: LedgerEntry
    content: bytes


class _Places:
    """Where the ledger's recorded files stand now, and which unit holds each download slot."""

    def __init__(self, root: Path, ledger: _Ledger) -> None:
        # Resolved, as every file it places is (resolve_repo_path).
        self.root = root.resolve()
        self._ledger = ledger
        self._owners: dict[str, tuple[str, ...]] | None = None
        self._restoring: dict[str, LedgerEntry] = {}
        self._named: dict[Path, list[tuple[Path, str]]] = {}

    def live_file(self, entry: LedgerEntry, recorded: str) -> Path | None:
        """``recorded``'s file name under the live item owning ``entry``, held inside the root."""
        item = self.live_item(entry)
        if item is None:
            return None
        return resolve_repo_path(self.root, f"enrichment/{item}/{PurePosixPath(recorded).name}")

    def standing(self, recorded: str, item: str) -> Path | None:
        """The file a landing recorded: at its path, else by its name under ``item``."""
        renamed = f"enrichment/{item}/{PurePosixPath(recorded).name}"
        for repo_path in dict.fromkeys((recorded, renamed)):
            file = resolve_repo_path(self.root, repo_path)
            if file is not None and file.is_file():
                return file
        return None

    def relative(self, file: Path) -> str:
        return str(file.relative_to(self.root))

    def restore_target(self, unit: LedgerEntry, found: _Source) -> Path | None:
        """Where the unit's file goes back: its own name, or the lowest free slot's.

        None where the file is no download and something else stands at its
        name: it has no slot to move to.
        """
        own = self.live_file(unit, found.landing.path or "")
        if own is None:
            return None
        if self._usable(own, unit.hash, found.content):
            return own
        if _slot(own) is None:
            return None
        return next(
            candidate
            for n in itertools.count()
            if self._usable(
                candidate := own.with_name(f"media-{n}{own.suffix}"), unit.hash, found.content
            )
        )

    def expect(self, landings: Iterable[LedgerEntry]) -> None:
        """Hold each slot a unit about to be restored landed in, so no other restore takes it."""
        self._restoring = {landing.hash: landing for landing in landings}
        self._named.clear()

    def live_item(self, entry: LedgerEntry) -> str | None:
        """The live corpus item the unit belongs to, or None where nothing live claims it."""
        if _item_file(self.root, entry.item).exists():
            return entry.item
        if self._owners is None:
            # The corpus answers one question — which live item owns a unit
            # whose stored item is gone — and only a renamed item asks it.
            self._owners = unit_owners(self.root, self._ledger.latest, default_drivers())
        for owner in self._owners.get(entry.hash, ()):
            if _item_file(self.root, owner).exists():
                return owner
        return None

    def _usable(self, file: Path, unit_hash: str, content: bytes) -> bool:
        """Whether ``file`` can be the unit's: no other unit's file holds its name or its slot.

        A download's slot is held by any file of it, whatever the extension:
        one another unit's path names, or one standing that no path names,
        which is nobody's to take over. A file standing where the unit's
        would go is the unit's own only as the very bytes history holds.
        """
        slot = _slot(file)
        for held, owner in self._downloads(file.parent):
            if owner != unit_hash and (held == file or (slot is not None and _slot(held) == slot)):
                return False
        if slot is None:
            standing = [file] if file.exists() else []
        else:
            standing = [p for p in file.parent.glob(f"media-{slot}.*") if is_media_file(p)]
        return all(p == file and p.read_bytes() == content for p in standing)

    def _downloads(self, folder: Path) -> list[tuple[Path, str]]:
        """The files in ``folder`` units' recorded paths name, each with its unit's hash.

        A unit closed without content holds nothing, whatever an earlier
        line of it recorded, unless this apply restores it. Two units can
        name one file, and each holds it.
        """
        if folder not in self._named:
            holding = {
                unit_hash: landing
                for unit_hash, landing in self._ledger.landings.items()
                if self._ledger.latest[unit_hash].status not in _CLOSED
            }
            named: list[tuple[Path, str]] = []
            for unit_hash, landing in (holding | self._restoring).items():
                file = self.live_file(landing, landing.path or "")
                if file is not None and file.parent == folder:
                    named.append((file, unit_hash))
            self._named[folder] = named
        return self._named[folder]


class _History:
    """The instance's git history, as far as this clone can read it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.readable = git_output(root, ["rev-parse", "--git-dir"]) is not None
        self.shallow = git_output(root, ["rev-parse", "--is-shallow-repository"]) == "true\n"

    def missing(self) -> str:
        """Why no revision this clone holds has the file."""
        if self.shallow:
            return (
                "this clone is shallow, and no revision it holds has the file — the commit "
                "that did may lie past its depth"
            )
        return "no commit in history holds it, so it was never committed"

    def no_sync(self) -> str:
        """Why no commit this clone holds records the 0.2.2 sync."""
        if self.shallow:
            return (
                "this clone is shallow, and no commit it holds records migration 18 in "
                f"{_MIGRATIONS_LOG} — the 0.2.2 sync commit may lie past its depth"
            )
        return (
            f"no commit records migration 18 in {_MIGRATIONS_LOG}, so the copies the 0.2.2 sync "
            "left cannot be found"
        )

    def holds(self, revision: str, rel: str) -> bool:
        return git_output(self.root, ["cat-file", "-e", f"{revision}:{rel}"]) is not None

    def holding(self, rel: str) -> str | None:
        """The newest revision whose tree holds ``rel``, as a full commit id.

        The newest commit that touched the path either wrote it, and then
        HEAD holds it as that commit left it, the deletion uncommitted, or
        deleted it, and then its parent holds it as it was last committed.
        HEAD over the commit that wrote it: the descriptions written of the
        file since stand beside it there.
        """
        touched = git_output(self.root, ["log", "-n", "1", "--format=%H %P", "--", rel]) or ""
        commit, *parents = touched.split() or [""]
        if commit and self.holds(commit, rel):
            head = git_output(self.root, ["rev-parse", "--verify", "HEAD"])
            return None if head is None else head.strip()
        return next((rev for rev in parents if self.holds(rev, rel)), None)

    def sync_commit(self) -> str | None:
        """The first commit whose migrations log records migration 18: the 0.2.2 sync.

        The pickaxe also finds a commit that took a record away, so each one
        found is read until one holds the record. A shallow clone's oldest
        commit reads as adding every file it holds, so the record found
        there proves nothing about when it was first written.
        """
        touched = git_output(
            self.root,
            ["log", "--reverse", "--format=%H", f"-S{_REREAD_LOGGED}", "--", _MIGRATIONS_LOG],
        )
        for commit in (touched or "").split():
            text = git_output(self.root, ["show", f"{commit}:{_MIGRATIONS_LOG}"])
            if text is None or not _logs_the_reread(text):
                continue
            if self.shallow and not self._has_parent(commit):
                return None
            return commit
        return None

    def _has_parent(self, commit: str) -> bool:
        return git_output(self.root, ["rev-parse", "--verify", "--quiet", f"{commit}^"]) is not None

    def descriptions(self, revision: str, rel: str) -> Iterator[tuple[str, bytes]]:
        """Every description at ``revision`` that 0.2.2 deleted along with ``rel``'s file."""
        name = PurePosixPath(rel).name
        folder = PurePosixPath(rel).parent
        spellings = {name, f"{folder}/{name}"}
        listed = git_output(self.root, ["ls-tree", "--name-only", revision, "--", f"{folder}/"])
        for path in (listed or "").split("\n"):
            if not fnmatch(PurePosixPath(path).name, "media-*.md"):
                continue
            content = checkout_bytes(self.root, revision, path)
            if content is not None and _described(content) in spellings:
                yield path, content

    def landings(self, hashes: set[str], *, now: datetime.datetime) -> dict[str, LedgerEntry]:
        """Each unit's latest landing in the newest committed ledger that still holds one.

        Only the commits whose ledger change mentions one of the units are
        read, found by one pickaxe query: the one that dropped a landing,
        and its parent, which still held it.
        """
        found: dict[str, LedgerEntry] = {}
        if not self.readable or not hashes:
            return found
        pattern = "|".join(sorted(hashes))
        touched = git_output(self.root, ["log", "--format=%H", f"-G{pattern}", "--", _LEDGER])
        for commit in (touched or "").split():
            for revision in (commit, f"{commit}^"):
                wanted = hashes - found.keys()
                if not wanted:
                    return found
                found |= self.landings_at(revision, wanted, now=now)
        return found

    def landings_at(
        self, revision: str, hashes: set[str], *, now: datetime.datetime
    ) -> dict[str, LedgerEntry]:
        """Each of the units' latest landing in the ledger ``revision`` holds."""
        text = git_output(self.root, ["show", f"{revision}:{_LEDGER}"]) or ""
        landed = ((n, e) for n, e in _records(text, None, only=hashes) if e.path is not None)
        return _latest(landed, now=now)


_NO_GIT = (
    "git answered nothing here — this is no working clone, or git is not installed — and "
    "history is the only place the earlier copies still exist"
)
_PAGE_UNTOUCHED = "the page was left as the re-read stored it"


def _source(
    history: _History, places: _Places, unit: LedgerEntry, landing: LedgerEntry | None
) -> _Source | Skipped:
    """What a retired unit's file held, as the newest revision holding it, or why none says.

    The file is looked for where it stands now, under its live item, and
    else where its landing recorded it: an item renamed after its video
    was retired holds it in history under the old name only.
    """
    if not history.readable:
        return _unrestored(unit, None if landing is None else landing.path, _NO_GIT)
    if landing is None or landing.path is None:
        return _unrestored(unit, None, "no ledger line, current or committed, records its file")
    live = places.live_file(unit, landing.path)
    if live is None or not live.parent.is_dir():
        gone = "no live corpus item claims it" if live is None else "its item has no directory"
        return _unrestored(unit, landing.path, gone)
    for rel in dict.fromkeys((places.relative(live), landing.path)):
        revision = history.holding(rel)
        if revision is None:
            continue
        content = checkout_bytes(history.root, revision, rel)
        if content is None:
            return _unrestored(unit, rel, f"git holds it at {revision[:12]} but could not read it")
        return _Source(history_path=rel, revision=revision, content=content, landing=landing)
    return _unrestored(unit, landing.path, history.missing())


def _copies(
    places: "_Places", history: "_History", sync: _Sync, page: LedgerEntry
) -> _Copies | str:
    """The page's file as it stands and the copy the sync commit held, or why there is no pair.

    The copy is read at the path where the file stands now, else under
    the same file name in the directory the landing at that commit
    recorded: an item renamed after the sync held it under its old id.
    """
    item = places.live_item(page)
    if item is None:
        return "no live corpus item claims it"
    current = places.standing(page.path or "", item)
    if current is None:
        return (
            f"its 0.2.2 re-read landed at {page.path}, and no file stands there or under "
            f"enrichment/{item}/ now"
        )
    earlier = _earlier(sync, page)
    if isinstance(earlier, str):
        return earlier
    rel = places.relative(current)
    renamed = f"{PurePosixPath(earlier.path or '').parent}/{current.name}"
    tried = list(dict.fromkeys((rel, renamed)))
    held = next((p for p in tried if history.holds(sync.commit, p)), None)
    if held is None:
        return (
            f"no file stood at {' or '.join(tried)} in the 0.2.2 sync commit "
            f"{sync.commit[:12]}, so there is no earlier copy to compare"
        )
    content = checkout_bytes(history.root, sync.commit, held)
    if content is None:
        return f"git holds {held} at {sync.commit[:12]} but could not read it"
    return _Copies(item=item, current=current, rel=rel, earlier=earlier, content=content)


def _earlier(sync: _Sync, page: LedgerEntry) -> LedgerEntry | str:
    """The page's landing in the ledger the sync commit holds, or why it cannot serve."""
    earlier = sync.before.get(page.hash)
    if earlier is None:
        return (
            f"no landing of it stood at the 0.2.2 sync commit {sync.commit[:12]}, so there is "
            "no earlier copy to compare"
        )
    if earlier.engine == _REREAD_ENGINE:
        return (
            f"the 0.2.2 sync commit {sync.commit[:12]} already holds its re-read, so history "
            "there cannot say what stood before"
        )
    return earlier


def _put_descriptions(history: _History, found: _Source, target: Path) -> _Beside:
    """Stand beside the restored file every description 0.2.2 deleted with it.

    One moved with its file into another slot is re-pointed to the new
    name, and the one named for the old slot takes the new slot's name.
    """
    beside = _Beside()
    old = PurePosixPath(found.history_path)
    moved = target.name != old.name
    for path, content in history.descriptions(found.revision, found.history_path):
        name = PurePosixPath(path).name
        if moved and name == f"{old.stem}.md":
            name = f"{target.stem}.md"
        body = _repoint(content, target.name) if moved else content
        standing = target.parent / name
        if not standing.exists():
            atomic.write_bytes(standing, body)
        elif not standing.is_file() or standing.read_bytes() != body:
            beside.kept_out.append(name)
            continue
        beside.standing.append(name)
    return beside


def _described(content: bytes) -> str | None:
    """The file a description's first line names, as 0.2.2 read it; None where none."""
    try:
        line = content.decode("utf-8").partition("\n")[0]
    except UnicodeDecodeError:
        return None
    match = _DESCRIBED_RE.match(line)
    return None if match is None else match.group(1)


def _repoint(content: bytes, name: str) -> bytes:
    """The description with the name its first line opens on re-spelled ``name``."""
    first, sep, rest = content.decode("utf-8").partition("\n")
    return (_BACKTICKED_RE.sub(f"`{name}`", first, count=1) + sep + rest).encode("utf-8")


def _done(landing: LedgerEntry, *, item: str, path: str, at: datetime.datetime) -> LedgerEntry:
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


def _page_line(page: LedgerEntry, earlier: LedgerEntry, *, item: str, path: str) -> LedgerEntry:
    """A restored page's line: the unit as it stands, with the restored copy's engine and title.

    Its date and write instant are set by the caller: dated today, because
    the item's enrichment changed back today, which lists it for a digest
    written from the restored copy.
    """
    return LedgerEntry(
        hash=page.hash,
        url=page.url,
        item=item,
        kind=page.kind,
        format=page.format,
        status=Status.DONE,
        http_shared=page.http_shared,
        engine=earlier.engine,
        date=page.date,
        job=page.job,
        via=_VIA,
        parent=page.parent,
        depth=page.depth,
        path=path,
        title=earlier.title,
    )


def _reread(entry: LedgerEntry) -> bool:
    """A page whose live line is the landing of 0.2.2's migration-18 re-read."""
    return (
        entry.job is None
        and entry.kind in _PAGES
        and entry.status is Status.DONE
        and entry.engine == _REREAD_ENGINE
        and entry.via == _REREAD_VIA
        and entry.path is not None
    )


def _counts(content: bytes) -> tuple[int, int]:
    """A copy's fence lines and table rows."""
    text = content.decode("utf-8", "replace")
    return len(_FENCE_RE.findall(text)), len(_ROW_RE.findall(text))


def _logs_the_reread(text: str) -> bool:
    """Whether a migrations log's text records migration 18."""
    for line in text.split("\n"):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("number") == _REREAD_NUMBER:
            return True
    return False


def _records(
    text: str, skipped: list[Skipped] | None, *, only: set[str] | None = None
) -> Iterator[tuple[int, LedgerEntry]]:
    """Every ledger line that parses, with its position; the rest named in ``skipped``.

    ``only`` narrows the read to those units' lines, so a walk through the
    ledger's history never parses every other line of every revision.
    """
    for position, line in enumerate(text.split("\n")):
        if not line.strip() or (only is not None and not any(h in line for h in only)):
            continue
        try:
            entry = from_line(line)
        except (LedgerSchemaError, ValueError):
            if skipped is not None:
                skipped.append(
                    Skipped(
                        what=f"ledger line {position + 1}",
                        why="does not parse — left untouched; migration 20 skipped it",
                    )
                )
            continue
        if only is None or entry.hash in only:
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


def _slot(file: Path) -> str | None:
    """The slot a ``media-<n>.<ext>`` download names, or None for any other file."""
    match = _SLOT_RE.match(file.name)
    return None if match is None else match[1]


def _unrestored(unit: LedgerEntry, rel: str | None, why: str) -> Skipped:
    """A retired unit history could not give back, with the session's route to it."""
    if rel is not None and rel.endswith(".md"):
        route = f"write it again and close the unit with `enrich mark {unit.url} done --path {rel}`"
    else:
        route = (
            f"requeue it with `enrich mark {unit.url} queued` to download it again, "
            "then describe it"
        )
    return Skipped(
        what=f"{unit.item}: {rel or 'the file'} of media unit {unit.url}",
        why=f"{why}; nothing was restored and the unit still reads skipped — {route}",
    )


def _redigest(places: _Places, item: str, why: str) -> Skipped | None:
    """The session's repair of a digest drawn before the restore; None where there is none."""
    digest = f"state/digests/{item}.md"
    if not (places.root / digest).is_file():
        return None
    return Skipped(
        what=digest,
        why=f"re-digest {item} {why}; carry none of that digest's facts forward",
    )


def _restored_video(
    places: _Places, post: LedgerEntry | None, found: _Source, target: Path, beside: _Beside
) -> str:
    """What the restore gave back."""
    heard = "its post" if post is None else post.url
    rel = places.relative(target)
    line = (
        f"{target.parent.name}: restored {rel} from {found.revision[:12]}, which 0.2.2 deleted "
        f"when the transcript of {heard} landed"
    )
    old = PurePosixPath(found.history_path).name
    if old != target.name:
        line += (
            f" — into slot {target.stem.removeprefix('media-')}, since another unit's file "
            f"holds the slot of {old} now"
        )
    if beside.standing:
        noun = "description" if len(beside.standing) == 1 else "descriptions"
        line += f", with its {noun} {', '.join(beside.standing)} beside it"
    line += "; its media unit is done at that path again"
    if beside.kept_out:
        line += f" ({', '.join(beside.kept_out)} not put back: another file stands at that name)"
    if found.content.startswith(_LFS_POINTER):
        line += (
            " — as its LFS pointer, since no store this clone reaches holds the video; "
            "`git lfs pull` brings the bytes"
        )
    return line
