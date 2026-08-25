"""Named render surfaces (§11): loud payload validation, layout via the kernel.

Two call paths feed these: engine-internal (a command renders its own report
in-process) and cognitive (Claude writes a JSON payload to ``cache/`` and
runs ``bin/dex render --file …`` — that CLI ships in a later phase). The
standing skill rule: never hand-draw a table or report; there is a surface
for it — call it.

Payload shapes are documented on each renderer. Validation is loud: a
missing, unknown, or mistyped key raises :class:`PayloadError` naming the
surface and the key — never a half-rendered report.
"""

from collections.abc import Callable, Mapping
from typing import NoReturn

from dex_engine.pipeline.types import Need, Status

from . import kernel

__all__ = ["SURFACES", "PayloadError", "render"]


class PayloadError(ValueError):
    """A surface payload does not conform to that surface's shape."""


# Statuses an entry may hold when it survives a session, parked (§1).
_PARKED_STATUSES = frozenset({Status.WAITING, Status.BLOCKED, Status.ERROR, Status.MANUAL})
_PROVIDER_STATES = frozenset({"active", "available", "unavailable"})


def render(surface: str, payload: Mapping[str, object]) -> str:
    """Render ``payload`` on the named surface.

    Args:
        surface: A key of :data:`SURFACES`.
        payload: The surface's payload (typically parsed JSON).

    Returns:
        The rendered report, newline-terminated.

    Raises:
        PayloadError: Unknown surface, or a nonconforming payload.
        NotImplementedError: The surface's payload shape has not firmed up
            yet (later-phase stubs).
    """
    if surface not in SURFACES:
        known = ", ".join(sorted(SURFACES))
        raise PayloadError(f"unknown surface {surface!r} — known surfaces: {known}")
    return SURFACES[surface](payload)


# ---------------------------------------------------------------------------
# Validation helpers. `where` locates the offending key ("parked[2].") so the
# error names the exact spot.
# ---------------------------------------------------------------------------


def _fail(surface: str, message: str) -> NoReturn:
    raise PayloadError(f"{surface}: {message}")


def _check_keys(
    surface: str,
    payload: Mapping[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    where: str = "",
) -> None:
    if not isinstance(payload, Mapping):
        _fail(surface, f"{where}payload must be an object, got {type(payload).__name__}")
    missing = sorted(required - set(payload))
    if missing:
        _fail(surface, f"{where}missing required key(s) {missing}")
    unknown = sorted(set(payload) - required - optional)
    if unknown:
        _fail(surface, f"{where}unknown key(s) {unknown}")


def _str_at(surface: str, obj: Mapping[str, object], key: str, where: str = "") -> str:
    if key not in obj:
        _fail(surface, f"{where}{key} is required")
    value = obj[key]
    if not isinstance(value, str) or not value:
        _fail(surface, f"{where}{key} must be a non-empty string, got {value!r}")
    if "\n" in value:
        # Validated here so the kernel's single-line ValueErrors can never
        # escape a surface as anything but a PayloadError.
        _fail(surface, f"{where}{key} must be single-line, got {value!r}")
    return value


def _int_at(
    surface: str,
    obj: Mapping[str, object],
    key: str,
    where: str = "",
    *,
    default: int | None = None,
) -> int:
    if key not in obj:
        if default is None:
            _fail(surface, f"{where}{key} is required")
        return default
    value = obj[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _fail(surface, f"{where}{key} must be a non-negative integer, got {value!r}")
    return value


def _str_list_at(surface: str, obj: Mapping[str, object], key: str, where: str = "") -> list[str]:
    value = obj.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        _fail(surface, f"{where}{key} must be a list of strings, got {value!r}")
    for i, element in enumerate(value):
        if not element or "\n" in element:
            _fail(
                surface,
                f"{where}{key}[{i}] must be a non-empty single-line string, got {element!r}",
            )
    return value


def _obj_list_at(
    surface: str, obj: Mapping[str, object], key: str, *, required: bool
) -> list[Mapping[str, object]]:
    if key not in obj:
        if required:
            _fail(surface, f"{key} is required")
        return []
    value = obj[key]
    if not isinstance(value, list):
        _fail(surface, f"{key} must be a list, got {type(value).__name__}")
    out: list[Mapping[str, object]] = []
    for i, element in enumerate(value):
        if not isinstance(element, Mapping):
            _fail(surface, f"{key}[{i}] must be an object, got {type(element).__name__}")
        out.append(element)
    return out


def _status_at(surface: str, obj: Mapping[str, object], key: str, where: str = "") -> Status:
    text = _str_at(surface, obj, key, where)
    try:
        return Status(text)
    except ValueError:
        options = ", ".join(s.value for s in Status)
        _fail(surface, f"{where}{key} must be one of {options}, got {text!r}")


def _counts_at(surface: str, payload: Mapping[str, object], key: str) -> dict[Status, int]:
    if key not in payload:
        _fail(surface, f"{key} is required")
    value = payload[key]
    if not isinstance(value, Mapping):
        _fail(surface, f"{key} must be an object of status -> count")
    counts: dict[Status, int] = {}
    for raw_status, raw_count in value.items():
        if not isinstance(raw_status, str):
            _fail(surface, f"{key} keys must be status strings, got {raw_status!r}")
        try:
            status = Status(raw_status)
        except ValueError:
            options = ", ".join(s.value for s in Status)
            _fail(surface, f"{key} key must be one of {options}, got {raw_status!r}")
        if not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count < 0:
            _fail(surface, f"{key}[{raw_status!r}] must be a non-negative integer")
        counts[status] = raw_count
    return counts


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural or singular + "s")
    return f"{count} {word}"


def _counts_table(counts: dict[Status, int]) -> str:
    ordered = [s for s in Status if s in counts]
    rows = [[status.value, str(counts[status])] for status in ordered]
    return kernel.table(rows, aligns=["l", "r"], indent=2)


def _bullets(texts: list[str], *, indent: int = 2) -> list[str]:
    lines: list[str] = []
    for text in texts:
        lines.extend(kernel.wrap_with_prefix("- " + text, prefix=" " * indent, hang=2))
    return lines


# ---------------------------------------------------------------------------
# enrich-report — the run report (§1, §11)
# ---------------------------------------------------------------------------


def _render_enrich_report(payload: Mapping[str, object]) -> str:
    """Render the ``enrich run`` report.

    Payload::

        {
          "counts": {"<status>": int, ...},          # units written this run
          "items": [{"id": str, "reason": str}],     # cognitive work list:
                                                     #   fresh / rerun / waiting-drained
          "parked": [{"item": str, "url": str,
                      "status": str, "reason": str}],  # survives the session
          "cognitive": [{"item": str, "url": str,
                         "need": str}],              # optional: jobs for the session
          "issues_filed": int,                       # optional, default 0
          "notes": [str],                            # optional
        }
    """
    surface = "enrich-report"
    _check_keys(
        surface,
        payload,
        required=frozenset({"counts", "items", "parked"}),
        optional=frozenset({"cognitive", "issues_filed", "notes"}),
    )
    counts = _counts_at(surface, payload, "counts")
    item_rows = _enrich_item_rows(surface, payload)
    parked_rows = _enrich_parked_rows(surface, payload)
    cognitive_rows = _enrich_cognitive_rows(surface, payload)
    issues_filed = _int_at(surface, payload, "issues_filed", default=0)
    notes = _str_list_at(surface, payload, "notes")

    total = sum(counts.values())
    lines = [f"enrich run — {_plural(total, 'unit')} processed"]
    if counts:
        lines.append(_counts_table(counts).rstrip("\n"))
    lines.append("")
    if item_rows:
        lines.append(
            f"cognitive work — {_plural(len(item_rows), 'item')} with new or changed content:"
        )
        lines.append(kernel.table(item_rows, indent=2).rstrip("\n"))
    else:
        lines.append("cognitive work — none (nothing new or changed)")
    lines.append("")
    if parked_rows:
        verb = "survives" if len(parked_rows) == 1 else "survive"
        lines.append(
            f"parked — {_plural(len(parked_rows), 'entry', 'entries')} {verb} "
            "this session, each for a stated reason:"
        )
        lines.append(kernel.table(parked_rows, indent=2).rstrip("\n"))
    else:
        lines.append("parked — none")
    if cognitive_rows:
        lines.append("")
        lines.append("cognitive jobs — the session completes these with eyes:")
        lines.append(kernel.table(cognitive_rows, indent=2).rstrip("\n"))
    if issues_filed:
        lines.append("")
        lines.append(f"reported upstream: {_plural(issues_filed, 'issue')}")
    if notes:
        lines.append("")
        lines.append("notes:")
        lines.extend(_bullets(notes))
    return "\n".join(lines) + "\n"


def _enrich_item_rows(surface: str, payload: Mapping[str, object]) -> list[list[str]]:
    rows = []
    for i, entry in enumerate(_obj_list_at(surface, payload, "items", required=True)):
        where = f"items[{i}]."
        _check_keys(surface, entry, required=frozenset({"id", "reason"}), where=where)
        rows.append(
            [_str_at(surface, entry, "id", where), _str_at(surface, entry, "reason", where)]
        )
    return rows


def _enrich_parked_rows(surface: str, payload: Mapping[str, object]) -> list[list[str]]:
    rows = []
    for i, entry in enumerate(_obj_list_at(surface, payload, "parked", required=True)):
        where = f"parked[{i}]."
        _check_keys(
            surface, entry, required=frozenset({"item", "url", "status", "reason"}), where=where
        )
        status = _status_at(surface, entry, "status", where)
        if status not in _PARKED_STATUSES:
            allowed = ", ".join(sorted(s.value for s in _PARKED_STATUSES))
            _fail(
                surface,
                f"{where}status must be a parked status ({allowed}), got {status.value!r}",
            )
        rows.append(
            [
                _str_at(surface, entry, "item", where),
                status.value,
                _str_at(surface, entry, "reason", where),
                _str_at(surface, entry, "url", where),
            ]
        )
    return rows


def _enrich_cognitive_rows(surface: str, payload: Mapping[str, object]) -> list[list[str]]:
    rows = []
    for i, entry in enumerate(_obj_list_at(surface, payload, "cognitive", required=False)):
        where = f"cognitive[{i}]."
        _check_keys(surface, entry, required=frozenset({"item", "url", "need"}), where=where)
        need = _str_at(surface, entry, "need", where)
        if need not in {n.value for n in Need}:
            options = ", ".join(n.value for n in Need)
            _fail(surface, f"{where}need must be one of {options}, got {need!r}")
        rows.append(
            [_str_at(surface, entry, "item", where), need, _str_at(surface, entry, "url", where)]
        )
    return rows


# ---------------------------------------------------------------------------
# status — ledger summary (`enrich status`)
# ---------------------------------------------------------------------------


def _render_status(payload: Mapping[str, object]) -> str:
    """Render the ledger status summary.

    Payload::

        {
          "counts": {"<status>": int, ...},   # whole-ledger, per status
          "waiting": {"<need>": int, ...},    # optional: waiting cohort by need
          "orphans": [str],                   # optional: item ids whose
                                              #   enrichment is newer than their
                                              #   digest (interrupted-session
                                              #   backstop, §1)
        }
    """
    surface = "status"
    _check_keys(
        surface,
        payload,
        required=frozenset({"counts"}),
        optional=frozenset({"waiting", "orphans"}),
    )
    counts = _counts_at(surface, payload, "counts")
    orphans = _str_list_at(surface, payload, "orphans")
    waiting_raw = payload.get("waiting", {})
    if not isinstance(waiting_raw, Mapping):
        _fail(surface, "waiting must be an object of need -> count")
    waiting: list[tuple[str, int]] = []
    for raw_need, raw_count in waiting_raw.items():
        if not isinstance(raw_need, str) or raw_need not in {n.value for n in Need}:
            options = ", ".join(n.value for n in Need)
            _fail(surface, f"waiting key must be one of {options}, got {raw_need!r}")
        if not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count < 0:
            _fail(surface, f"waiting[{raw_need!r}] must be a non-negative integer")
        waiting.append((raw_need, raw_count))

    total = sum(counts.values())
    lines = [f"ledger — {_plural(total, 'entry', 'entries')}"]
    if counts:
        lines.append(_counts_table(counts).rstrip("\n"))
    if waiting:
        lines.append("")
        lines.append("waiting on capabilities:")
        rows = [[need, str(count)] for need, count in sorted(waiting)]
        lines.append(kernel.table(rows, aligns=["l", "r"], indent=2).rstrip("\n"))
    if orphans:
        lines.append("")
        lines.append("enrichment newer than digest — interrupted-session backstop, digest these:")
        nodes = [kernel.TreeNode(title=item_id) for item_id in orphans]
        lines.append(kernel.tree(nodes, indent=2).rstrip("\n"))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# capability-report (§6)
# ---------------------------------------------------------------------------


def _render_capability_report(payload: Mapping[str, object]) -> str:
    """Render the capability report: each capability, its providers, upgrades.

    Payload::

        {
          "capabilities": [
            {"name": str,                       # a Need value
             "providers": [
               {"name": str,
                "state": "active" | "available" | "unavailable",
                "note": str}]}                  # note optional; what a dormant
          ]                                     #   provider would need
        }
    """
    surface = "capability-report"
    _check_keys(surface, payload, required=frozenset({"capabilities"}))
    capabilities = _obj_list_at(surface, payload, "capabilities", required=True)
    if not capabilities:
        _fail(surface, "capabilities must name at least one capability")
    pairs: list[tuple[str, str]] = []
    for i, capability in enumerate(capabilities):
        where = f"capabilities[{i}]."
        _check_keys(surface, capability, required=frozenset({"name", "providers"}), where=where)
        name = _str_at(surface, capability, "name", where)
        if name not in {n.value for n in Need}:
            options = ", ".join(n.value for n in Need)
            _fail(surface, f"{where}name must be one of {options}, got {name!r}")
        providers = _obj_list_at(surface, capability, "providers", required=True)
        parts: list[str] = []
        for j, provider in enumerate(providers):
            pwhere = f"{where}providers[{j}]."
            _check_keys(
                surface,
                provider,
                required=frozenset({"name", "state"}),
                optional=frozenset({"note"}),
                where=pwhere,
            )
            pname = _str_at(surface, provider, "name", pwhere)
            state = _str_at(surface, provider, "state", pwhere)
            if state not in _PROVIDER_STATES:
                options = ", ".join(sorted(_PROVIDER_STATES))
                _fail(surface, f"{pwhere}state must be one of {options}, got {state!r}")
            note = _str_at(surface, provider, "note", pwhere) if "note" in provider else ""
            if state == "active":
                parts.append(f"{pname} (active)")
            elif note:
                parts.append(f"{pname} {state} — {note}")
            else:
                parts.append(f"{pname} {state}")
        pairs.append((name, " · ".join(parts) if parts else "no providers"))
    return "capabilities\n" + kernel.kv_block(pairs, indent=2)


# ---------------------------------------------------------------------------
# sync-report (§12)
# ---------------------------------------------------------------------------


def _render_sync_report(payload: Mapping[str, object]) -> str:
    """Render the sync report: pin state, migrations applied, skills synced.

    Payload::

        {
          "pin": str,                 # the tag now pinned
          "previous": str,            # optional: prior pin (absent = no bump)
          "migrations": [             # applied this sync, may be empty
            {"number": int, "intent": str,
             "actions": [str],                     # optional
             "skipped": [{"what": str, "why": str}],  # optional
             "anomalies": [str]}                   # optional
          ],
          "skills_synced": int,
          "notes": [str],             # optional
        }

    ``skipped`` and ``anomalies`` are repaired with judgment in-session
    (§12) — they render prominently, never summarized away.
    """
    surface = "sync-report"
    _check_keys(
        surface,
        payload,
        required=frozenset({"pin", "migrations", "skills_synced"}),
        optional=frozenset({"previous", "notes"}),
    )
    pin = _str_at(surface, payload, "pin")
    previous = _str_at(surface, payload, "previous") if "previous" in payload else None
    migrations = _obj_list_at(surface, payload, "migrations", required=True)
    skills_synced = _int_at(surface, payload, "skills_synced")
    notes = _str_list_at(surface, payload, "notes")

    if previous is not None and previous != pin:
        lines = [f"sync — pin bumped {previous} → {pin}"]
    else:
        lines = [f"sync — engine pinned at {pin}"]
    if migrations:
        lines.append(f"migrations applied — {len(migrations)}:")
        for i, migration in enumerate(migrations):
            where = f"migrations[{i}]."
            _check_keys(
                surface,
                migration,
                required=frozenset({"number", "intent"}),
                optional=frozenset({"actions", "skipped", "anomalies"}),
                where=where,
            )
            number = _int_at(surface, migration, "number", where)
            intent = _str_at(surface, migration, "intent", where)
            actions = _str_list_at(surface, migration, "actions", where)
            anomalies = _str_list_at(surface, migration, "anomalies", where)
            skipped_entries = _obj_list_at(surface, migration, "skipped", required=False)
            skipped: list[str] = []
            for j, entry in enumerate(skipped_entries):
                swhere = f"{where}skipped[{j}]."
                _check_keys(surface, entry, required=frozenset({"what", "why"}), where=swhere)
                skipped.append(
                    f"{_str_at(surface, entry, 'what', swhere)} — "
                    f"{_str_at(surface, entry, 'why', swhere)}"
                )
            lines.extend(
                kernel.wrap_with_prefix(f"migration {number} — {intent}", prefix="  ", hang=2)
            )
            if actions:
                lines.append(f"    actions — {len(actions)}:")
                lines.extend(_bullets(actions, indent=6))
            else:
                lines.append("    actions — none")
            if skipped:
                lines.append(f"    skipped — {len(skipped)} (repair with judgment):")
                lines.extend(_bullets(skipped, indent=6))
            if anomalies:
                lines.append(f"    anomalies — {len(anomalies)} (REVIEW REQUIRED):")
                lines.extend(_bullets(anomalies, indent=6))
    else:
        lines.append("migrations applied — none (state already current)")
    lines.append(f"skills synced: {skills_synced}")
    if notes:
        lines.append("notes:")
        lines.extend(_bullets(notes))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Later-phase stubs — payload shapes firm up with the features they report on.
# ---------------------------------------------------------------------------


def _render_ingest_receipt(_payload: Mapping[str, object]) -> str:
    """Stub: the per-capture receipt ships with the skill rewrites."""
    raise NotImplementedError(
        "surface 'ingest-receipt' is not implemented yet — its payload shape firms "
        "up in a later phase (design §15a); do not hand-draw a receipt in the meantime"
    )


def _render_health_report(_payload: Mapping[str, object]) -> str:
    """Stub: the health report ships with the lint additions."""
    raise NotImplementedError(
        "surface 'health-report' is not implemented yet — its payload shape firms "
        "up in a later phase (design §15a); do not hand-draw a report in the meantime"
    )


# The typed registry literal is the conformance point (§2): every renderer is
# checked against the surface signature at this one assignment.
SURFACES: dict[str, Callable[[Mapping[str, object]], str]] = {
    "enrich-report": _render_enrich_report,
    "status": _render_status,
    "capability-report": _render_capability_report,
    "sync-report": _render_sync_report,
    "ingest-receipt": _render_ingest_receipt,
    "health-report": _render_health_report,
}
