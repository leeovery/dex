"""Migration 5 — type the ledger's cap fires.

A cap-fire marker used to carry `capped: true`: a fire happened, but not
which bound refused the work. The bound lived only in the line's stated
`reason`, free text written for the audit trail, and the health check
aggregated on that text — so one bound worded two ways read as two bounds,
and an owner-requested `enrich fetch` refusal, worded a third way, read as
harvest drift. The bound is now typed: `cap` holds `depth`, `url`, or
`url-requested`, and no reader touches the prose.

The rewrite is per line: `capped` goes, `cap` arrives, everything else is
re-emitted through the serialization boundary unchanged. The bound comes
from the line's own reason, which the engine wrote from a closed
vocabulary — three strings, two bounds, one of them the owner's own
request. This is the one place that prose is read, and it is read once.

A line whose reason matches nothing known keeps its fire but not its
bound: it is skipped-with-why and left verbatim, because guessing a bound
would put a made-up reading in front of the owner. It stays unreadable to
the current schema, and every later command says so with migration 5
named — which is the honest state for a line nobody can classify.

Idempotent: a second apply finds no `capped` key anywhere.
"""

import datetime
import json
from collections.abc import Callable
from pathlib import Path

from dex_engine import atomic
from dex_engine.pipeline.ledger import LedgerSchemaError, from_line, to_line
from dex_engine.pipeline.types import Cap, MigrationReport, Skipped

__all__ = ["TypedCapFires", "build", "cap_from_reason"]

NUMBER = 5
INTENT = (
    "type the ledger's cap fires: `capped: true` becomes `cap: depth|url|url-requested`, "
    "so the health check counts a bound by the bound and never by the prose that "
    "happened to word it"
)

# The engine's own wording, closed and historic: two harvest-time bounds
# and the owner-requested refusal, which named the URL bound and offered
# --force. Matched on the stem so the --force tail decides the last one.
_DEPTH = "depth cap ("
_URL = "url cap ("
_REQUESTED_TAIL = "--force"


def cap_from_reason(reason: str) -> Cap | None:
    """The bound a pre-typing cap fire named in its stated reason, or None.

    Shared with migration 1: an instance that has not run migration 1 yet
    holds pre-rewrite lines with the same `capped: true` shape, and migration
    1 rewrites every line it meets before this migration ever sees it. One
    rule, stated once, so the two migrations cannot classify the same line
    differently.
    """
    if reason.startswith(_DEPTH):
        return Cap.DEPTH
    if reason.startswith(_URL):
        return Cap.URL_REQUESTED if _REQUESTED_TAIL in reason else Cap.URL
    return None


def build(
    *,
    today: Callable[[], datetime.date],  # noqa: ARG001 — the shared build signature; this writes no dated state
    now: Callable[[], datetime.datetime],  # noqa: ARG001 — and appends no ledger line
    engine_version: str,  # noqa: ARG001 — lines are rewritten in place, never re-stamped
) -> "TypedCapFires":
    """Build migration 5 (the shared build signature; this migration only rewrites)."""
    return TypedCapFires()


class TypedCapFires:
    """Migration 5: see the module docstring."""

    number = NUMBER
    intent = INTENT

    def apply(self, root: Path) -> MigrationReport:
        """Rewrite every `capped` ledger line to carry its typed bound.

        Args:
            root: The instance root.

        Returns:
            The report: how many lines were typed, under which bounds, and a
            skip for any line whose bound cannot be established.
        """
        actions: list[str] = []
        skipped: list[Skipped] = []
        path = root / "state" / "enrichment-ledger.jsonl"
        if not path.exists():
            return MigrationReport(actions=actions, skipped=skipped, anomalies=[])
        lines = [
            line for line in path.read_text(encoding="utf-8").split("\n") if line.strip()
        ]
        typed: dict[Cap, int] = {}
        rewritten: list[str] = []
        changed = False
        for lineno, line in enumerate(lines, start=1):
            new_line, cap = _translate(line, lineno, skipped)
            rewritten.append(new_line)
            changed = changed or new_line != line
            if cap is not None:
                typed[cap] = typed.get(cap, 0) + 1
        if not changed:
            return MigrationReport(actions=actions, skipped=skipped, anomalies=[])
        # Atomic: a crash mid-write must never lose the ledger.
        atomic.write_text(path, "".join(line + "\n" for line in rewritten))
        _report(typed, actions)
        return MigrationReport(actions=actions, skipped=skipped, anomalies=[])


def _translate(line: str, lineno: int, skipped: list[Skipped]) -> tuple[str, Cap | None]:
    """One line: its rewritten form and the bound it was typed with.

    Read raw rather than through ``from_line``: a pre-typing line does not
    conform to the current schema by definition, and every line that is not
    one of these must survive byte for byte.
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return line, None
    if not isinstance(raw, dict) or "capped" not in raw:
        return line, None
    fired = raw.pop("capped")
    if not fired:
        # `capped: false` said nothing; the schema drops the field entirely.
        return json.dumps(raw, ensure_ascii=False), None
    cap = cap_from_reason(str(raw.get("reason") or ""))
    if cap is None:
        skipped.append(
            Skipped(
                what=f"ledger line {lineno}",
                why=f"a cap fire whose stated reason names no bound this migration "
                f"knows ({raw.get('reason')!r}) — left as it is rather than typed with "
                "a guess; the line stays nonconforming until it is repaired by hand",
            )
        )
        return line, None
    raw["cap"] = cap.value
    try:
        return to_line(from_line(json.dumps(raw, ensure_ascii=False))), cap
    except LedgerSchemaError as e:
        skipped.append(
            Skipped(
                what=f"ledger line {lineno}",
                why=f"a cap fire that does not conform even with its bound typed ({e}) — "
                "left as it is; repair it per migration 1's report",
            )
        )
        return line, None


def _report(typed: dict[Cap, int], actions: list[str]) -> None:
    total = sum(typed.values())
    if not total:
        actions.append("ledger: dropped a `capped: false` field that recorded nothing")
        return
    spread = ", ".join(f"{cap.value}: {count}" for cap, count in sorted(typed.items()))
    actions.append(
        f"ledger: {total} cap fire{'' if total == 1 else 's'} typed with the bound that "
        f"refused the work ({spread}) — the health check counts bounds now, not the "
        "prose that worded them"
    )
