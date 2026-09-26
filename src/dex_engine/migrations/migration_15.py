"""Migration 15 — re-read the github links the old dispatch misread.

Until this release the github driver told three shapes apart — a blob, an
issue or pull request, and everything else — and everything else was the
repo itself: whatever a link addressed below ``owner/repo``, the driver
stored the repository's metadata over its default branch's root README. A
tree link to a subdirectory, a branch root, a commit, a release page, a
raw file, a wiki page: each landed ``done`` holding a page it does not
point at, and nothing in the report or the ledger said so. Three shapes
failed instead. A ``user-attachments`` link was read as owner
``user-attachments``, repo ``files``, and 404'd into ``dead``. A blob link
naming a directory got the contents API's array answer, blocked on it
until it escalated to ``manual``. An issue link whose number is not one
(``issues/new``) asked the API for issue "new" and died. The fixed driver
reads every one of these — but a unit that has landed, died or parked is
never fetched again, so on every existing instance the damage stays.

Members are read off what the engine stored: each live github page line's
URL, classified the way the old dispatch classified it, and its status.

- **An attachment**, ``dead`` — or ``done``, which only a hand heal can
  have made, since the old route never read one.
- **A link the old dispatch sent to the repo route** — anything below
  ``owner/repo`` that is not a blob, an issue or a pull request —
  ``done`` over that route's own output: frontmatter recording the unit's
  URL, carrying ``stars``, which only the repo route writes, and no
  ``ref``, which the fixed route writes whenever a link names a ref. The
  fixed engine lands this route for a link naming a ref alone, so that key
  is the whole difference between a correct landing and the old collapse.
  A line whose stored output is missing altogether is a member too: an
  apply interrupted between the delete and the seed leaves exactly that,
  and the rerun is what such a line needs either way. A stored output of
  any other shape — the fixed engine's, or the owner's own heal — stands.
- **A blob link** ``manual`` on the old failure's own words: the
  escalation recorded ``gh api returned an unexpected shape``, the array
  answer a directory gets.
- **An issue or pull request link whose number is not a number**, ``dead``.

Each member is seeded ``{queued, rerun, via: migration-15}``, its kind,
parent and depth kept, and the fixed driver routes it: an attachment to
file or media work, a directory to its README and listing, a raw link to
its file, a document download to file work, and every other shape to a
park naming it. A repo-route output is wrong rather than incomplete — it
is a different page — so it is deleted BEFORE the seed: left standing,
the drain would keep it over a smaller re-fetch, and keep it again
whenever a re-fetch fails. A hand heal of an attachment is the owner's
reading and stays; the landing that replaces it retires it through the
superseded-output drop. A digested item that lost an output has its
digest named for the session, because that digest was written from the
root README.

Which live item a seed writes under is the runtime's own answer
(``unit_owners``): the items listing the URL, the stored item where its
corpus file stands, and a harvested child's parent chain. A unit no live
item claims is skipped with the reason, naming the ``state/exclusions.tsv``
entry where one exists, and nothing of it is deleted.

Idempotent: a seeded line is ``queued``, which no member is, so a second
apply before the drain finds nothing; after it, the fixed engine has
parked the unit, corrected its kind away from github, or landed an output
without ``stars`` or with ``ref``.
"""

import datetime
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from dex_engine.pipeline.enrichment import read_enrichment
from dex_engine.pipeline.ledger import (
    LedgerSchemaError,
    append,
    from_line,
    resolution_key,
    stamp,
)
from dex_engine.pipeline.ownership import unit_owners
from dex_engine.pipeline.registry import default_drivers
from dex_engine.pipeline.types import (
    Kind,
    LedgerEntry,
    MigrationReport,
    Skipped,
    Status,
)
from dex_engine.pipeline.urls import host_of, resolve_repo_path

__all__ = ["MisreadGithubRequeue", "build"]

NUMBER = 15
INTENT = (
    "re-read the github links the old dispatch misread: a link below owner/repo that "
    "landed the repo's root README loses that file, and it, an attachment read as a repo, "
    "a blob link blocked on a directory and an issue link with no number requeue as "
    "{queued, rerun, via: migration-15} for the fixed routes"
)

_VIA = "migration-15"
_ATTACHMENTS = "user-attachments"
_DIRECTORY_REFUSAL = "gh api returned an unexpected shape"
_REPO_ROUTE_FIELD = "stars"
_REF_FIELD = "ref"


class _Shape(Enum):
    """A link shape the old dispatch misread, named by what it did with it."""

    ATTACHMENT = auto()
    COLLAPSED = auto()
    BLOB = auto()
    NOT_AN_ISSUE = auto()


def build(
    *,
    today: Callable[[], datetime.date],
    now: Callable[[], datetime.datetime],
    engine_version: str,
) -> "MisreadGithubRequeue":
    """Build migration 15; seeds are stamped with the injected clocks and engine."""
    return MisreadGithubRequeue(today=today, now=now, engine_version=engine_version)


class MisreadGithubRequeue:
    """Migration 15: see the module docstring."""

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
        """Delete every root README stored for a link it is not, and requeue the misread.

        Args:
            root: The instance root.

        Returns:
            The report: a summary action with the count, one action per
            deleted output, a digest repair for every item that lost one,
            and a skip for every unparseable ledger line, unreadable
            output, and member no live item claims.
        """
        skipped: list[Skipped] = []
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport()
        members = _members(root, _latest_per_hash(path, skipped, now=self._now()), skipped)
        if not members:
            return MigrationReport(skipped=skipped)
        actions: list[str] = []
        for member in members:
            # Deleted before the seed: a queued line over the wrong file is
            # what a failed re-fetch would keep.
            for repo_path, file in member.wrong:
                file.unlink()
                actions.append(
                    f"deleted {repo_path}: the repo's root README the old dispatch stored for "
                    f"{member.entry.url}, which is not what the link points at — the rerun "
                    "replaces it"
                )
            append(path, self._stamped(_seed(member.entry, member.item)))
        skipped.extend(_digest_repairs(root, members))
        return MigrationReport(actions=[_summary(members), *actions], skipped=skipped)

    def _stamped(self, entry: LedgerEntry) -> LedgerEntry:
        return stamp(entry, today=self._today, now=self._now, engine_version=self._engine_version)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Member:
    """A unit to re-read, the live item it writes under, and what to delete first."""

    entry: LedgerEntry
    item: str
    wrong: list[tuple[str, Path]]


def _members(root: Path, latest: dict[str, LedgerEntry], skipped: list[Skipped]) -> list[_Member]:
    """Every live line recording a misreading that a live item still claims."""
    found = [(entry, shape) for entry in latest.values() if (shape := _misread(entry)) is not None]
    if not found:
        return []
    owners = unit_owners(root, latest, default_drivers())
    exclusions = _exclusions(root)
    members: list[_Member] = []
    for entry, shape in found:
        item = _live_item(root, owners[entry.hash])
        wrong: list[tuple[str, Path]] = []
        if shape is _Shape.COLLAPSED:
            outputs = _repo_route_outputs(root, entry, item or entry.item, skipped)
            if outputs is None:
                continue
            wrong = outputs
        if item is None:
            skipped.append(_unclaimed(entry, exclusions))
            continue
        members.append(_Member(entry=entry, item=item, wrong=wrong))
    return members


def _summary(members: list[_Member]) -> str:
    summary = (
        f"seeded {len(members)} github rerun(s): links the old dispatch read as the repo's "
        "root README, as a repo named user-attachments, as a file when they named a "
        "directory, or as an issue when they named none — the fixed routes read what each "
        "points at"
    )
    reattributed = sum(1 for member in members if member.item != member.entry.item)
    if reattributed:
        summary += f"; {reattributed} re-attributed to the renamed item that claims them"
    return summary


def _misread(entry: LedgerEntry) -> _Shape | None:
    """The misreading this live line records, or None when it records none."""
    if entry.kind is not Kind.GITHUB or entry.job is not None:
        return None
    match _old_shape(entry.url), entry.status:
        case _Shape.ATTACHMENT, Status.DEAD | Status.DONE:
            return _Shape.ATTACHMENT
        case _Shape.COLLAPSED, Status.DONE:
            return _Shape.COLLAPSED
        case _Shape.BLOB, Status.MANUAL if _DIRECTORY_REFUSAL in (entry.reason or ""):
            return _Shape.BLOB
        case _Shape.NOT_AN_ISSUE, Status.DEAD:
            return _Shape.NOT_AN_ISSUE
    return None


def _old_shape(url: str) -> _Shape | None:
    """How the old dispatch read a github.com link, for the shapes it misread.

    Its own rules, frozen here: a first segment of ``user-attachments`` was
    an owner like any other; a blob needed a ref after it and an issue or
    pull request a number's place; everything else below ``owner/repo``
    went to the repo route. A profile and the repo itself were read right.
    """
    if host_of(url) != "github.com":
        return None
    segments = [segment for segment in urllib.parse.urlsplit(url).path.split("/") if segment]
    if segments and segments[0].lower() == _ATTACHMENTS:
        return _Shape.ATTACHMENT
    match segments[2:]:
        case []:
            return None
        case ["blob", _, *_]:
            return _Shape.BLOB
        case ["issues" | "pull", number, *_]:
            return None if number.isdigit() else _Shape.NOT_AN_ISSUE
        case _:
            return _Shape.COLLAPSED


def _repo_route_outputs(
    root: Path, entry: LedgerEntry, item: str, skipped: list[Skipped]
) -> list[tuple[str, Path]] | None:
    """The repo-route outputs stored for this unit, or None when it is no member.

    Two places are asked: the stored ``path``, and the engine's own name
    for the unit under its live item — the one the drain keeps a larger
    stored body at. Nothing stored at either is a member with nothing to
    delete; an output of any other shape, or one that cannot be read, is
    no proof of the collapse, and the unit is left alone.
    """
    stored = _stored_outputs(root, entry, item)
    if not stored:
        return []
    wrong: list[tuple[str, Path]] = []
    for repo_path, file in stored:
        try:
            fields, _body = read_enrichment(file)
        except (OSError, UnicodeDecodeError):
            skipped.append(
                Skipped(
                    what=repo_path,
                    why=f"could not be read, so whether it is the repo's root README stored "
                    f"for {entry.url} is unknown — if it is, delete it and requeue the link "
                    "with `enrich fetch --force`",
                )
            )
            return None
        if (
            fields.get("url") == entry.url
            and _REPO_ROUTE_FIELD in fields
            and _REF_FIELD not in fields
        ):
            wrong.append((repo_path, file))
    return wrong or None


def _stored_outputs(root: Path, entry: LedgerEntry, item: str) -> list[tuple[str, Path]]:
    """The files standing at the stored ``path`` and at the engine's name, each once.

    Both are data a ledger line spells, so both resolve through the one
    containment check before anything is read, let alone deleted.
    """
    named = f"enrichment/{item}/{entry.kind.value}-{entry.hash[:6]}.md"
    found: dict[Path, str] = {}
    for repo_path in (entry.path, named):
        if repo_path is None:
            continue
        file = resolve_repo_path(root, repo_path)
        if file is not None and file.is_file():
            found.setdefault(file, repo_path)
    return [(repo_path, file) for file, repo_path in found.items()]


def _seed(entry: LedgerEntry, item: str) -> LedgerEntry:
    """The rerun line: the unit's own identity and lineage, queued again."""
    return LedgerEntry(
        hash=entry.hash,
        url=entry.url,
        item=item,
        kind=entry.kind,
        status=Status.QUEUED,
        http_shared=entry.http_shared,
        engine="seed",  # stamped in apply
        date=datetime.date.min,
        # A harvested link carries lineage of its own — parent and depth
        # travel together, so both ride the seed.
        parent=entry.parent,
        depth=entry.depth,
        via=_VIA,
        rerun=True,
    )


def _digest_repairs(root: Path, members: list[_Member]) -> list[Skipped]:
    """One repair per item whose standing digest read a deleted root README."""
    misfiled: dict[str, list[str]] = {}
    for member in members:
        if member.wrong:
            misfiled.setdefault(member.item, []).append(member.entry.url)
    repairs: list[Skipped] = []
    for item, urls in sorted(misfiled.items()):
        digest = f"state/digests/{item}.md"
        if not (root / digest).is_file():
            continue
        repairs.append(
            Skipped(
                what=digest,
                why=f"written from the repo's root README the old dispatch stored for "
                f"{', '.join(urls)} instead of what the link points at; that file is "
                f"deleted and the link re-fetches in this run — once it lands, re-digest "
                f"{item} from a fresh reading and carry none of the old digest's facts forward",
            )
        )
    return repairs


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
                    why="does not parse — left untouched; the github re-read skipped it",
                )
            )
            continue
        key = resolution_key(entry, position, now=now)
        if entry.hash in winning and key < winning[entry.hash]:
            continue
        latest[entry.hash] = entry
        winning[entry.hash] = key
    return latest


def _live_item(root: Path, owners: tuple[str, ...]) -> str | None:
    """The first owner with a corpus file, or None when no live item claims the unit."""
    return next((item for item in owners if _item_path(root, item).is_file()), None)


def _item_path(root: Path, item: str) -> Path:
    return root / "corpus" / item[:4] / f"{item}.md"


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


def _unclaimed(entry: LedgerEntry, exclusions: dict[str, str]) -> Skipped:
    """Why a member no live item claims is left exactly as it stands."""
    if entry.item in exclusions:
        reason = exclusions[entry.item] or "no reason recorded"
        why = (
            f"the item is excluded on the record (state/exclusions.tsv: {reason}) — "
            f"{entry.url} is not re-read"
        )
    else:
        why = (
            f"no live corpus item claims {entry.url} and no state/exclusions.tsv record names "
            "the item — nothing claims this work, so the rerun would have no item to write "
            "under; not re-read. If the item was renamed and no longer lists the link, "
            "requeue it by hand with `enrich fetch --force`"
        )
    return Skipped(what=f"github re-read for {entry.item}", why=why)
