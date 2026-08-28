"""Migration 7 — restore article bodies a wayback rescue overwrote.

A rerun whose live fetch was refused fell back to a wayback snapshot, and
the snapshot carried only the member-only preview — a fraction of the
stored body. The run rewrote the file with the preview and reported the
unit as done with new material. The fixed engine keeps the stored body
when a re-fetch comes back under half its size; this repairs the files the
old engine already traded away.

The lost body exists in exactly one place — the instance's own git
history — so this migration reads it back. Candidates are the enrichment
files stamped ``via: wayback`` (the rescue stamps its provenance, and has
since it shipped). For each, the file's committed revisions are walked
newest-first, and the first whose body is at least twice the current one
and is NOT itself a wayback rescue is the direct fetch the rescue
clobbered: the file is restored to that revision whole — frontmatter and
body together, the fetch they belong to. A candidate with no such
ancestor was BORN of its rescue — the ordinary case, where wayback saved
a page nothing else could reach — and is left alone without comment.

Reading git is new ground for a migration and deliberately narrow: two
read-only commands (``log``, ``show``) and one working-tree write through
the same atomic path every state write takes. Nothing is committed —
the sync that ran this migration commits, as it does every migration's
work. An instance where git itself fails answers with one skip naming
the failure, and every file is left exactly as it was.

Idempotent: a restored file no longer carries the wayback stamp, so a
second apply does not consider it; a candidate left alone is scanned
again and left alone again.
"""

import datetime
import subprocess
from collections.abc import Callable
from pathlib import Path

from dex_engine import atomic
from dex_engine.pipeline.enrichment import read_enrichment
from dex_engine.pipeline.types import MigrationReport, Skipped

__all__ = ["WaybackClobberRestore", "build"]

NUMBER = 7
INTENT = (
    "restore article bodies a wayback rescue overwrote: a refused live fetch fell back "
    "to a snapshot holding only a preview, which replaced the complete stored body and "
    "reported as new material"
)

# The overwrite proof, shared with the engine's landing guard: a page
# shrinks a little when edited; it does not lose half of itself.
_SHRINK_FACTOR = 2


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — the shared build signature; nothing here is dated
    now: Callable[[], datetime.datetime],  # noqa: ARG001 — and nothing here is a ledger line
    engine_version: str,  # noqa: ARG001 — nothing here is version-stamped
) -> "WaybackClobberRestore":
    """Build migration 7 (the shared build signature; no state is stamped)."""
    return WaybackClobberRestore()


class WaybackClobberRestore:
    """Migration 7: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def apply(self, root: Path) -> MigrationReport:
        """Restore every wayback-stamped file its history proves was clobbered.

        Args:
            root: The instance root.

        Returns:
            The report: one action per restored file naming both sizes and
            the revision restored from, and a skip for anything git or the
            file itself refused to answer.
        """
        enrichment = root / "enrichment"
        if not enrichment.is_dir():
            return MigrationReport()
        actions: list[str] = []
        skipped: list[Skipped] = []
        checked = _git(root, "rev-parse", "--git-dir")
        if checked is None:
            return MigrationReport(
                skipped=[
                    Skipped(
                        what="the wayback-clobber sweep",
                        why="git answered nothing here, and history is the only place "
                        "the clobbered bodies still exist — run `dex sync` from a "
                        "working clone",
                    )
                ]
            )
        for file in sorted(enrichment.glob("*/*.md")):
            self._restore(root, file, actions, skipped)
        return MigrationReport(actions=actions, skipped=skipped)

    def _restore(self, root: Path, file: Path, actions: list[str], skipped: list[Skipped]) -> None:
        """Restore one candidate when its history proves the clobber."""
        rel = file.relative_to(root).as_posix()
        try:
            fields, body = read_enrichment(file)
        except (OSError, UnicodeDecodeError):
            skipped.append(
                Skipped(
                    what=rel,
                    why="could not be read, so whether it is a wayback rescue is unknown",
                )
            )
            return
        if fields.get("via") != "wayback":
            return
        log = _git(root, "log", "--format=%H", "--", rel)
        if log is None:
            skipped.append(Skipped(what=rel, why="git log failed — history unreadable"))
            return
        for revision in log.split():
            stored = _git(root, "show", f"{revision}:{rel}")
            if stored is None:
                continue  # the revision renamed or cannot decode; older ones may answer
            if _is_wayback(stored):
                continue
            stored_body = _body_of(stored)
            if len(stored_body) < _SHRINK_FACTOR * len(body):
                # The nearest direct fetch does not dwarf the rescue: the
                # rescue was a real answer, not a clobber. Older revisions
                # only get further from the page and prove nothing more.
                return
            atomic.write_text(file, stored)
            actions.append(
                f"restored {rel} from {revision[:12]}: the wayback rescue held "
                f"{len(body)} chars over a stored body of {len(stored_body)} — the "
                "preview left with the rescue stamp, and the item re-lists for "
                "digesting through the staleness backstop"
            )
            return

    # `_restore` returning without an action is the ordinary case: the file
    # was born of its rescue, and there is nothing older to prefer.


def _git(root: Path, *args: str) -> str | None:
    """One read-only git answer, or None for any refusal.

    None is deliberately the only failure shape: which of git's refusals
    happened (no repo, no commits, a renamed path, undecodable bytes) never
    changes what a caller may do — proceed without history.
    """
    try:
        done = subprocess.run(  # noqa: S603 — engine-built args, no shell
            ["git", "-C", str(root), *args],  # noqa: S607 — PATH resolution is the dependency contract
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if done.returncode != 0:
        return None
    try:
        return done.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_wayback(text: str) -> bool:
    """Whether an enrichment file's text carries the ``via: wayback`` stamp."""
    head = text[4:].partition("\n---\n")[0] if text.startswith("---\n") else ""
    for line in head.split("\n"):
        key, sep, value = line.partition(":")
        if sep and key.strip() == "via":
            return value.strip().strip('"') == "wayback"
    return False


def _body_of(text: str) -> str:
    """The body below an enrichment file's frontmatter fence, stripped.

    The same split ``read_enrichment`` applies, on text instead of a path:
    a fence that opens and never closes yields the whole text, exactly as
    the reader treats it.
    """
    if not text.startswith("---\n"):
        return text.strip()
    _head, sep, body = text[4:].partition("\n---\n")
    return body.strip() if sep else text.strip()
