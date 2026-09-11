"""Migration 14 — re-point or retire the descriptions migration 13 unmoored.

Migration 13 moved media out from under the descriptions written against
it, two ways, and left both standing.

**Renamed.** A file whose bytes named another format was renamed in
place — ``media-0.jpg`` holding PNG bytes became ``media-0.png`` — and
the item's digest was re-pointed to the new name. The description was
not: its first line still names the old spelling. Those are the same
bytes, so the reading is still true, and the repair is a re-point of
that one name. Left alone it is worse than wrong — the first line is the
only tie between a description and the file it covers, so ``enrich item
describe`` no longer finds it, and a session revising the reading writes
a SECOND description into a fresh slot with the stale one still counted
beside it.

**Deleted.** A file whose bytes were not media at all was deleted and
its unit requeued, and migration 13's report asked the session to
"rewrite or remove" the description. Neither half is reachable:
``describe`` only accepts a file the item still carries, hand-writing a
description is what that verb exists to prevent, and the describe row
compares counts — with the file gone the item reads as zero media and
one description, which is met, so no run report, status listing or
health check names it again. Worse than silent: the requeued unit
re-fetches, a landing file takes the same slot, the count reads one to
one and the stale reading — "the file is zero bytes, there is nothing to
describe" — is silently adopted for a real image, reaching the digest's
``media:`` listing and any page that embeds it. Nothing mechanical can
notice, because the description's *content* is what went stale and no
check reads content.

Migration 13 now does both of these itself. This migration is for the
instances that already ran it.

Membership: every ``enrichment/<item>/media-<n>.md`` under a live corpus
item whose first line names a file — the verb's own ``Describes `<file>`
`` opening, read for the name alone so a description written before the
verb existed is found too — where nothing on disk answers to that name.
Three spellings are resolved before a description is called unmoored:
the verb's bare download name, the repo path a pre-verb description
wrote, and the bare basename of a path the item's ``media:`` states. A
description whose first line names nothing is left alone: what it covers
is not derivable, and a migration that guessed would be destroying a
session's writing on a hunch.

The slot decides which repair:

- **One media file in the slot** — the rename. The backticked name in
  the first line becomes that file's bare name, the spelling the verb
  itself writes and the one ``--of`` takes; every other byte of the file
  is untouched, including whatever prose the line carries after the name
  (a size, a caveat — all still true of the same bytes).
- **Nothing in the slot** — the deletion. ``media-<n>.md`` becomes
  ``discarded-media-<n>.md``: out of the family the describe row counts
  and the describe verb searches, text untouched. A rename, never a
  delete — a description of discarded bytes is often the only record of
  what a URL answered with, and two in the field turned out to hold the
  full text of a page later and fuller than the item's own enrichment.
- **More than one** — an anomaly. Which file the reading covers is not
  derivable, and this migration did not create that state.

What it leaves for the session: nothing, by design. Where the item still
carries undescribed media, the describe row reappears on the next run
report and is answered the ordinary way.

Idempotent: a re-pointed first line names a file that is there, and a
retired description no longer matches ``media-*.md``, so a second apply
finds no members.
"""

import datetime
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from dex_engine import atomic, corpus
from dex_engine.pipeline.describe import described_file
from dex_engine.pipeline.run import is_media_file
from dex_engine.pipeline.types import MigrationReport
from dex_engine.pipeline.urls import resolve_repo_path

__all__ = ["UnmooredDescriptionRepair", "build"]

NUMBER = 14
INTENT = (
    "re-point or retire the descriptions migration 13 unmoored: a first line naming a file "
    "the rename replaced is re-pointed to it, and one naming a file that is simply gone is "
    "renamed to discarded-media-<n>.md, out of the family the describe row counts"
)

_RETIRED_PREFIX = "discarded-"
_SLOT_RE = re.compile(r"^media-(\d+)\.")
_BACKTICKED_RE = re.compile(r"`[^`]+`")


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — the shared build signature; nothing here is dated
    now: Callable[[], datetime.datetime],  # noqa: ARG001 — and nothing here is a ledger line
    engine_version: str,  # noqa: ARG001 — nothing here is version-stamped
) -> "UnmooredDescriptionRepair":
    """Build migration 14 (the shared build signature; no state is stamped)."""
    return UnmooredDescriptionRepair()


@dataclass(frozen=True, slots=True, kw_only=True)
class _Unmoored:
    """A description, the item holding it, the name it carries, and its slot's media."""

    item: str
    path: Path
    names: str
    in_slot: list[Path]


class UnmooredDescriptionRepair:
    """Migration 14: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def apply(self, root: Path) -> MigrationReport:
        """Re-point every description whose file was renamed; retire the rest.

        Args:
            root: The instance root.

        Returns:
            The report: a summary action with both counts, one action per
            description repaired, and an anomaly for every slot holding
            more than one media file or whose retired name was taken.
        """
        enrichment = root / "enrichment"
        if not enrichment.is_dir():
            return MigrationReport()
        actions: list[str] = []
        anomalies: list[str] = []
        repointed = retired = 0
        for found in _unmoored(root, enrichment):
            match found.in_slot:
                case [landed]:
                    _repoint(found.path, landed.name)
                    repointed += 1
                    actions.append(
                        f"{found.item}: {_relative(root, found.path)} named `{found.names}`, "
                        f"which migration 13 renamed to {landed.name} after reading its bytes; "
                        "first line re-pointed to that name and nothing else touched — the "
                        "name is the only tie between a description and the file it covers, "
                        "and describe finds it again now"
                    )
                case []:
                    retired += 1 if _retire(root, found, actions, anomalies) else 0
                case _:
                    anomalies.append(
                        f"{_relative(root, found.path)} named `{found.names}`, and its slot "
                        f"holds {len(found.in_slot)} media files "
                        f"({', '.join(sorted(p.name for p in found.in_slot))}) — which one the "
                        "reading covers is not derivable; describe the files afresh and "
                        "delete what no longer applies"
                    )
        if not actions:
            return MigrationReport(anomalies=anomalies)
        summary = (
            f"re-pointed {repointed} description(s) to the file migration 13 renamed and "
            f"retired {retired} left standing over a file it deleted — a description is tied "
            "to its file by the name in its first line, and migration 13 moved the files "
            "without moving the names"
        )
        return MigrationReport(actions=[summary, *actions], anomalies=anomalies)


def _retire(root: Path, found: _Unmoored, actions: list[str], anomalies: list[str]) -> bool:
    """Rename the description out of the counted family; True when it moved."""
    target = found.path.with_name(_RETIRED_PREFIX + found.path.name)
    if target.exists():
        anomalies.append(
            f"{_relative(root, found.path)}: {_relative(root, target)} already exists, so the "
            "retirement would overwrite a standing file — read both and keep the one that "
            "describes something"
        )
        return False
    found.path.rename(target)
    actions.append(
        f"{found.item}: {_relative(root, found.path)} described `{found.names}`, which "
        f"nothing on disk answers to; retired to {_relative(root, target)} with its text "
        "untouched — out of the family the describe row counts, so a file landing in that "
        "slot is described afresh instead of inheriting this reading"
    )
    return True


def _repoint(path: Path, name: str) -> None:
    """Re-spell the first line's backticked name; every other byte stands."""
    first, sep, rest = path.read_text(encoding="utf-8").partition("\n")
    atomic.write_text(path, _BACKTICKED_RE.sub(f"`{name}`", first, count=1) + sep + rest)


def _unmoored(root: Path, enrichment: Path) -> list[_Unmoored]:
    """Every description under a live item naming a file nothing answers to."""
    found: list[_Unmoored] = []
    for item_dir in sorted(p for p in enrichment.iterdir() if p.is_dir()):
        item = item_dir.name
        stated = _stated_media(root, item)
        if stated is None:
            continue
        for path in sorted(item_dir.glob("media-*.md")):
            names = described_file(path)
            if names is None or _carried(root, item_dir, stated, names):
                continue
            found.append(
                _Unmoored(item=item, path=path, names=names, in_slot=_slot_media(item_dir, names))
            )
    return found


def _stated_media(root: Path, item: str) -> tuple[str, ...] | None:
    """The item's stated media, or None where no live corpus item reads.

    An enrichment directory outliving its item is not this migration's
    business: nothing reads its descriptions either.
    """
    path = root / "corpus" / item[:4] / f"{item}.md"
    try:
        return tuple(corpus.read_item(path).media)
    except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError):
        return None


def _carried(root: Path, item_dir: Path, stated: tuple[str, ...], names: str) -> bool:
    """Whether anything on disk answers to ``names``, read generously.

    Wider than the describe verb's own reading on purpose: that verb
    decides what a session may describe, this decides what a migration
    may move, and only a name nothing whatsoever answers to is safe to
    act on. All three spellings the field carries are resolved — the
    verb's bare download name, the repo path a pre-verb description
    wrote, and the bare basename of a path the item's ``media:`` states.
    """
    if "/" in names:
        resolved = resolve_repo_path(root, names)
        return resolved is not None and resolved.is_file()
    if (item_dir / names).is_file():
        return True
    return any(PurePosixPath(entry).name == names and _exists(root, entry) for entry in stated)


def _exists(root: Path, repo_path: str) -> bool:
    """Whether a stated repo path resolves to a file under the root."""
    resolved = resolve_repo_path(root, repo_path)
    return resolved is not None and resolved.is_file()


def _slot_media(item_dir: Path, names: str) -> list[Path]:
    """The media files in the slot ``names`` belongs to; empty where it has none.

    A capture's media carries no slot, so a name that is not a download
    answers with nothing and the description retires rather than
    re-points.
    """
    slot = _SLOT_RE.match(PurePosixPath(names).name)
    if slot is None:
        return []
    return sorted(p for p in item_dir.glob(f"media-{slot.group(1)}.*") if is_media_file(p))


def _relative(root: Path, path: Path) -> str:
    """``path`` as the repo spells it."""
    return str(path.relative_to(root))
