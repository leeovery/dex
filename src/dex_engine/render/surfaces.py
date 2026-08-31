"""Named render surfaces: loud payload validation, markdown via the kernel.

Two call paths feed these: engine-internal (a command renders its own report
in-process) and cognitive (Claude writes a JSON payload to ``cache/`` and
runs ``bin/dex render --file …`` — the CLI in ``render/cli.py``). The
standing skill rule: never hand-draw a report; there is a surface for it —
call it.

Reports are markdown. Sections group by who owns the next action, not by the
ledger status the engine happens to store, and an item id, a URL or a path
reaches the output whole — never shortened, never split across lines.

Payload shapes are documented on each renderer. Validation is loud: a
missing, unknown, or mistyped key raises :class:`PayloadError` naming the
surface and the key — never a half-rendered report.
"""

from collections.abc import Callable, Mapping
from typing import NoReturn, TypeVar

from dex_engine.pipeline.types import Need, Status

from . import kernel

__all__ = ["SURFACES", "PayloadError", "render"]


class PayloadError(ValueError):
    """A surface payload does not conform to that surface's shape."""


# Statuses an entry may hold when it survives a session, parked.
_PARKED_STATUSES = frozenset({Status.WAITING, Status.BLOCKED, Status.ERROR, Status.MANUAL})
# The split that decides which section a parked entry lands in: `manual` is
# the engine saying it has given up, and only a person moves it. Everything
# else re-enters the queue unasked — blocked next run, waiting when a
# provider appears, error when a newer engine ships.
_OWNER_IS_YOU = frozenset({Status.MANUAL})
_PROVIDER_STATES = frozenset({"active", "available", "unavailable"})


def render(surface: str, payload: Mapping[str, object]) -> str:
    """Render ``payload`` on the named surface.

    Args:
        surface: A key of :data:`SURFACES`.
        payload: The surface's payload (typically parsed JSON).

    Returns:
        The rendered markdown report, newline-terminated.

    Raises:
        PayloadError: Unknown surface, or a nonconforming payload.
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


def _needs_counts(surface: str, payload: Mapping[str, object], key: str) -> list[tuple[str, int]]:
    raw = payload.get(key, {})
    if not isinstance(raw, Mapping):
        _fail(surface, f"{key} must be an object of need -> count")
    counts: list[tuple[str, int]] = []
    for raw_need, raw_count in raw.items():
        if not isinstance(raw_need, str) or raw_need not in {n.value for n in Need}:
            options = ", ".join(n.value for n in Need)
            _fail(surface, f"{key} key must be one of {options}, got {raw_need!r}")
        if not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count < 0:
            _fail(surface, f"{key}[{raw_need!r}] must be a non-negative integer")
        counts.append((raw_need, raw_count))
    return counts


def _need_at(surface: str, obj: Mapping[str, object], key: str, where: str = "") -> str:
    need = _str_at(surface, obj, key, where)
    if need not in {n.value for n in Need}:
        options = ", ".join(n.value for n in Need)
        _fail(surface, f"{where}{key} must be one of {options}, got {need!r}")
    return need


# ---------------------------------------------------------------------------
# Shared markdown shapes.
# ---------------------------------------------------------------------------


def _counts_line(counts: dict[Status, int]) -> str:
    """The status counts as one line — the stand-in for a table of counts."""
    return kernel.inline(
        f"{kernel.bold(status.value)} {counts[status]}" for status in Status if status in counts
    )


def _entry(identity: str, *tags: str) -> str:
    """One bullet: the identity, whole and bold, then its short tags."""
    return kernel.bullet(kernel.inline([kernel.bold(identity), *(tag for tag in tags if tag)]))


def _agree(count: int, singular: str, plural_form: str) -> str:
    return singular if count == 1 else plural_form


def _note_section(title: str, notes: list[str]) -> list[str]:
    if not notes:
        return []
    scaled = f"{title} — {kernel.plural(len(notes), 'note')}"
    return ["", kernel.heading(scaled, level=3), "", *(kernel.bullet(note) for note in notes)]


def _parked_rows(
    surface: str, payload: Mapping[str, object], *, required: bool
) -> list[dict[str, object]]:
    """The ``parked`` rows, checked — one shape, read by two surfaces.

    The run report says what its own run parked (always present, often
    empty); the standing view says what is parked now (present only when
    something is). Same rows either way, so same reader.
    """
    rows: list[dict[str, object]] = []
    for i, entry in enumerate(_obj_list_at(surface, payload, "parked", required=required)):
        where = f"parked[{i}]."
        _check_keys(
            surface,
            entry,
            required=frozenset({"item", "url", "status", "reason"}),
            optional=frozenset({"attempts", "attempt_cap", "resting", "drainable", "cognitive"}),
            where=where,
        )
        status = _status_at(surface, entry, "status", where)
        if status not in _PARKED_STATUSES:
            allowed = ", ".join(sorted(s.value for s in _PARKED_STATUSES))
            _fail(
                surface,
                f"{where}status must be a parked status ({allowed}), got {status.value!r}",
            )
        row: dict[str, object] = {
            "item": _str_at(surface, entry, "item", where),
            "url": _str_at(surface, entry, "url", where),
            "status": status,
            "reason": _str_at(surface, entry, "reason", where),
        }
        if "attempts" in entry:
            row["attempts"] = _int_at(surface, entry, "attempts", where)
        if "attempt_cap" in entry:
            if "attempts" not in entry:
                _fail(surface, f"{where}attempt_cap without attempts says nothing")
            row["attempt_cap"] = _int_at(surface, entry, "attempt_cap", where)
        for key in _PARKED_MARKERS:
            if _marker_at(surface, entry, key, where):
                row[key] = True
        if "drainable" in row and "cognitive" in row:
            _fail(
                surface,
                f"{where}drainable and cognitive are exclusive — a unit whose "
                "capability falls to the cognitive floor has no active provider",
            )
        rows.append(row)
    return rows


# A parked row's classification markers, each present-means-true.
_PARKED_MARKERS = ("resting", "drainable", "cognitive")


def _marker_at(surface: str, entry: Mapping[str, object], key: str, where: str) -> bool:
    """One marker's value: absent is false, present is true and nothing else.

    ``false`` is refused rather than read: a marker is the builder stating
    a classification it made, so the absence of the statement is the only
    way to say it did not.
    """
    if key not in entry:
        return False
    if entry[key] is not True:
        _fail(surface, f"{where}{key} can only be true — omit the key otherwise")
    return True


# What the engine will do about a parked entry unasked. `blocked` is absent
# because its entries carry the concrete attempt count instead, and `manual`
# because it is the one status where the answer is "nothing". Both waiting
# marks override this note, in opposite directions: `drainable` because the
# provider it waits on is active, and `cognitive` because no provider will
# ever appear. Unmarked, the note is left saying what is true of the one
# waiting row that neither describes — a transcribe park, which has no
# cognitive floor and does wait for a mechanical provider.
_RETRY_NOTE = {
    Status.WAITING: "retries when a provider appears",
    Status.ERROR: "retries on the next engine release",
}

_DRAINABLE_NOTE = "a provider is active — drains on the next run"

_COGNITIVE_NOTE = "no provider will appear — you read this one"


def _owner_is_you(row: dict[str, object]) -> bool:
    """Whether the row's next action belongs to the reader, not the engine.

    ``manual`` always does; a ``resting`` row does too, whatever its
    status — the builder marked it because the engine will not touch it
    (a media unit deferred under ``media_fetch: none``), so only the
    owner's own change of config moves it. So does a ``cognitive`` one:
    its capability resolves to the floor, which is the reader's eyes, and
    filing it under the engine put it on the retry list of a report whose
    own cognitive section said the engine could not do it.
    """
    return row["status"] in _OWNER_IS_YOU or bool(row.get("resting") or row.get("cognitive"))


def _parked_section(parked: list[dict[str, object]], *, mine: bool) -> str:
    """One parked section: the entries whose next action belongs to one owner."""
    rows = [row for row in parked if _owner_is_you(row) == mine]
    if not rows:
        return ""
    if mine:
        # Not "given up on": a resting row is here because the owner's
        # config paused it, not because the engine surrendered. What is
        # true of every row in this section is the ownership itself.
        title = f"Needs you — {kernel.plural(len(rows), 'entry', 'entries')} only you can move"
    else:
        # "retries" never agrees with the count: the subject of the clause
        # is the engine, one of it however many entries it holds.
        title = (
            f"Waiting on the engine — {kernel.plural(len(rows), 'entry', 'entries')} "
            "it retries by itself"
        )
    lines = [kernel.heading(title, level=3), ""]
    for row in rows:
        lines += [
            _entry(str(row["item"]), *_parked_tags(row)),
            kernel.detail(str(row["reason"])),
            kernel.detail(str(row["url"])),
        ]
    return "\n".join(lines)


def _parked_tags(row: dict[str, object]) -> list[str]:
    status = Status(str(row["status"]))
    tags = [kernel.code(status.value)]
    if row.get("resting"):
        # No retry note and no attempt framing: nothing here retries, and
        # the row's reason names what unblocks it.
        return tags
    if row.get("cognitive"):
        # Attempt framing is as wrong here as retry framing: the engine
        # makes no attempt at a job it hands to the reader.
        tags.append(_COGNITIVE_NOTE)
        return tags
    attempts = row.get("attempts")
    if isinstance(attempts, int):
        cap = row.get("attempt_cap")
        tags.append(
            f"attempt {attempts} of {cap}" if isinstance(cap, int) else f"attempt {attempts}"
        )
    elif row.get("drainable"):
        tags.append(_DRAINABLE_NOTE)
    elif status in _RETRY_NOTE:
        tags.append(_RETRY_NOTE[status])
    return tags


def _undescribed_media(surface: str, payload: Mapping[str, object]) -> str:
    """The describe queue: media on disk that no session has written up.

    One section on two surfaces — the run's own work list and the standing
    listing — because the answer to "what is still owed a description" is
    the same question asked at two moments, and two spellings of it would
    drift apart.

    The counts are all the row can honestly say: descriptions are counted
    against binaries, never matched to them, so this names the item and
    the shortfall and leaves the reader to find which file is missing one.
    """
    rows = []
    for i, entry in enumerate(_obj_list_at(surface, payload, "undescribed_media", required=False)):
        where = f"undescribed_media[{i}]."
        _check_keys(
            surface, entry, required=frozenset({"item", "binaries", "described"}), where=where
        )
        item = _str_at(surface, entry, "item", where)
        binaries = _int_at(surface, entry, "binaries", where)
        described = _int_at(surface, entry, "described", where)
        if described >= binaries:
            _fail(
                surface,
                f"{where}described ({described}) is not short of binaries ({binaries}) — "
                "only an item still owing a description belongs here",
            )
        shortfall = (
            f"{described} of {kernel.plural(binaries, 'media file')} described — view the "
            f"rest and write {kernel.code(f'enrichment/{item}/media-<n>.md')}"
        )
        rows.append((item, shortfall))
    if not rows:
        return ""
    lines = [
        kernel.heading(
            f"Describe these — {kernel.plural(len(rows), 'item')} "
            f"{_agree(len(rows), 'carries', 'carry')} media nobody has described",
            level=3,
        ),
        "",
    ]
    for item, shortfall in rows:
        lines += [_entry(item), kernel.detail(shortfall)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# enrich-report — the run report
# ---------------------------------------------------------------------------


def _render_enrich_report(payload: Mapping[str, object]) -> str:
    """Render the ``enrich run`` report.

    Payload::

        {
          "counts": {"<status>": int, ...},          # units written this run
          "items": [{"id": str, "reason": str}],     # items with new material
          "parked": [{"item": str, "url": str,       # survives the session
                      "status": str, "reason": str,
                      "attempts": int,               # optional: retry state
                      "attempt_cap": int,            #   (blocked entries)
                      "drainable": true,             # optional: waiting on an
                                                     #   active provider
                      "cognitive": true,             # optional: waiting on the
                                                     #   cognitive floor
                      "resting": true}],             # optional: the engine will
                                                     #   not act; the owner will
          "incomplete": [{"item": str,               # optional: touched items
                          "landed": int,             #   still owed work — the
                          "total": int,              #   ledger units that have
                          "outstanding": [           #   landed, of all it owns
                            {"status": str,          #   an outstanding status
                             "needs": str,           #   optional
                             "count": int}]}],
          "cognitive": [{"item": str, "url": str,
                         "need": str}],              # optional: jobs for the session
          "undescribed_media": [                     # optional: items carrying
            {"item": str,                            #   media no session has
             "binaries": int,                        #   described yet
             "described": int}],
          "unchanged": [{"id": str,                  # optional: units re-fetched
                         "count": int}],             #   to what was already stored
          "issues_filed": int,                       # optional, default 0
          "notes": [str],                            # optional
        }

    ``parked`` arrives as one list and renders as two sections, split on who
    owns the next action: ``manual`` is the engine giving up, everything
    else re-enters the queue by itself — except a row marked ``resting``
    or ``cognitive``, which the builder classified as the owner's because
    the engine will not act on it.
    """
    surface = "enrich-report"
    _check_keys(
        surface,
        payload,
        required=frozenset({"counts", "items", "parked"}),
        optional=frozenset(
            {
                "incomplete",
                "cognitive",
                "undescribed_media",
                "unchanged",
                "issues_filed",
                "notes",
            }
        ),
    )
    counts = _counts_at(surface, payload, "counts")
    parked = _parked_rows(surface, payload, required=True)
    issues_filed = _int_at(surface, payload, "issues_filed", default=0)
    notes = _str_list_at(surface, payload, "notes")

    total = sum(counts.values())
    blocks = [kernel.heading(f"Enrich run — {kernel.plural(total, 'unit')} processed")]
    if counts:
        blocks += ["", _counts_line(counts)]

    sections = [
        _enrich_writeups(surface, payload),
        _enrich_cognitive(surface, payload),
        _undescribed_media(surface, payload),
        _parked_section(parked, mine=True),
        _parked_section(parked, mine=False),
        _enrich_incomplete(surface, payload),
        _enrich_unchanged(surface, payload),
    ]
    rendered = [section for section in sections if section]
    if rendered:
        for section in rendered:
            blocks += ["", section]
    else:
        blocks += [
            "",
            (
                "Nothing to report from this run: no new material, nothing newly "
                "parked, nothing left outstanding on the items it touched. What the "
                "instance is holding is `enrich status`."
            ),
        ]
    if issues_filed:
        blocks += [
            "",
            f"{kernel.bold('Reported upstream')} — {kernel.plural(issues_filed, 'issue')} filed",
        ]
    blocks += _note_section("Notes", notes)
    return kernel.document(blocks)


def _enrich_writeups(surface: str, payload: Mapping[str, object]) -> str:
    """Items the run gave new material — the session's writing-up queue."""
    rows = []
    for i, entry in enumerate(_obj_list_at(surface, payload, "items", required=True)):
        where = f"items[{i}]."
        _check_keys(surface, entry, required=frozenset({"id", "reason"}), where=where)
        rows.append(
            (_str_at(surface, entry, "id", where), _str_at(surface, entry, "reason", where))
        )
    if not rows:
        return ""
    lines = [
        kernel.heading(
            f"Needs writing up — {kernel.plural(len(rows), 'item')} "
            f"{_agree(len(rows), 'has', 'have')} new material",
            level=3,
        ),
        "",
    ]
    for item, reason in rows:
        lines += [_entry(item), kernel.detail(reason)]
    return "\n".join(lines)


def _enrich_unchanged(surface: str, payload: Mapping[str, object]) -> str:
    """Units re-fetched to exactly what was already stored.

    Not the writing-up queue — nothing here is new material — but not
    silence either: a run that processed units and then printed "nothing
    to report" read as though the fetch had done nothing, over content
    that was on disk the whole time. This section is what makes the
    nothing-to-report line honest when it does print.
    """
    rows = []
    for i, entry in enumerate(_obj_list_at(surface, payload, "unchanged", required=False)):
        where = f"unchanged[{i}]."
        _check_keys(surface, entry, required=frozenset({"id", "count"}), where=where)
        rows.append((_str_at(surface, entry, "id", where), _int_at(surface, entry, "count", where)))
    if not rows:
        return ""
    total = sum(count for _, count in rows)
    lines = [
        kernel.heading(
            f"Already stored — {kernel.plural(total, 'unit')} re-fetched to what was "
            f"on disk, across {kernel.plural(len(rows), 'item')}",
            level=3,
        ),
        "",
    ]
    for item, count in rows:
        lines += [_entry(item), kernel.detail(f"{kernel.plural(count, 'unit')} unchanged")]
    return "\n".join(lines)


def _enrich_cognitive(surface: str, payload: Mapping[str, object]) -> str:
    """Waiting jobs that resolve to the cognitive floor: nobody but the reader."""
    rows = _cognitive_rows(surface, payload)
    if not rows:
        return ""
    lines = [
        kernel.heading(
            f"Read these yourself — {kernel.plural(len(rows), 'job')} the engine cannot do",
            level=3,
        ),
        "",
    ]
    for item, need, url in rows:
        lines += [_entry(item, kernel.code(need)), kernel.detail(url)]
    return "\n".join(lines)


def _cognitive_rows(surface: str, payload: Mapping[str, object]) -> list[tuple[str, str, str]]:
    rows = []
    for i, entry in enumerate(_obj_list_at(surface, payload, "cognitive", required=False)):
        where = f"cognitive[{i}]."
        _check_keys(surface, entry, required=frozenset({"item", "url", "need"}), where=where)
        rows.append(
            (
                _str_at(surface, entry, "item", where),
                _need_at(surface, entry, "need", where),
                _str_at(surface, entry, "url", where),
            )
        )
    return rows


# What an outstanding unit is waiting for, as prose. Statuses that are not
# outstanding never reach this surface — the payload validation refuses them.
_OUTSTANDING_STATUSES = frozenset(_PARKED_STATUSES | {Status.QUEUED})
_NEED_NOUNS = {Need.TRANSCRIBE: "transcription", Need.EXTRACT: "extraction", Need.OCR: "OCR"}
_STATUS_PHRASES = {
    Status.QUEUED: "queued",
    Status.WAITING: "waiting",
    Status.BLOCKED: "blocked",
    Status.ERROR: "in error",
    Status.MANUAL: "needing a decision",
}


def _enrich_incomplete(surface: str, payload: Mapping[str, object]) -> str:
    """Items still raw: one outstanding unit holds the whole item out of digest."""
    rows = []
    for i, entry in enumerate(_obj_list_at(surface, payload, "incomplete", required=False)):
        where = f"incomplete[{i}]."
        _check_keys(
            surface,
            entry,
            required=frozenset({"item", "landed", "total", "outstanding"}),
            where=where,
        )
        landed = _int_at(surface, entry, "landed", where)
        total = _int_at(surface, entry, "total", where)
        if landed > total:
            _fail(surface, f"{where}landed ({landed}) exceeds total ({total})")
        outstanding = _enrich_outstanding(surface, entry, where)
        rows.append(
            (
                _str_at(surface, entry, "item", where),
                f"{landed} of {kernel.plural(total, 'unit')} landed — {outstanding}",
            )
        )
    if not rows:
        return ""
    lines = [
        kernel.heading(
            f"Not finished — {kernel.plural(len(rows), 'item')} "
            f"{_agree(len(rows), 'stays', 'stay')} raw until every unit lands",
            level=3,
        ),
        "",
    ]
    for item, shape in rows:
        lines += [_entry(item), kernel.detail(shape)]
    return "\n".join(lines)


def _enrich_outstanding(surface: str, entry: Mapping[str, object], where: str) -> str:
    parts = []
    for j, group in enumerate(_obj_list_at(surface, entry, "outstanding", required=True)):
        gwhere = f"{where}outstanding[{j}]."
        _check_keys(
            surface,
            group,
            required=frozenset({"status", "count"}),
            optional=frozenset({"needs"}),
            where=gwhere,
        )
        status = _status_at(surface, group, "status", gwhere)
        if status not in _OUTSTANDING_STATUSES:
            allowed = ", ".join(sorted(s.value for s in _OUTSTANDING_STATUSES))
            _fail(
                surface,
                f"{gwhere}status must be an outstanding status ({allowed}), got {status.value!r}",
            )
        phrase = _STATUS_PHRASES[status]
        if "needs" in group:
            phrase += f" on {_NEED_NOUNS[Need(_need_at(surface, group, 'needs', gwhere))]}"
        parts.append(f"{_int_at(surface, group, 'count', gwhere)} {phrase}")
    if not parts:
        _fail(surface, f"{where}outstanding must name at least one outstanding unit")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# status — ledger summary (`enrich status`)
# ---------------------------------------------------------------------------


def _render_status(payload: Mapping[str, object]) -> str:
    """Render the ledger status summary.

    The standing view of parked work lives here rather than on the run
    report: a unit parked months ago is untouched state, and a run report
    that re-listed it would name the same permanently-parked entry as work
    to do on every run forever — the very thing the digest backstop refuses
    to do. Counts alone said ``**manual** 1`` and left the id, the URL and
    the reason on no surface at all.

    Payload::

        {
          "counts": {"<status>": int, ...},   # whole-ledger, per status
          "waiting": {"<need>": int, ...},    # optional: waiting cohort by need
          "parked": [{"item": str, "url": str,  # optional: every unit parked
                      "status": str, "reason": str,   # now, whichever run
                      "attempts": int,               #   parked it
                      "attempt_cap": int,
                      "drainable": true,        # optional: waiting on an active
                                                #   provider
                      "cognitive": true,        # optional: waiting on the
                                                #   cognitive floor
                      "resting": true}],        # optional: the engine will not
                                                #   act on it; the owner will
          "undescribed_media": [              # optional: items carrying media
            {"item": str,                     #   no session has described —
             "binaries": int,                 #   the standing describe queue
             "described": int}],
          "orphans": [str],                   # optional: item ids whose
                                              #   enrichment is newer than their
                                              #   digest (interrupted-session
                                              #   backstop)
        }
    """
    surface = "status"
    _check_keys(
        surface,
        payload,
        required=frozenset({"counts"}),
        optional=frozenset({"waiting", "parked", "undescribed_media", "orphans"}),
    )
    counts = _counts_at(surface, payload, "counts")
    orphans = _str_list_at(surface, payload, "orphans")
    waiting = _needs_counts(surface, payload, "waiting")
    parked = _parked_rows(surface, payload, required=False)

    total = sum(counts.values())
    blocks = [kernel.heading(f"Ledger — {kernel.plural(total, 'entry', 'entries')}")]
    if counts:
        blocks += ["", _counts_line(counts)]
    if waiting:
        waiting_total = sum(count for _, count in waiting)
        blocks += [
            "",
            kernel.heading(
                f"Waiting on a capability — {kernel.plural(waiting_total, 'unit')}", level=3
            ),
            "",
        ]
        blocks += [
            kernel.bullet(f"{kernel.code(need)} — {count}") for need, count in sorted(waiting)
        ]
    for section in (
        _parked_section(parked, mine=True),
        _parked_section(parked, mine=False),
        _undescribed_media(surface, payload),
    ):
        if section:
            blocks += ["", section]
    if orphans:
        blocks += [
            "",
            kernel.heading(
                f"Digest these — {kernel.plural(len(orphans), 'item')} whose enrichment is "
                f"newer than {_agree(len(orphans), 'its', 'their')} digest",
                level=3,
            ),
            "",
        ]
        blocks += [_entry(item_id) for item_id in orphans]
    return kernel.document(blocks)


# ---------------------------------------------------------------------------
# item-status — one item's ledger view (`enrich status --item <id>`)
# ---------------------------------------------------------------------------


def _render_item_status(payload: Mapping[str, object]) -> str:
    """Render one item's ledger units: what was fetched, parked, and written.

    "What was fetched for this item" is a ledger query — promoted URLs live
    in the ledger only, never in corpus frontmatter, so this surface is
    where an item's full fetch history shows. The URL is the unit's
    identity, so it leads each entry and reaches the reader whole.

    Payload::

        {
          "item": str,
          "units": [
            {"url": str, "status": str,        # required per unit
             "job": str,                       # optional: media/asset work
             "via": str, "depth": int,         # optional provenance
             "needs": str,                     # optional: waiting capability
             "reason": str,                    # optional: parking reason
             "path": str}                      # optional: output written
          ],
        }
    """
    surface = "item-status"
    _check_keys(surface, payload, required=frozenset({"item", "units"}))
    item = _str_at(surface, payload, "item")
    units = _obj_list_at(surface, payload, "units", required=True)
    if not units:
        return kernel.document(
            [
                kernel.heading(f"Item {item} — no work units"),
                "",
                (
                    "No ledger work units: a no-source capture, or `enrich run` "
                    "has not seeded it yet."
                ),
            ]
        )
    blocks = [kernel.heading(f"Item {item} — {kernel.plural(len(units), 'unit')}"), ""]
    blocks += [
        _item_status_unit(surface, unit, where=f"units[{i}].") for i, unit in enumerate(units)
    ]
    return kernel.document(blocks)


def _item_status_unit(surface: str, unit: Mapping[str, object], *, where: str) -> str:
    _check_keys(
        surface,
        unit,
        required=frozenset({"url", "status"}),
        optional=frozenset({"job", "via", "depth", "needs", "reason", "path"}),
        where=where,
    )
    status = _status_at(surface, unit, "status", where)
    tags = [kernel.code(status.value)]
    if "needs" in unit:
        tags.append(f"needs {kernel.code(_need_at(surface, unit, 'needs', where))}")
    provenance = []
    if "job" in unit:
        provenance.append(f"job {_str_at(surface, unit, 'job', where)}")
    if "via" in unit:
        provenance.append(f"via {_str_at(surface, unit, 'via', where)}")
    if "depth" in unit:
        provenance.append(f"depth {_int_at(surface, unit, 'depth', where)}")
    if provenance:
        tags.append(", ".join(provenance))
    lines = [_entry(_str_at(surface, unit, "url", where), *tags)]
    if "reason" in unit:
        lines.append(kernel.detail(_str_at(surface, unit, "reason", where)))
    if "path" in unit:
        lines.append(kernel.detail(f"wrote {kernel.code(_str_at(surface, unit, 'path', where))}"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# capability-report
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
                "note": str}]}                  # note optional, on ANY state:
          ]                                     #   what a dormant provider
        }                                       #   would need, or an active
                                                #   one's caveat
    """
    surface = "capability-report"
    _check_keys(surface, payload, required=frozenset({"capabilities"}))
    capabilities = _obj_list_at(surface, payload, "capabilities", required=True)
    if not capabilities:
        _fail(surface, "capabilities must name at least one capability")
    scale = kernel.plural(len(capabilities), "capability", "capabilities")
    blocks = [kernel.heading(f"Capabilities — {scale}")]
    for i, capability in enumerate(capabilities):
        where = f"capabilities[{i}]."
        _check_keys(surface, capability, required=frozenset({"name", "providers"}), where=where)
        name = _need_at(surface, capability, "name", where)
        providers = _obj_list_at(surface, capability, "providers", required=True)
        blocks += [
            "",
            kernel.heading(f"{name} — {kernel.plural(len(providers), 'provider')}", level=3),
            "",
        ]
        if not providers:
            blocks.append(kernel.bullet("no providers"))
            continue
        blocks += [
            _capability_provider(surface, provider, where=f"{where}providers[{j}].")
            for j, provider in enumerate(providers)
        ]
    return kernel.document(blocks)


def _capability_provider(surface: str, provider: Mapping[str, object], *, where: str) -> str:
    _check_keys(
        surface,
        provider,
        required=frozenset({"name", "state"}),
        optional=frozenset({"note"}),
        where=where,
    )
    name = _str_at(surface, provider, "name", where)
    state = _str_at(surface, provider, "state", where)
    if state not in _PROVIDER_STATES:
        options = ", ".join(sorted(_PROVIDER_STATES))
        _fail(surface, f"{where}state must be one of {options}, got {state!r}")
    lines = [_entry(name, kernel.code(state))]
    if "note" in provider:
        lines.append(kernel.detail(_str_at(surface, provider, "note", where)))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# sync-report
# ---------------------------------------------------------------------------

# Fetched, never cloned: an instance holds no copy of the engine's docs, so
# a bare `docs/connect.md` would be a path to nothing on the machine reading
# this line.
_CONNECT_DOC = "https://raw.githubusercontent.com/leeovery/dex/main/docs/connect.md"

# What sync found about this machine's chat clients, as the offer the session
# reading it makes. Addressed to that session, not to the owner: the owner is
# asked a question and never handed a command, and the doc is the session's
# own next move on a yes. A client that is not installed never reaches here.
_CLIENTS = {
    "desktop": "the Claude desktop app's chat",
    "code": "Claude Code sessions on this machine",
}
_CONNECT_OFFERS = {
    "unconnected": (
        "{where}: no dex is connected — ask the owner whether they want this one "
        "reachable there, and on yes read {doc} and do the setup yourself"
    ),
    "unlisted": (
        "{where}: the connected dex serves other instances but not this one — ask the "
        "owner whether they want this dex there too, and on yes read {doc} and do the "
        "setup yourself"
    ),
    "broken": (
        "{where}: the entry points at a bin/dex that is no longer on disk, so the "
        "server cannot start and says nothing about it — the anchor instance moved or "
        "was deleted. Re-run the setup at {doc} against an instance that is still here"
    ),
}


def _render_sync_report(payload: Mapping[str, object]) -> str:
    """Render the sync report: pin state, migrations applied, machinery changes.

    Payload::

        {
          "pin": str,                 # optional: the tag now pinned (absent =
                                      #   unpinned pre-first-release mode)
          "previous": str,            # optional: prior pin (absent = no bump;
                                      #   requires pin)
          "major": bool,              # optional: the bump crossed a major —
                                      #   an owner-visible event, announced
                                      #   distinctly (requires previous)
          "migrations": [             # applied this sync, may be empty
            {"number": int, "intent": str,
             "actions": [str],                     # optional
             "skipped": [{"what": str, "why": str}],  # optional
             "anomalies": [str]}                   # optional
          ],
          "machinery_changes": int,   # template files written + retired skills removed
          "connect": [               # optional: one per client with a gap
             {"client": str,         # "desktop" | "code"
              "gap": str}            # "unconnected" | "unlisted" | "broken"
          ],
          "notes": [str],             # optional
        }

    ``skipped`` and ``anomalies`` are repaired with judgment in-session
    — they render prominently, never summarized away.
    """
    surface = "sync-report"
    _check_keys(
        surface,
        payload,
        required=frozenset({"migrations", "machinery_changes"}),
        optional=frozenset({"pin", "previous", "major", "connect", "notes"}),
    )
    pin = _str_at(surface, payload, "pin") if "pin" in payload else None
    previous = _str_at(surface, payload, "previous") if "previous" in payload else None
    if previous is not None and pin is None:
        _fail(surface, "previous requires pin — a bump lands on a pinned tag")
    major = payload.get("major", False)
    if not isinstance(major, bool):
        _fail(surface, f"major must be a boolean, got {major!r}")
    if major and previous is None:
        _fail(surface, "major requires a pin transition — previous and pin")
    migrations = _obj_list_at(surface, payload, "migrations", required=True)
    machinery_changes = _int_at(surface, payload, "machinery_changes")
    connect = _sync_connect(surface, payload)
    notes = _str_list_at(surface, payload, "notes")

    if pin is None:
        head = "Sync — engine unpinned (no release pinned; see notes)"
    elif major:
        head = f"Sync — MAJOR upgrade {previous} → {pin}"
    elif previous is not None and previous != pin:
        head = f"Sync — pin bumped {previous} → {pin}"
    else:
        head = f"Sync — engine pinned at {pin}"
    scale = kernel.inline(
        [
            kernel.plural(len(migrations), "migration"),
            kernel.plural(machinery_changes, "machinery change"),
        ]
    )
    blocks = [kernel.heading(f"{head} — {scale}"), ""]
    if major:
        # A major is an owner-visible event, not a row: the loud line sits
        # above everything else the report has to say.
        blocks += [
            (
                f"{kernel.bold('MAJOR ENGINE UPGRADE')} — {previous} → {pin} auto-applied "
                "(the always-migratable commitment); review this report closely."
            ),
            "",
        ]
    if migrations:
        blocks += [kernel.heading(f"Migrations applied — {len(migrations)}", level=3), ""]
        blocks += [
            _sync_migration(surface, migration, where=f"migrations[{i}].")
            for i, migration in enumerate(migrations)
        ]
    else:
        blocks.append("No migrations to apply — state was already current.")
    blocks += ["", f"{kernel.bold('Machinery changes')} — {machinery_changes}"]
    if connect:
        blocks += ["", *connect]
    blocks += _note_section("Notes", notes)
    return kernel.document(blocks)


def _sync_connect(surface: str, payload: Mapping[str, object]) -> list[str]:
    """The chat-connection gaps, one line per client with something to say."""
    rows = _obj_list_at(surface, payload, "connect", required=False)
    lines = []
    for i, row in enumerate(rows):
        where = f"connect[{i}]."
        _check_keys(surface, row, required=frozenset({"client", "gap"}), where=where)
        client = _str_at(surface, row, "client", where)
        gap = _str_at(surface, row, "gap", where)
        if client not in _CLIENTS:
            _fail(
                surface,
                f"connect client must be one of {', '.join(sorted(_CLIENTS))}, got {client!r}",
            )
        if gap not in _CONNECT_OFFERS:
            _fail(
                surface,
                f"connect gap must be one of {', '.join(sorted(_CONNECT_OFFERS))}, got {gap!r}",
            )
        offer = _CONNECT_OFFERS[gap].format(where=_CLIENTS[client], doc=_CONNECT_DOC)
        lines.append(f"{kernel.bold('Chat connection')} — {offer}")
    return lines


def _sync_migration(surface: str, migration: Mapping[str, object], *, where: str) -> str:
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
    skipped: list[str] = []
    for j, entry in enumerate(_obj_list_at(surface, migration, "skipped", required=False)):
        swhere = f"{where}skipped[{j}]."
        _check_keys(surface, entry, required=frozenset({"what", "why"}), where=swhere)
        skipped.append(
            f"{kernel.code(_str_at(surface, entry, 'what', swhere))} — "
            f"{_str_at(surface, entry, 'why', swhere)}"
        )
    lines = [kernel.bullet(f"{kernel.bold(f'migration {number}')} — {intent}")]
    if actions:
        lines.append(kernel.bullet(f"actions — {len(actions)}", depth=1))
        lines += [kernel.bullet(action, depth=2) for action in actions]
    else:
        lines.append(kernel.bullet("actions — none", depth=1))
    if skipped:
        lines.append(
            kernel.bullet(
                f"{kernel.bold('Repair with judgment')} — {len(skipped)} skipped", depth=1
            )
        )
        lines += [kernel.bullet(entry, depth=2) for entry in skipped]
    if anomalies:
        lines.append(
            kernel.bullet(
                f"{kernel.bold('REVIEW REQUIRED')} — "
                f"{kernel.plural(len(anomalies), 'anomaly', 'anomalies')}",
                depth=1,
            )
        )
        lines += [kernel.bullet(anomaly, depth=2) for anomaly in anomalies]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ingest-receipt — the per-item receipt the session renders
# ---------------------------------------------------------------------------

_SIGNALS = frozenset({"high", "medium", "low"})


def _render_ingest_receipt(payload: Mapping[str, object]) -> str:
    """Render the per-item ingest receipt (the cognitive call path).

    Payload::

        {
          "item": str,               # the corpus item id
          "title": str,              # optional: one-line title
          "fetched": int,            # optional: enrichment units landed
          "parked": int,             # optional: units still outstanding
          "signal": str,             # optional: the digest's high|medium|low
          "topics": [str],           # optional: where the item was placed
          "pages": [str],            # optional: wiki pages touched
          "notes": [str],            # optional: free prose, judgment's slot
        }
    """
    surface = "ingest-receipt"
    _check_keys(
        surface,
        payload,
        required=frozenset({"item"}),
        optional=frozenset({"title", "fetched", "parked", "signal", "topics", "pages", "notes"}),
    )
    item = _str_at(surface, payload, "item")
    title = _str_at(surface, payload, "title") if "title" in payload else None
    fetched = _int_at(surface, payload, "fetched", default=0)
    parked = _int_at(surface, payload, "parked", default=0)
    signal = _str_at(surface, payload, "signal") if "signal" in payload else None
    if signal is not None and signal not in _SIGNALS:
        options = ", ".join(sorted(_SIGNALS))
        _fail(surface, f"signal must be one of {options}, got {signal!r}")
    topics = _str_list_at(surface, payload, "topics")
    pages = _str_list_at(surface, payload, "pages")
    notes = _str_list_at(surface, payload, "notes")

    if fetched or parked:
        scale = f"{kernel.plural(fetched, 'unit')} fetched"
        if parked:
            scale += f", {parked} outstanding"
    else:
        # A no-source capture (note-only, image-only) has no number to
        # give, and the heading says so out loud.
        scale = "no units fetched"
    blocks = [kernel.heading(f"Ingested {item} — {scale}")]
    if title:
        blocks += ["", title]
    counts = [
        part
        for part in (
            f"fetched {fetched}" if fetched else "",
            f"outstanding {parked}" if parked else "",
        )
        if part
    ]
    bullets = [kernel.bullet(kernel.inline(counts))] if counts else []
    if signal:
        bullets.append(kernel.bullet(f"signal {kernel.code(signal)}"))
    if topics:
        bullets.append(kernel.bullet("topics: " + ", ".join(topics)))
    if pages:
        bullets.append(kernel.bullet("pages: " + ", ".join(pages)))
    if bullets:
        blocks += ["", *bullets]
    blocks += _note_section("Notes", notes)
    return kernel.document(blocks)


# ---------------------------------------------------------------------------
# health-report — the lint surface
# ---------------------------------------------------------------------------

_HEALTH_OPTIONAL = frozenset(
    {
        "broken_links",
        "reserved_links",
        "bad_citations",
        "shortid_citations",
        "orphans",
        "unplaced",
        "ghost_members",
        "unindexed",
        "ghost_index",
        "stale_pages",
        "count_drift",
        "restated",
        "taxonomy_error",
        "entity_members_error",
        "ledger_entries",
        "ledger_error",
        "passes_error",
        "ghost_items",
        "missing_outputs",
        "misfiled_outputs",
        "waiting",
        "cognitive",
        "never_harvested",
        "stale_passes",
        "capped",
        "incomplete_threads",
        "digests",
        "digest_errors",
        "digest_orphans",
        "digest_media_drift",
        "undescribed_media",
        "reconciled",
        "notes",
    }
)

# Listing caps — the report names the scale, shows enough to act on, and
# says how much it withheld; the checks' full detail is greppable in the
# instance itself.
_HEALTH_LIST_CAP = 20
# How many worst offenders the cap-fire summary names before the listing.
_HEALTH_OFFENDERS = 5


def _render_health_report(payload: Mapping[str, object]) -> str:
    """Render the lint health report.

    Unlike the run reports, a check with nothing to say still renders its
    line: there the absence IS the finding, and the line is the proof the
    check ran at all.

    Payload::

        {
          # EITHER the pre-taxonomy shape, travelling alone — the two
          # states before state/taxonomy.json exists, when no other check
          # has run and a full payload would claim checks that never did:
          "pre_taxonomy": {"stranded": [str]},   # [] = fresh instance;
                                                 # ids = broken mid-ingest
          # OR the full shape:
          "summary": {"corpus_items": int, "pages": int, "cited": int},
          # wiki checks — each optional, absent = not applicable
          "broken_links": [{"page": str, "target": str}],
          "reserved_links": int,
          "bad_citations": [{"page": str, "id": str}],
          "shortid_citations": [{"page": str, "token": str}],
          "orphans": [str],
          "unplaced": [str],   # cited, but in no topic's items and unledgered
          # a taxonomy topic's or entity's member id with no corpus file —
          # `where` names the list holding it
          "ghost_members": [{"item": str, "where": str}],
          "unindexed": [str],
          "ghost_index": [str],
          "stale_pages": [{"page": str, "newer": int}],
          "count_drift": [{"page": str, "recorded": str, "actual": int}],  # actual = member count
          "restated": [{"page": str, "first": str, "second": str}],
          "taxonomy_error": str,        # malformed taxonomy — renders loud
          "entity_members_error": str,  # malformed entity-members — renders loud
          # state checks
          "ledger_entries": int,
          "ledger_error": str,          # schema failure — renders loud
          "passes_error": str,          # torn/unparseable pass record — renders loud
          # one row per (item, finding), never per entry — "entries" is the multiplicity
          "ghost_items": [{"item": str, "why": str, "entries": int}],  # item has no corpus file
          "missing_outputs": [{"item": str, "path": str}], # done output gone from disk
          # done output on disk, but under another item's enrichment directory
          "misfiled_outputs": [{"item": str, "path": str}],
          "waiting": {"<need>": int},
          "cognitive": [{"item": str, "url": str, "need": str}],
          "never_harvested": [str],   # fetched pages landed, no pass on record
          "stale_passes": [{"item": str, "rules": int}],
          # judgment drift — recorded for this surface, shown on no other
          "capped": [{"item": str, "url": str, "reason": str}],   # re-entry cap fires
          "incomplete_threads": [{"path": str, "why": str}],      # short thread walk-ups
          # digests
          "digests": int,
          "digest_errors": [{"item": str, "why": str}],   # shape failure — renders loud
          "digest_orphans": [str],
          "digest_media_drift": [str],   # stated media: is not the media on disk
          # media on disk that no session has described
          "undescribed_media": [{"item": str, "binaries": int, "described": int}],
          # --write outcomes and free notes
          "reconciled": [str],
          "notes": [str],
        }
    """
    surface = "health-report"
    if "pre_taxonomy" in payload:
        return _health_pre_taxonomy(surface, payload)
    _check_keys(surface, payload, required=frozenset({"summary"}), optional=_HEALTH_OPTIONAL)
    summary = payload["summary"]
    if not isinstance(summary, Mapping):
        _fail(surface, "summary must be an object")
    _check_keys(
        surface,
        summary,
        required=frozenset({"corpus_items", "pages", "cited"}),
        where="summary.",
    )
    scale = kernel.inline(
        [
            kernel.plural(_int_at(surface, summary, "corpus_items", "summary."), "corpus item"),
            kernel.plural(_int_at(surface, summary, "pages", "summary."), "page"),
            f"{_int_at(surface, summary, 'cited', 'summary.')} cited",
        ]
    )
    blocks = [kernel.heading(f"Health check — {scale}")]
    blocks += _health_wiki(surface, payload, _int_at(surface, summary, "pages", "summary."))
    blocks += _health_state(surface, payload)
    blocks += _health_digests(surface, payload)
    reconciled = _str_list_at(surface, payload, "reconciled")
    if reconciled:
        scaled = f"Reconciled by `--write` — {kernel.plural(len(reconciled), 'repair')}"
        blocks += ["", kernel.heading(scaled, level=3), ""]
        blocks += [kernel.bullet(line) for line in reconciled]
    blocks += _note_section("Notes", _str_list_at(surface, payload, "notes"))
    return kernel.document(blocks)


def _health_pre_taxonomy(surface: str, payload: Mapping[str, object]) -> str:
    """The two states before a taxonomy exists: fresh instance, broken mid-ingest.

    The key travels alone — no check beyond the corpus glob has run, and a
    full payload beside it would render checks that never did. The heading
    still names the scale it has in hand: the corpus item count.
    """
    _check_keys(surface, payload, required=frozenset({"pre_taxonomy"}))
    inner = payload["pre_taxonomy"]
    if not isinstance(inner, Mapping):
        _fail(surface, f"pre_taxonomy must be an object, got {type(inner).__name__}")
    _check_keys(surface, inner, required=frozenset({"stranded"}), where="pre_taxonomy.")
    stranded = _str_list_at(surface, inner, "stranded", "pre_taxonomy.")
    if not stranded:
        return kernel.document(
            [
                kernel.heading("Health check — fresh instance, 0 corpus items"),
                "",
                "No `state/taxonomy.json` yet — nothing to lint.",
            ]
        )
    scale = kernel.plural(len(stranded), "corpus item")
    blocks = [
        kernel.heading(f"Health check — broken mid-ingest, {scale}"),
        "",
        kernel.bullet(
            f"{kernel.bold('BROKEN MID-INGEST')} — {scale} but no "
            "`state/taxonomy.json`; placement never ran"
        ),
    ]
    blocks += [kernel.bullet(kernel.bold(item), depth=1) for item in stranded[:_HEALTH_LIST_CAP]]
    blocks += _health_elided(len(stranded))
    return kernel.document(blocks)


def _health_wiki(surface: str, payload: Mapping[str, object], pages: int) -> list[str]:
    blocks = ["", kernel.heading(f"Wiki — {kernel.plural(pages, 'page')}", level=3), ""]
    if "taxonomy_error" in payload:
        blocks.append(
            kernel.bullet(
                f"{kernel.bold('TAXONOMY FAILURE')} — repair `state/taxonomy.json`; "
                "the wiki checks below ran against an empty taxonomy"
            )
        )
        blocks.append(kernel.bullet(_str_at(surface, payload, "taxonomy_error"), depth=1))
    if "entity_members_error" in payload:
        blocks.append(
            kernel.bullet(
                f"{kernel.bold('ENTITY MEMBERS FAILURE')} — repair "
                "`state/entity-members.json`; the wiki checks below ran with "
                "no entity members"
            )
        )
        blocks.append(kernel.bullet(_str_at(surface, payload, "entity_members_error"), depth=1))
    blocks += _health_pairs(
        surface,
        payload,
        "broken_links",
        "broken wikilinks",
        ("page", "target"),
        lambda p, t: f"{kernel.bold(p)} → `[[{t}]]`",
    )
    reserved = _int_at(surface, payload, "reserved_links", default=0)
    blocks.append(_health_count("reserved/unbuilt links (informational)", reserved))
    blocks += _health_pairs(
        surface,
        payload,
        "bad_citations",
        "bad citations (id not in corpus)",
        ("page", "id"),
        lambda p, i: f"{kernel.bold(p)} → {kernel.bold(i)}",
    )
    blocks += _health_pairs(
        surface,
        payload,
        "shortid_citations",
        "shortid-shaped citations (citations are full item ids, always)",
        ("page", "token"),
        lambda p, t: f"{kernel.bold(p)} → `{t}`",
    )
    blocks += _health_names(
        surface, payload, "orphans", "items no page cites and the taxonomy does not record"
    )
    blocks += _health_names(
        surface, payload, "unplaced", "items a page cites but no taxonomy topic records"
    )
    blocks += _health_pairs(
        surface,
        payload,
        "ghost_members",
        "ghost members (id has no corpus file — remove it from the named list; "
        "for an excluded item, that is the whole repair)",
        ("item", "where"),
        lambda i, w: f"{kernel.bold(i)} — in {w}",
    )
    blocks += _health_names(surface, payload, "unindexed", "pages missing from index")
    blocks += _health_names(surface, payload, "ghost_index", "ghost index entries")
    stale = _health_rows(surface, payload, "stale_pages", ("page",), int_keys=("newer",))
    blocks += _health_listing(
        "stale pages (members newer than the page)",
        stale,
        lambda row: (
            f"{kernel.bold(str(row['page']))} — "
            f"{kernel.plural(int(str(row['newer'])), 'newer item')}"
        ),
    )
    drift = _health_rows(
        surface, payload, "count_drift", ("page", "recorded"), int_keys=("actual",)
    )
    blocks += _health_listing(
        "item-count drift (frontmatter `items:` vs members)",
        drift,
        lambda row: (
            f"{kernel.bold(str(row['page']))} — `items: {row['recorded']}`, {row['actual']} members"
        ),
    )
    blocks += _health_restated(surface, payload)
    return blocks


def _health_restated(surface: str, payload: Mapping[str, object]) -> list[str]:
    """Pairs of sentences on one page that may say the same thing twice.

    The ``~`` marker rides inside the bullet text, never as a prefix: a
    prefix repeats on every continuation line, so two facts rendered four
    markers.
    """
    rows = _health_rows(surface, payload, "restated", ("page", "first", "second"))
    lines = [_health_count("possible restated facts (same page, merge?)", len(rows))]
    for row in rows[:_HEALTH_LIST_CAP]:
        lines.append(kernel.bullet(kernel.bold(str(row["page"])), depth=1))
        lines.append(kernel.bullet(f"~ {row['first']}", depth=2))
        lines.append(kernel.bullet(f"~ {row['second']}", depth=2))
    lines += _health_elided(len(rows))
    return lines


def _health_state(surface: str, payload: Mapping[str, object]) -> list[str]:
    # The ledger is the section's scale, and a ledger that will not load has
    # none to state — saying "0 entries" there would report an unreadable
    # file as an empty one.
    scale = (
        "ledger unreadable"
        if "ledger_error" in payload
        else kernel.plural(
            _int_at(surface, payload, "ledger_entries", default=0), "ledger entry", "ledger entries"
        )
    )
    blocks = ["", kernel.heading(f"State — {scale}", level=3), ""]
    if "ledger_error" in payload:
        blocks.append(
            kernel.bullet(
                f"{kernel.bold('LEDGER SCHEMA FAILURE')} — "
                "repair before anything else touches state"
            )
        )
        blocks.append(kernel.bullet(_str_at(surface, payload, "ledger_error"), depth=1))
    else:
        entries = _int_at(surface, payload, "ledger_entries", default=0)
        blocks.append(
            kernel.bullet(
                f"ledger — {kernel.bold(kernel.plural(entries, 'entry', 'entries'))}, schema valid"
            )
        )
    ghost = _health_rows(surface, payload, "ghost_items", ("item", "why"), int_keys=("entries",))
    blocks += _health_listing(
        "ledger items with no corpus file",
        ghost,
        lambda row: (
            f"{kernel.bold(str(row['item']))} — "
            f"{kernel.plural(int(str(row['entries'])), 'entry', 'entries')} ({row['why']})"
        ),
    )
    blocks += _health_pairs(
        surface,
        payload,
        "missing_outputs",
        "done entries whose output file is gone from disk",
        ("item", "path"),
        lambda i, p: f"{kernel.bold(i)} → `{p}`",
    )
    blocks += _health_pairs(
        surface,
        payload,
        "misfiled_outputs",
        "done entries whose output sits under another item's directory",
        ("item", "path"),
        lambda i, p: f"{kernel.bold(i)} → `{p}`",
    )
    waiting = _needs_counts(surface, payload, "waiting")
    if waiting:
        cohorts = kernel.inline(f"{kernel.code(need)} {count}" for need, count in sorted(waiting))
        blocks.append(kernel.bullet(f"waiting on a capability — {cohorts}"))
    else:
        blocks.append(kernel.bullet("waiting on a capability — none"))
    cognitive = _cognitive_rows(surface, payload)
    blocks.append(_health_count("read these yourself (the engine cannot do them)", len(cognitive)))
    for item, need, url in cognitive[:_HEALTH_LIST_CAP]:
        blocks.append(kernel.bullet(kernel.inline([kernel.bold(item), kernel.code(need)]), depth=1))
        blocks.append(kernel.detail(url, depth=1))
    blocks += _health_elided(len(cognitive))
    blocks += _health_names(
        surface,
        payload,
        "never_harvested",
        "harvest these (enrichment landed, no harvest pass on record)",
    )
    if "passes_error" in payload:
        blocks.append(
            kernel.bullet(
                f"{kernel.bold('PASSES FAILURE')} — repair `state/passes.jsonl`: delete "
                "the torn line named below, and only that line (a half-written line is "
                "not a record; every intact line is); the pass readings below ran "
                "without the broken record"
            )
        )
        blocks.append(kernel.bullet(_str_at(surface, payload, "passes_error"), depth=1))
    stale_passes = _health_rows(surface, payload, "stale_passes", ("item",), int_keys=("rules",))
    blocks += _health_listing(
        "harvest passes under old rules (re-judge)",
        stale_passes,
        lambda row: f"{kernel.bold(str(row['item']))} — rules v{row['rules']}",
    )
    blocks += _health_cap_fires(surface, payload)
    blocks += _health_pairs(
        surface,
        payload,
        "incomplete_threads",
        "stored threads recorded incomplete (never cite one as whole)",
        ("path", "why"),
        lambda p, w: f"`{p}` — {w}",
    )
    return blocks


def _health_digests(surface: str, payload: Mapping[str, object]) -> list[str]:
    digests = _int_at(surface, payload, "digests", default=0)
    blocks = ["", kernel.heading(f"Digests — {kernel.plural(digests, 'digest')}", level=3), ""]
    blocks += _health_pairs(
        surface,
        payload,
        "digest_errors",
        "MALFORMED DIGESTS (the wiki layer reads these)",
        ("item", "why"),
        lambda i, w: f"{kernel.bold(i)} — {w}",
    )
    # The repair ("digest these") is the label; the procedure lives in the
    # dex-lint skill.
    blocks += _health_names(
        surface, payload, "digest_orphans", "digest these (enrichment newer than digest)"
    )
    blocks += _health_names(
        surface,
        payload,
        "digest_media_drift",
        "re-emit these digests (`media:` is not the media on disk)",
    )
    undescribed = _health_rows(
        surface, payload, "undescribed_media", ("item",), int_keys=("binaries", "described")
    )
    blocks += _health_listing(
        "describe these (media carried, no description written)",
        undescribed,
        lambda row: (
            f"{kernel.bold(str(row['item']))} — {row['described']} of {row['binaries']} described"
        ),
    )
    return blocks


def _health_cap_fires(surface: str, payload: Mapping[str, object]) -> list[str]:
    """The cap-fire block: a tuning reading on the depth-4 / 12-URL bounds.

    Shaped as counts first — total, spread across items, per bound, worst
    offenders — because the question a fire answers is never "is this one
    wrong" but "are the bounds wrong for this corpus, or is harvest
    over-promoting". The refused URLs follow so the answer is checkable.
    """
    rows = _health_rows(surface, payload, "capped", ("item", "url", "reason"))
    label = "re-entry cap fires (tuning signal, not an alarm)"
    if not rows:
        return [_health_count(label, 0)]
    per_item: dict[str, int] = {}
    per_reason: dict[str, int] = {}
    for row in rows:
        per_item[str(row["item"])] = per_item.get(str(row["item"]), 0) + 1
        per_reason[str(row["reason"])] = per_reason.get(str(row["reason"]), 0) + 1
    lines = [
        kernel.bullet(
            f"{label} — {kernel.bold(str(len(rows)))} across {kernel.plural(len(per_item), 'item')}"
        ),
        kernel.bullet(
            "by bound: "
            + kernel.inline(f"`{reason}` {count}" for reason, count in sorted(per_reason.items())),
            depth=1,
        ),
    ]
    offenders = sorted(per_item.items(), key=lambda pair: (-pair[1], pair[0]))
    named = [f"{kernel.bold(item)} {count}" for item, count in offenders[:_HEALTH_OFFENDERS]]
    if len(offenders) > _HEALTH_OFFENDERS:
        # A capped listing says it is capped — a silently short list lies
        # about scale.
        named.append(f"… and {len(offenders) - _HEALTH_OFFENDERS} more items, not listed")
    lines.append(kernel.bullet("most often: " + kernel.inline(named), depth=1))
    for row in rows[:_HEALTH_LIST_CAP]:
        lines.append(kernel.bullet(kernel.bold(str(row["item"])), depth=1))
        lines.append(kernel.detail(str(row["url"]), depth=1))
    lines += _health_elided(len(rows))
    return lines


_T = TypeVar("_T")


def _health_count(label: str, count: int) -> str:
    return kernel.bullet(f"{label} — {kernel.bold(str(count)) if count else 'none'}")


def _health_elided(total: int) -> list[str]:
    """Say what the listing cap withheld — a silently short list lies about scale."""
    if total <= _HEALTH_LIST_CAP:
        return []
    return [kernel.bullet(f"… and {total - _HEALTH_LIST_CAP} more, not listed", depth=1)]


def _health_listing(label: str, entries: list[_T], shape: Callable[[_T], str]) -> list[str]:
    """One check: its count, its first rows, and what the cap withheld."""
    lines = [_health_count(label, len(entries))]
    lines += [kernel.bullet(shape(entry), depth=1) for entry in entries[:_HEALTH_LIST_CAP]]
    lines += _health_elided(len(entries))
    return lines


def _health_names(surface: str, payload: Mapping[str, object], key: str, label: str) -> list[str]:
    names = _str_list_at(surface, payload, key)
    return _health_listing(label, names, lambda name: kernel.bold(str(name)))


def _health_rows(
    surface: str,
    payload: Mapping[str, object],
    key: str,
    str_keys: tuple[str, ...],
    *,
    int_keys: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for i, entry in enumerate(_obj_list_at(surface, payload, key, required=False)):
        where = f"{key}[{i}]."
        _check_keys(surface, entry, required=frozenset(str_keys) | frozenset(int_keys), where=where)
        row: dict[str, object] = {k: _str_at(surface, entry, k, where) for k in str_keys}
        row |= {k: _int_at(surface, entry, k, where) for k in int_keys}
        rows.append(row)
    return rows


def _health_pairs(  # noqa: PLR0913, PLR0917 — a formatting helper: surface, payload, key, label, fields, shape
    surface: str,
    payload: Mapping[str, object],
    key: str,
    label: str,
    fields: tuple[str, str],
    shape: Callable[[str, str], str],
) -> list[str]:
    rows = _health_rows(surface, payload, key, fields)
    return _health_listing(label, rows, lambda row: shape(str(row[fields[0]]), str(row[fields[1]])))


# The typed registry literal is the conformance point: every renderer is
# checked against the surface signature at this one assignment.
SURFACES: dict[str, Callable[[Mapping[str, object]], str]] = {
    "enrich-report": _render_enrich_report,
    "status": _render_status,
    "item-status": _render_item_status,
    "capability-report": _render_capability_report,
    "sync-report": _render_sync_report,
    "ingest-receipt": _render_ingest_receipt,
    "health-report": _render_health_report,
}
