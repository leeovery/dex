"""Migration 6 — split pass records holding a pasted list of ids.

``enrich pass`` accepted its item argument verbatim, so a multi-line paste
of ids landed as ONE record whose ``item`` field holds the whole blob. No
item can ever claim it — pass readers match by the id's trailing shortid —
so every item in the paste read as never passed, while the session that
wrote the record read the stage as covered.

The repair re-emits what the verb should have written: one record per
whitespace-separated token that a live corpus item claims — its exact id,
or the unique live item carrying its trailing shortid, the same resolution
every pass reader applies — with every other field carried verbatim.
Tokens no live item claims are dropped and named for the session, which
knows whether the stage actually ran for them and can re-record with the
fixed verb (it now refuses non-item arguments at the door).

The rewrite touches only the blob lines: every other line is carried
byte-for-byte, so a file with no blobs is not rewritten at all and the
union-merged history stays quiet. An unparseable line is carried verbatim
too — a line this migration cannot read is not one it may destroy.

Idempotent: the rewrite leaves no record whose item field holds
whitespace, so a second pass finds nothing to split.
"""

import datetime
import json
from collections.abc import Callable
from pathlib import Path

from dex_engine import atomic
from dex_engine.pipeline.types import MigrationReport, Skipped

__all__ = ["PassBlobSplit", "build"]

NUMBER = 6
INTENT = (
    "split pass records holding a pasted list of ids: the verb accepted the blob as one "
    "item, landing a record no item can claim while the stage read as covered"
)


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — the shared build signature; nothing here is dated
    now: Callable[[], datetime.datetime],  # noqa: ARG001 — and nothing here is a ledger line
    engine_version: str,  # noqa: ARG001 — nothing here is version-stamped
) -> "PassBlobSplit":
    """Build migration 6 (the shared build signature; no state is stamped)."""
    return PassBlobSplit()


class PassBlobSplit:
    """Migration 6: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def apply(self, root: Path) -> MigrationReport:
        """Split every whitespace-bearing pass record into per-item records.

        Args:
            root: The instance root.

        Returns:
            The report: one action per split naming the counts, and a skip
            for every token no live item claims.
        """
        path = root / "state" / "passes.jsonl"
        if not path.exists():
            return MigrationReport()
        actions: list[str] = []
        skipped: list[Skipped] = []
        live = {item.stem for item in (root / "corpus").glob("*/*.md")}
        out: list[str] = []
        split_any = False
        for line in path.read_text(encoding="utf-8").split("\n"):
            record = _blob_record(line)
            if record is None:
                out.append(line)
                continue
            split_any = True
            tokens = str(record["item"]).split()
            emitted = 0
            for token in tokens:
                owner = _live_owner(token, live)
                if owner is None:
                    skipped.append(
                        Skipped(
                            what=f"pass token {token!r} (stage {record.get('stage')})",
                            why="no live corpus item claims it — if the stage really ran "
                            "for that item, re-record with `dex enrich pass`",
                        )
                    )
                    continue
                out.append(json.dumps({**record, "item": token}, ensure_ascii=False))
                emitted += 1
            actions.append(
                f"split one {len(tokens)}-id pass record (stage {record.get('stage')}) "
                f"into {emitted} per-item record(s)"
            )
        if split_any:
            atomic.write_text(path, "\n".join(out))
        return MigrationReport(actions=actions, skipped=skipped)


def _blob_record(line: str) -> dict | None:
    """The parsed record when its item field holds whitespace, else None.

    None answers for every line the rewrite must carry verbatim: blanks,
    lines that do not parse, records of any other shape, and clean
    single-id records. A legal id never holds whitespace, so the field is
    the whole fingerprint.
    """
    if not line.strip():
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    item = record.get("item")
    if not isinstance(item, str) or not any(map(str.isspace, item)):
        return None
    return record


def _live_owner(token: str, live: set[str]) -> str | None:
    """The live item claiming ``token``, by the pass readers' own rule.

    An exact live id answers for itself; otherwise the unique live item
    carrying the token's trailing shortid — a rename keeps the shortid and
    rewrites the slug. No match, or two, claims nothing.
    """
    if token in live:
        return token
    shortid = token.rsplit("-", 1)[-1]
    matches = [item for item in live if item.rsplit("-", 1)[-1] == shortid]
    return matches[0] if len(matches) == 1 else None
