"""The orchestrator (§1): seed, drain, write, re-enter — and report verbatim.

The ledger is the work queue. A run seeds queued entries for new corpus
URLs, drains everything :func:`is_drainable`, hands units to drivers, writes
deterministically named outputs (reruns overwrite, never duplicate),
downloads media (§7), re-enters children with provenance and caps, and
renders its report through the ``enrich-report`` surface (§11).

Exactly ONE ``except Exception`` exists in the whole pipeline: the per-unit
loop here (§5). Every ledger write goes through ``ledger.stamp`` with the
injected clock and engine version (§14).
"""

import dataclasses
import datetime
import json
import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import assert_never

from dex_engine import corpus
from dex_engine.drivers.transport import Transport, urllib_transport
from dex_engine.render import surfaces

from . import ledger
from .classify import ProviderInputError, classify_connection, classify_http, scrub
from .detect import Sniff, canonical_url, detect, detect_kind
from .registry import driver_for
from .types import (
    Availability,
    Child,
    Config,
    Instance,
    Kind,
    LedgerEntry,
    MediaFetch,
    Need,
    Result,
    SourceDriver,
    Status,
    WorkUnit,
    version_newer,
)
from .urls import work_hash

__all__ = [
    "HARVEST_RULES_VERSION",
    "MAX_BLOCKED_ATTEMPTS",
    "MAX_DEPTH",
    "MAX_URLS_PER_ITEM",
    "MEDIA_MAX_BYTES",
    "MEDIA_MAX_FILES",
    "RunContext",
    "fetch_urls",
    "head_sniffer",
    "is_drainable",
    "mark",
    "no_providers",
    "record_pass",
    "run",
    "status_report",
]

# Re-entry caps (§1): mechanical backstops, not targets. Fires are recorded
# in the ledger (a skipped entry with the reason) — never user-surfaced.
MAX_DEPTH = 4
MAX_URLS_PER_ITEM = 12

# Blocked retries every run; the 5th failed attempt escalates to manual (§5).
MAX_BLOCKED_ATTEMPTS = 5

# Media stage (§7).
MEDIA_MAX_FILES = 4
MEDIA_MAX_BYTES = 10 * 1024 * 1024

# Bumped when the harvest rules change (§10); recorded in passes.jsonl.
HARVEST_RULES_VERSION = 1

_PARKED = frozenset({Status.WAITING, Status.BLOCKED, Status.ERROR, Status.MANUAL})
_PASS_STAGES = frozenset({"harvest", "digest", "wiki"})


def no_providers(need: Need) -> Availability:
    """The phase-2 provider seam: no mechanical providers exist yet (§6)."""
    return Availability(
        ok=False, reason=f"no mechanical '{need}' provider registered (capabilities are phase 3)"
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RunContext:
    """Everything a run needs, constructor-injected (§14) — no ambient state."""

    instance: Instance
    config: Config
    drivers: Sequence[SourceDriver]
    today: Callable[[], datetime.date]
    engine_version: str
    transport: Transport = urllib_transport
    provider_available: Callable[[Need], Availability] = no_providers
    sleep: Callable[[float], None] = time.sleep


def head_sniffer(transport: Transport) -> Sniff:
    """A detection sniff seam over ``transport``: one HEAD, content type or None."""

    def sniff(url: str) -> str | None:
        try:
            response = transport(url, method="HEAD")
        except OSError:
            return None  # inconclusive — the GET that follows will classify
        if not response.ok:
            return None
        return response.content_type or None

    return sniff


def is_drainable(entry: LedgerEntry, ctx: RunContext) -> bool:
    """Whether a run picks ``entry`` up — the drain predicate, total (§2).

    queued always; blocked while attempts remain; waiting when a mechanical
    provider is available; error once per newer engine; every terminal
    status never.
    """
    match entry.status:
        case Status.QUEUED:
            return True
        case Status.BLOCKED:
            return (entry.attempts or 0) < MAX_BLOCKED_ATTEMPTS
        case Status.WAITING:
            return entry.needs is not None and ctx.provider_available(entry.needs).ok
        case Status.ERROR:
            return version_newer(ctx.engine_version, entry.engine)
        case Status.DONE | Status.DEAD | Status.SKIPPED | Status.MANUAL:
            return False
        case _:
            assert_never(entry.status)


# ---------------------------------------------------------------------------
# The drain.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ItemOutcome:
    """What a run did for one item — feeds the report's cognitive work list."""

    new: int = 0
    changed: int = 0
    media: int = 0

    def reason(self) -> str:
        parts = [
            f"{self.new} new" if self.new else "",
            f"{self.changed} changed" if self.changed else "",
            f"{self.media} media" if self.media else "",
        ]
        return ", ".join(part for part in parts if part)


@dataclass(slots=True)
class _Drain:
    """One run's mutable state. Internal to this module."""

    ctx: RunContext
    entries: dict[str, LedgerEntry] = field(default_factory=dict)
    item_paths: dict[str, Path] = field(default_factory=dict)
    queue: deque[str] = field(default_factory=deque)
    counts: dict[Status, int] = field(default_factory=dict)
    parked: list[dict[str, str]] = field(default_factory=list)
    outcomes: dict[str, _ItemOutcome] = field(default_factory=dict)
    sniff: Sniff | None = None

    def __post_init__(self) -> None:
        self.entries = ledger.load(self.ctx.instance.ledger_path)
        self.sniff = head_sniffer(self.ctx.transport)

    # -- seeding ---------------------------------------------------------

    def seed_from_corpus(self) -> None:
        """Every captured URL becomes a ledger entry from birth (§1)."""
        for path in sorted(self.ctx.instance.corpus_dir.glob("*/*.md")):
            item = corpus.read_item(path)
            self.item_paths[item.id] = path
            for url in item.urls:
                self._seed_url(item.id, url)

    def _seed_url(self, item_id: str, url: str) -> None:
        canonical, unit_hash = self.identify(url)
        if unit_hash in self.entries:
            return  # includes the two-items dedupe edge (§5): first item won
        detection = detect(url, self.ctx.drivers, sniff=self.sniff)
        self.record(
            LedgerEntry(
                hash=unit_hash,
                url=canonical,
                item=item_id,
                kind=detection.kind,
                format=detection.format,
                status=Status.QUEUED,
                engine="seed",  # stamped below
                date=datetime.date.min,
            )
        )

    def identify(self, url: str) -> tuple[str, str]:
        """Canonical form and work hash — delegation lives in detect (§2)."""
        canonical = canonical_url(url, self.ctx.drivers)
        return canonical, work_hash(canonical)

    # -- the loop --------------------------------------------------------

    def drain(self, *, limit: int | None = None, only: set[str] | None = None) -> None:
        """Process every drainable entry (or the ``only`` subset), FIFO."""
        self.queue = deque(
            unit_hash
            for unit_hash, entry in self.entries.items()
            if (only is None or unit_hash in only) and is_drainable(entry, self.ctx)
        )
        processed = 0
        while self.queue and (limit is None or processed < limit):
            entry = self.entries[self.queue.popleft()]
            if not is_drainable(entry, self.ctx):
                continue  # superseded while queued
            processed += 1
            self._process(entry)
        self._refresh_items()

    def _process(self, entry: LedgerEntry) -> None:
        if entry.via == "media":
            # The ONE via-routed dispatch: §7 gives blocked media downloads
            # normal retry rules, and via is the only mark they carry.
            self._download_media(entry, prior=entry)
            return
        driver = driver_for(entry.kind, self.ctx.drivers)
        if driver is None:
            self._park_driverless(entry)
            return
        unit = WorkUnit(
            hash=entry.hash,
            url=entry.url,
            kind=entry.kind,
            format=entry.format,
            item=entry.item,
            depth=entry.depth or 0,
            parent=entry.parent,
        )
        try:
            # The try spans ALL per-unit processing (§5's "per-unit loop"):
            # fetch, output write, media stage, children admission. An
            # engine bug anywhere in it ledgers `error` — the outcome line
            # supersedes any partial one — and never aborts the run.
            self._apply(entry, driver.fetch(unit))
        except ProviderInputError as e:
            self.record_outcome(entry, status=Status.MANUAL, reason=scrub(str(e)))
        except Exception as e:  # noqa: BLE001 — THE one broad catch in the pipeline (§5)
            self.record_outcome(entry, status=Status.ERROR, error=scrub(f"{type(e).__name__}: {e}"))
        self.ctx.sleep(driver.sleep)

    def _park_driverless(self, entry: LedgerEntry) -> None:
        """No driver ships for this kind yet (file/podcast until phase 3).

        Parked, never crashed: phase 3 requeues these entries when the
        driver lands (file work drains as `waiting` the moment an extract
        provider exists; other kinds are re-marked by the shipping phase).
        """
        if entry.kind is Kind.FILE:
            self.record_outcome(
                entry,
                status=Status.WAITING,
                needs=Need.EXTRACT,
                reason=f"{entry.format or 'file'} extraction not yet available",
            )
            return
        self.record_outcome(
            entry,
            status=Status.MANUAL,
            reason=f"no driver for kind '{entry.kind}' yet (ships phase 3)",
        )

    def _apply(self, entry: LedgerEntry, result: Result) -> None:
        match result.status:
            case Status.DONE:
                self._apply_done(entry, result)
            case Status.WAITING:
                self.record_outcome(
                    entry, status=Status.WAITING, needs=result.needs, reason=result.reason
                )
            case Status.BLOCKED:
                self._apply_blocked(entry, result.reason)
            case Status.DEAD | Status.SKIPPED | Status.MANUAL:
                self.record_outcome(entry, status=result.status, reason=result.reason)
            case Status.QUEUED | Status.ERROR:
                # Result.__post_init__ forbids both driver outcomes (§2/§5):
                # queued is a birth state; errors are raised, never returned.
                raise RuntimeError(
                    f"unreachable: Result validation rejects {result.status.value!r}"
                )
            case _:
                assert_never(result.status)

    def _apply_done(self, entry: LedgerEntry, result: Result) -> None:
        path = None
        if result.body is not None:
            path = self._write_output(entry, result)
        title = result.meta.get("title")
        self.record_outcome(
            entry,
            status=Status.DONE,
            path=path,
            title=title if isinstance(title, str) and path is not None else None,
        )
        if result.media and self.ctx.config.media_fetch is not MediaFetch.NONE:
            self._media_stage(self.entries[entry.hash], result.media)
        self._admit_children(self.entries[entry.hash], result.children)

    def _apply_blocked(self, entry: LedgerEntry, reason: str | None) -> None:
        attempts = (entry.attempts or 0) + 1
        if attempts >= MAX_BLOCKED_ATTEMPTS:
            # Escalation appends attempt context to what the driver knew —
            # it never invents a reason (§2).
            detail = reason or "no reason recorded"
            self.record_outcome(
                entry,
                status=Status.MANUAL,
                reason=f"still blocked after {attempts} attempts — {detail}",
            )
            return
        self.record_outcome(entry, status=Status.BLOCKED, attempts=attempts, reason=reason)

    # -- outputs ---------------------------------------------------------

    def _write_output(self, entry: LedgerEntry, result: Result) -> str:
        """Write ``<kind>-<hash6>.md`` deterministically; byte-compare reruns (§12).

        The ``fetched:`` stamp is masked out of the comparison — it changes
        every run by definition, and an unchanged rerun must not rewrite the
        file (nor report the item as changed).
        """
        name = f"{entry.kind.value}-{entry.hash[:6]}.md"
        out = self.ctx.instance.enrichment_dir / entry.item / name
        body = result.body or ""
        content = _render_enrichment(entry.url, self.ctx.today(), result.meta, body)
        outcome = self.outcomes.setdefault(entry.item, _ItemOutcome())
        if out.exists():
            old = out.read_text(encoding="utf-8")
            if _mask_fetched(old) == _mask_fetched(content):
                return str(out.relative_to(self.ctx.instance.root))
            outcome.changed += 1
        else:
            outcome.new += 1
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
        return str(out.relative_to(self.ctx.instance.root))

    # -- media stage (§7) ------------------------------------------------

    def _media_stage(self, parent: LedgerEntry, urls: list[str]) -> None:
        for url in urls:
            unit_hash = work_hash(url)
            existing = self.entries.get(unit_hash)
            if existing is not None:
                continue  # done/parked already, or queued for a later pass
            child = LedgerEntry(
                hash=unit_hash,
                url=url,  # media URLs are fetched verbatim — no canonicalization,
                # signed query params ARE the resource
                item=parent.item,
                kind=parent.kind,
                status=Status.QUEUED,
                engine="seed",
                date=datetime.date.min,
                via="media",
                parent=parent.hash,
                depth=(parent.depth or 0) + 1,
            )
            # The birth line lands BEFORE the download (§1: entries from
            # birth) — a crash mid-download leaves a queued via:media entry
            # the next run's redrain path picks up.
            self.record(child)
            self._download_media(self.entries[unit_hash], prior=None)

    def _download_media(self, entry: LedgerEntry, *, prior: LedgerEntry | None) -> None:
        # Cap and oversize skips below land in the report's unit counts —
        # §7 sanctions that: they are unit outcomes, unlike the §1 re-entry
        # cap fires, which never surface.
        slot = self._media_slot(entry.item)
        if slot is None:
            # Capacity checked BEFORE any fetch — no bandwidth spent on a
            # file the §7 cap would refuse. Downloads are synchronous, so
            # the slot stays free until the write below.
            self.record_outcome(
                entry, status=Status.SKIPPED, reason=f"media cap ({MEDIA_MAX_FILES} files) reached"
            )
            return
        if self._media_oversize_by_head(entry.url):
            self.record_outcome(
                entry,
                status=Status.SKIPPED,
                reason="media exceeds 10MB ceiling (declared Content-Length)",
            )
            return
        try:
            response = self.ctx.transport(entry.url)
        except OSError as e:
            failure = classify_connection(e)
            self._media_failure(entry, prior, failure.status, failure.reason)
            return
        if not response.ok:
            failure = classify_http(response.status)
            self._media_failure(entry, prior, failure.status, failure.reason)
            return
        if len(response.body) > MEDIA_MAX_BYTES:
            # Backstop for servers that lie about (or omit) Content-Length.
            self.record_outcome(entry, status=Status.SKIPPED, reason="media exceeds 10MB ceiling")
            return
        out = self.ctx.instance.enrichment_dir / entry.item / f"media-{slot}.{_ext_of(entry.url)}"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(response.body)
        self.outcomes.setdefault(entry.item, _ItemOutcome()).media += 1
        self.record_outcome(
            entry, status=Status.DONE, path=str(out.relative_to(self.ctx.instance.root))
        )

    def _media_oversize_by_head(self, url: str) -> bool:
        """One HEAD before the GET: refuse a declared-oversize body unfetched."""
        try:
            probe = self.ctx.transport(url, method="HEAD")
        except OSError:
            return False  # inconclusive — the GET will surface the truth
        return probe.ok and (probe.content_length or 0) > MEDIA_MAX_BYTES

    def _media_failure(
        self, entry: LedgerEntry, prior: LedgerEntry | None, status: Status, reason: str
    ) -> None:
        match status:
            case Status.BLOCKED:
                base = dataclasses.replace(entry, attempts=(prior.attempts if prior else None))
                self._apply_blocked(base, reason)
            case Status.MANUAL | Status.DEAD:
                self.record_outcome(entry, status=status, reason=reason)
            case _:
                # The classifier returns blocked/manual/dead only — anything
                # else is an engine bug, not a quiet default.
                raise RuntimeError(f"classifier returned unexpected status {status!r}")

    def _media_slot(self, item_id: str) -> int | None:
        """The next free media index for the item, or None at the §7 cap."""
        item_dir = self.ctx.instance.enrichment_dir / item_id
        taken = {
            int(path.stem.split("-")[1])
            for path in item_dir.glob("media-*.*")
            if path.stem.split("-")[1].isdigit()
        }
        for slot in range(MEDIA_MAX_FILES):
            if slot not in taken:
                return slot
        return None

    # -- children (§1/§10) -----------------------------------------------

    def _admit_children(self, parent: LedgerEntry, children: list[Child]) -> None:
        for child in children:
            canonical, unit_hash = self.identify(child.url)
            if unit_hash in self.entries:
                continue  # dedupe by hash (§5 recorded edge)
            depth = (parent.depth or 0) + 1
            cap_reason = self._cap_fired(parent.item, depth)
            detection = (
                None if cap_reason else detect(child.url, self.ctx.drivers, sniff=self.sniff)
            )
            entry = LedgerEntry(
                hash=unit_hash,
                url=canonical,
                item=parent.item,
                kind=detection.kind if detection else detect_kind(child.url, self.ctx.drivers),
                format=detection.format if detection else None,
                status=Status.SKIPPED if cap_reason else Status.QUEUED,
                engine="seed",
                date=datetime.date.min,
                via=child.via,
                parent=parent.hash,
                depth=depth,
                reason=cap_reason,
            )
            self.record(entry)
            if cap_reason is None:
                self.queue.append(unit_hash)

    def _cap_fired(self, item_id: str, depth: int) -> str | None:
        """The §1 cap check: recorded in the ledger, never user-surfaced."""
        if depth > MAX_DEPTH:
            return f"depth cap ({MAX_DEPTH}) reached"
        if self.fetched_count(item_id) >= MAX_URLS_PER_ITEM:
            return f"url cap ({MAX_URLS_PER_ITEM} per item) reached"
        return None

    def fetched_count(self, item_id: str) -> int:
        """Fetched-page entries for the item: media and cap-skips don't count."""
        return sum(
            1
            for entry in self.entries.values()
            if entry.item == item_id and entry.via != "media" and entry.status is not Status.SKIPPED
        )

    # -- recording -------------------------------------------------------

    def record(self, entry: LedgerEntry, *, count: bool = False) -> None:
        stamped = ledger.stamp(entry, today=self.ctx.today, engine_version=self.ctx.engine_version)
        ledger.append(self.ctx.instance.ledger_path, stamped)
        self.entries[stamped.hash] = stamped
        if count:
            self.counts[stamped.status] = self.counts.get(stamped.status, 0) + 1
        if count and stamped.status in _PARKED:
            self.parked.append(
                {
                    "item": stamped.item,
                    "url": stamped.url,
                    "status": stamped.status.value,
                    "reason": stamped.reason
                    or stamped.error
                    or (f"waiting on {stamped.needs}" if stamped.needs else "no reason recorded"),
                }
            )

    def record_outcome(  # noqa: PLR0913 — one keyword per §5 schema slot, all optional
        self,
        entry: LedgerEntry,
        *,
        status: Status,
        needs: Need | None = None,
        attempts: int | None = None,
        path: str | None = None,
        title: str | None = None,
        error: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Write the outcome for a drained entry, provenance carried forward."""
        self.record(
            LedgerEntry(
                hash=entry.hash,
                url=entry.url,
                item=entry.item,
                kind=entry.kind,
                format=entry.format,
                status=status,
                needs=needs,
                attempts=attempts,
                engine="seed",  # stamped in _record
                date=datetime.date.min,
                via=entry.via,
                parent=entry.parent,
                depth=entry.depth,
                rerun=entry.rerun,
                path=path,
                title=title,
                error=error,
                reason=reason,
            ),
            count=True,
        )

    # -- item frontmatter (derived from disk, written by corpus.py) ------

    def _refresh_items(self) -> None:
        for item_id in self.outcomes:
            path = self.item_paths.get(item_id)
            if path is None:
                continue
            item = corpus.read_item(path)
            files = sorted(
                p.name for p in (self.ctx.instance.enrichment_dir / item_id).glob("*.md")
            )
            updated = dataclasses.replace(
                item, status="enriched" if files else "raw", enrichment=files
            )
            if updated != item:
                corpus.write_item(path, updated)

    # -- report ----------------------------------------------------------

    def report_payload(self) -> dict[str, object]:
        items = [
            {"id": item_id, "reason": outcome.reason()}
            for item_id, outcome in sorted(self.outcomes.items())
            if outcome.reason()
        ]
        return {
            "counts": {status.value: n for status, n in self.counts.items()},
            "items": items,
            "parked": self.parked,
        }


# ---------------------------------------------------------------------------
# Verbs (called by the CLI; zero business logic lives there, §14).
# ---------------------------------------------------------------------------


def run(ctx: RunContext, *, limit: int | None = None) -> str:
    """One full run: seed from the corpus, drain, report (§1).

    Args:
        ctx: The run context.
        limit: Optional cap on units processed this run (big cohorts drain
            across runs, §12).

    Returns:
        The rendered enrich-report.
    """
    drain = _Drain(ctx=ctx)
    drain.seed_from_corpus()
    drain.drain(limit=limit)
    return surfaces.render("enrich-report", drain.report_payload())


def fetch_urls(
    ctx: RunContext,
    item_id: str,
    urls: Sequence[str],
    *,
    parent: str | None = None,
    force: bool = False,
) -> str:
    """Fetch specific extra URLs into an existing item, ledgered as children (§10).

    ``parent`` defaults to the item's primary work unit; depth is the
    parent's + 1; fetches count against the item's 12-URL cap. ``--force``
    may exceed the cap for an owner-requested deepen — the cap fire is still
    recorded (a superseded skipped line in the audit trail).

    Args:
        ctx: The run context.
        item_id: The owning corpus item id.
        urls: The URLs to fetch.
        parent: Overriding parent work-unit hash.
        force: Exceed the URL cap deliberately.

    Returns:
        The rendered enrich-report for the fetched units.

    Raises:
        ValueError: Unknown item, or no parent unit is derivable.
    """
    drain = _Drain(ctx=ctx)
    drain.seed_from_corpus()  # loads item paths and keeps seeds honest
    if item_id not in drain.item_paths:
        raise ValueError(f"unknown corpus item {item_id!r}")
    parent_entry = _resolve_parent(drain, item_id, parent)
    targets: set[str] = set()
    for url in urls:
        unit_hash = _admit_fetch(drain, item_id, url, parent_entry, force=force)
        if unit_hash is not None:
            targets.add(unit_hash)
    drain.drain(only=targets)
    return surfaces.render("enrich-report", drain.report_payload())


def _resolve_parent(drain: _Drain, item_id: str, parent: str | None) -> LedgerEntry:
    if parent is not None:
        entry = drain.entries.get(parent)
        if entry is None:
            raise ValueError(f"--parent {parent!r} is not a ledger entry")
        return entry
    for entry in drain.entries.values():
        if entry.item == item_id and (entry.depth or 0) == 0:
            return entry
    raise ValueError(f"item {item_id!r} has no primary work unit — pass --parent <hash> explicitly")


def _admit_fetch(
    drain: _Drain, item_id: str, url: str, parent: LedgerEntry, *, force: bool
) -> str | None:
    canonical, unit_hash = drain.identify(url)
    existing = drain.entries.get(unit_hash)
    capped = drain.fetched_count(item_id) >= MAX_URLS_PER_ITEM
    depth = (parent.depth or 0) + 1
    if capped:
        # The cap fire is recorded either way; --force supersedes it with a
        # queued line (the skipped line stays in the audit trail, §10).
        reason = (
            f"url cap ({MAX_URLS_PER_ITEM} per item) exceeded by --force"
            if force
            else f"url cap ({MAX_URLS_PER_ITEM} per item) reached — rerun with --force to exceed"
        )
        drain.record(
            LedgerEntry(
                hash=unit_hash,
                url=canonical,
                item=item_id,
                kind=detect_kind(url, drain.ctx.drivers),
                status=Status.SKIPPED,
                engine="seed",
                date=datetime.date.min,
                via="harvest",
                parent=parent.hash,
                depth=depth,
                reason=reason,
            ),
            count=not force,
        )
        if not force:
            return None
    detection = detect(url, drain.ctx.drivers, sniff=drain.sniff)
    rerun = existing is not None and existing.path is not None
    drain.record(
        LedgerEntry(
            hash=unit_hash,
            url=canonical,
            item=item_id,
            kind=detection.kind,
            format=detection.format,
            status=Status.QUEUED,
            engine="seed",
            date=datetime.date.min,
            via="harvest",
            parent=parent.hash,
            depth=depth,
            rerun=rerun,
        )
    )
    return unit_hash


def status_report(ctx: RunContext) -> str:
    """The ledger summary plus the enrichment-newer-than-digest backstop (§1)."""
    entries = ledger.load(ctx.instance.ledger_path)
    counts: dict[str, int] = {}
    waiting: dict[str, int] = {}
    for entry in entries.values():
        counts[entry.status.value] = counts.get(entry.status.value, 0) + 1
        if entry.status is Status.WAITING and entry.needs is not None:
            waiting[entry.needs.value] = waiting.get(entry.needs.value, 0) + 1
    payload: dict[str, object] = {"counts": counts}
    if waiting:
        payload["waiting"] = waiting
    orphans = _digest_orphans(ctx.instance)
    if orphans:
        payload["orphans"] = orphans
    return surfaces.render("status", payload)


def _digest_orphans(instance: Instance) -> list[str]:
    """Items whose enrichment is newer than their digest — the backstop (§1)."""
    orphans = []
    if not instance.enrichment_dir.is_dir():
        return orphans
    for item_dir in sorted(instance.enrichment_dir.iterdir()):
        if not item_dir.is_dir():
            continue
        outputs = list(item_dir.glob("*.md"))
        if not outputs:
            continue
        digest = instance.digests_dir / f"{item_dir.name}.md"
        if not digest.exists():
            orphans.append(item_dir.name)
            continue
        newest = max(path.stat().st_mtime for path in outputs)
        if newest > digest.stat().st_mtime:
            orphans.append(item_dir.name)
    return orphans


def mark(
    ctx: RunContext, url: str, status: Status, *, reason: str | None = None, path: str | None = None
) -> str:
    """Heal one ledger entry through the sanctioned verb (§5/§14).

    Args:
        ctx: The run context.
        url: The unit's URL (canonicalized here).
        status: The corrected status.
        reason: The stated reason (required for manual/skipped, §5).
        path: Output path, for done heals that wrote a file.

    Returns:
        A one-line confirmation.

    Raises:
        ValueError: No ledger entry exists for the URL, ``error`` was
            requested (errors are engine outcomes, not heals), or the
            status/field combination violates the §5 schema.
    """
    if status is Status.ERROR:
        raise ValueError("mark cannot write 'error' — errors are engine outcomes, not heals")
    drain = _Drain(ctx=ctx)
    canonical, unit_hash = drain.identify(url)
    prior = drain.entries.get(unit_hash)
    if prior is None:
        raise ValueError(f"no ledger entry for {canonical!r} — mark heals existing state")
    healed = LedgerEntry(
        hash=prior.hash,
        url=prior.url,
        item=prior.item,
        kind=prior.kind,
        format=prior.format,
        status=status,
        needs=prior.needs if status is Status.WAITING else None,
        attempts=max(prior.attempts or 0, 1) if status is Status.BLOCKED else None,
        engine="seed",  # stamped in _record
        date=datetime.date.min,
        via=prior.via,
        parent=prior.parent,
        depth=prior.depth,
        rerun=prior.rerun,
        path=path,
        error=None,
        reason=reason,
    )
    drain.record(healed)
    return f"marked {canonical} ({unit_hash}) {status.value}"


def record_pass(ctx: RunContext, item_id: str, stage: str) -> str:
    """Record a stage completion in ``state/passes.jsonl`` (§4).

    "Ran and promoted nothing" must be distinguishable from "never ran" —
    hence a record even when a pass changed nothing.

    Args:
        ctx: The run context.
        item_id: The corpus item the pass covered.
        stage: One of harvest / digest / wiki.

    Returns:
        A one-line confirmation.

    Raises:
        ValueError: Unknown stage.
    """
    if stage not in _PASS_STAGES:
        options = ", ".join(sorted(_PASS_STAGES))
        raise ValueError(f"stage must be one of {options}, got {stage!r}")
    record: dict[str, str | int] = {
        "stage": stage,
        "item": item_id,
        "date": ctx.today().isoformat(),
    }
    if stage == "harvest":
        record["rules"] = HARVEST_RULES_VERSION
    path = ctx.instance.passes_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return f"recorded {stage} pass for {item_id}"


def compact(ctx: RunContext) -> str:
    """Compact the ledger to the latest line per hash (§4)."""
    removed = ledger.compact(ctx.instance.ledger_path)
    noun = "line" if removed == 1 else "lines"
    return f"compacted: {removed} superseded {noun} removed"


# ---------------------------------------------------------------------------
# Enrichment file rendering.
# ---------------------------------------------------------------------------

_YAML_UNSAFE = ":#[]{}&*!|>%@`\"'"


def _render_enrichment(
    url: str, fetched: datetime.date, meta: dict[str, str | int | None], body: str
) -> str:
    lines = ["---", f"url: {url}", f"fetched: {fetched.isoformat()}"]
    for key, value in meta.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {_yaml_value(value)}")
    lines.extend(["---", "", body.strip(), ""])
    return "\n".join(lines)


def _yaml_value(value: str | int) -> str:
    if isinstance(value, int):
        return str(value)
    needs_quoting = (
        value != value.strip() or "\n" in value or any(ch in value for ch in _YAML_UNSAFE)
    )
    return json.dumps(value, ensure_ascii=False) if needs_quoting else value


def _mask_fetched(content: str) -> str:
    """Drop the ``fetched:`` stamp so rerun byte-compares see real change only.

    Only the frontmatter head is masked — a body line that happens to start
    with ``fetched:`` is content and must count as change.
    """
    head, sep, rest = content.partition("\n---\n")
    masked = "\n".join(line for line in head.split("\n") if not line.startswith("fetched: "))
    return masked + sep + rest


def _ext_of(url: str) -> str:
    tail = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    ext = tail.rsplit(".", 1)[-1].lower() if "." in tail else ""
    if not ext or len(ext) > 4 or not ext.isalnum():  # noqa: PLR2004 — 4-char extension heuristic
        return "jpg"
    return ext
