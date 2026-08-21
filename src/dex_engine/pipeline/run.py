"""The orchestrator: seed, drain, write, re-enter — and report verbatim.

The ledger is the work queue. A run seeds queued entries for new corpus
URLs, drains everything :func:`is_drainable`, hands units to drivers, writes
deterministically named outputs (reruns overwrite, never duplicate),
downloads media, re-enters children with provenance and caps, and
renders its report through the ``enrich-report`` surface.

Exactly ONE ``except Exception`` exists in the whole pipeline: the per-unit
loop here. Every ledger write goes through ``ledger.stamp`` with the
injected clock and engine version.
"""

import dataclasses
import datetime
import json
import re
import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import assert_never

from dex_engine import corpus
from dex_engine.capabilities import Capabilities
from dex_engine.drivers.transport import Transport, urllib_transport
from dex_engine.render import surfaces

from . import issues, ledger
from .classify import (
    Classification,
    ProviderInputError,
    ProviderUnavailableError,
    classify_connection,
    classify_http,
    scrub,
)
from .detect import Sniff, canonical_url, detect, detect_kind, sniff_format
from .registry import driver_for
from .transcribe import (
    TRANSCRIBE_RUN_CAP,
    Acquired,
    DownloadAudio,
    acquire_podcast_audio,
    acquire_youtube_audio,
    podcast_body,
    youtube_body,
    yt_dlp_audio,
)
from .types import (
    Availability,
    Child,
    Config,
    Format,
    Instance,
    Kind,
    LedgerEntry,
    MediaFetch,
    Need,
    Redetection,
    Result,
    SourceDriver,
    Status,
    Transcriber,
    WorkUnit,
    version_newer,
)
from .urls import resolve_repo_path, work_hash

__all__ = [
    "HARVEST_RULES_VERSION",
    "MAX_BLOCKED_ATTEMPTS",
    "MAX_DEPTH",
    "MAX_URLS_PER_ITEM",
    "MEDIA_MAX_BYTES",
    "MEDIA_MAX_FILES",
    "RERUN_DRAIN_CAP",
    "RunContext",
    "digest_orphans",
    "fetch_urls",
    "head_sniffer",
    "is_drainable",
    "mark",
    "no_providers",
    "record_pass",
    "run",
    "run_transcribe",
    "status_report",
]

# Re-entry caps: mechanical backstops, not targets. Fires are recorded
# in the ledger (a skipped entry with the reason) — never user-surfaced.
MAX_DEPTH = 4
MAX_URLS_PER_ITEM = 12

# Blocked retries every run; the 5th failed attempt escalates to manual.
MAX_BLOCKED_ATTEMPTS = 5

# Rerun entries (migration reseeds and other rerun:true requeues) drain at
# most this many per full run, AFTER all fresh work — automated pacing so a
# thousand-item reseed never starves new captures or monopolizes a machine.
# The remainder stays queued and drains across subsequent runs.
RERUN_DRAIN_CAP = 50

# Media stage.
MEDIA_MAX_FILES = 4
MEDIA_MAX_BYTES = 10 * 1024 * 1024

# Bumped when the harvest rules change; recorded in passes.jsonl.
HARVEST_RULES_VERSION = 1

_PARKED = frozenset({Status.WAITING, Status.BLOCKED, Status.ERROR, Status.MANUAL})
_PASS_STAGES = frozenset({"harvest", "digest", "wiki"})

# A failed audio download takes the normal blocked lifecycle (acquisition
# failures are NOT provider failures — never no-clock waiting). The blocked
# line cannot carry `needs` (waiting-only), so this reason prefix is the
# marker that routes the retry back to the transcribe path — the one place
# it is written and matched.
_ACQUISITION_FAILED_PREFIX = "audio acquisition failed: "


def _is_transcribe_job(entry: LedgerEntry) -> bool:
    """Transcribe-drain work: a waiting park, or a blocked acquisition retry."""
    if entry.status is Status.WAITING and entry.needs is Need.TRANSCRIBE:
        return True
    return entry.status is Status.BLOCKED and (entry.reason or "").startswith(
        _ACQUISITION_FAILED_PREFIX
    )


def no_providers(need: Need, fmt: Format | None = None) -> Availability:  # noqa: ARG001 — the null seam ignores its inputs
    """The null provider seam: nothing mechanical is wired (tests, bare contexts).

    The real seam is ``Capabilities.available`` — the CLI wires it.
    """
    return Availability(ok=False, reason=f"no mechanical '{need}' provider wired")


@dataclass(frozen=True, slots=True, kw_only=True)
class RunContext:
    """Everything a run needs, constructor-injected — no ambient state.

    ``provider_available`` is the drain predicate's seam and
    ``capabilities`` the registry the transcribe path calls; the CLI wires
    both to the same :class:`~dex_engine.capabilities.Capabilities` so they
    cannot disagree, and tests may fake either independently.
    """

    instance: Instance
    config: Config
    drivers: Sequence[SourceDriver]
    today: Callable[[], datetime.date]
    engine_version: str
    transport: Transport = urllib_transport
    provider_available: Callable[[Need, Format | None], Availability] = no_providers
    capabilities: Capabilities | None = None
    download_audio: DownloadAudio = yt_dlp_audio
    sleep: Callable[[float], None] = time.sleep
    # The issue filer's seams: the gh runner and the CLI command the
    # issue body names. Injected so filing tests are hermetic.
    gh: issues.GhRunner = issues.gh_runner
    command: str = "enrich"


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


# Leading bytes cover every signature ``sniff_format`` checks (its longest
# magic number is 5 bytes; anydoc reads container signatures from the
# head). The one exception is a ZIP container, whose package identity
# lives in the central directory at the END of the file — only that magic
# warrants reading the whole file. Everything else (images, video, audio —
# most of media/, and LFS-scale) sniffs from the prefix alone.
_SNIFF_PREFIX_BYTES = 4096
_ZIP_MAGIC = b"PK\x03\x04"


def _sniff_bytes(path: Path) -> bytes:
    """The bytes format detection needs from ``path`` — bounded, not the file."""
    if not path.is_file():
        return b""
    with path.open("rb") as f:
        prefix = f.read(_SNIFF_PREFIX_BYTES)
    if prefix.startswith(_ZIP_MAGIC) and len(prefix) == _SNIFF_PREFIX_BYTES:
        return path.read_bytes()
    return prefix


def is_drainable(entry: LedgerEntry, ctx: RunContext) -> bool:
    """Whether a run picks ``entry`` up — the drain predicate, total.

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
            return entry.needs is not None and ctx.provider_available(entry.needs, entry.format).ok
        case Status.ERROR:
            try:
                return version_newer(ctx.engine_version, entry.engine)
            except ValueError:
                # An unparseable stored engine (hand-healed line, ancient
                # relic) must not crash queue-building. Drainable once: the
                # retry re-stamps a valid engine, healing the field.
                return True
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
    item_status: dict[str, str] = field(default_factory=dict)
    no_source_items: list[str] = field(default_factory=list)
    # Rerun-cohort pacing bookkeeping (full drains only). The counted set
    # exists because a redetected unit re-enters the live queue and pops
    # twice — one logical unit must spend one slot of the cohort, not two.
    rerun_total: int = 0
    rerun_drained: int = 0
    rerun_counted: set[str] = field(default_factory=set)
    # Hashes kind-corrected THIS run: the once-only guard is per-run state,
    # not ledger history — within a run, a second correction is a ping-pong
    # loop; across runs the world genuinely changes, and a unit may be
    # legitimately re-corrected months later.
    redetected_hashes: set[str] = field(default_factory=set)
    queue: deque[str] = field(default_factory=deque)
    counts: dict[Status, int] = field(default_factory=dict)
    parked: list[dict[str, str]] = field(default_factory=list)
    outcomes: dict[str, _ItemOutcome] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    # Error outcomes captured at the catch site for the issue filer —
    # the ledger keeps only the scrubbed message; the fingerprint needs the
    # traceback while the exception object is still in hand.
    error_events: list[issues.ErrorEvent] = field(default_factory=list)
    sniff: Sniff | None = None
    # Transcription is capped per run so a resurrected backlog never
    # monopolizes a machine (the dedicated verb sets this from --limit).
    # Only attempts that REACH a provider spend it.
    transcribe_budget: int | None = TRANSCRIBE_RUN_CAP
    transcribed: int = 0
    deferred_transcriptions: int = 0

    def __post_init__(self) -> None:
        self.entries = ledger.load(self.ctx.instance.ledger_path)
        self.sniff = head_sniffer(self.ctx.transport)

    # -- seeding ---------------------------------------------------------

    def seed_from_corpus(self) -> None:
        """Every captured URL and media file becomes a ledger entry from birth."""
        for path in sorted(self.ctx.instance.corpus_dir.glob("*/*.md")):
            try:
                item = corpus.read_item(path)
            except corpus.CorpusSchemaError as e:
                # A nonconforming item (hand-healed file, unfinished
                # migration repair) is judgment work: named on the report,
                # skipped this run, never fatal to seeding.
                rel = path.relative_to(self.ctx.instance.root)
                detail = " ".join(str(e).removeprefix(f"{path}: ").split())
                self.notes.append(
                    f"corpus item {rel} is unreadable — {detail} — "
                    "skipped this run; repair the file by hand"
                )
                continue
            self.item_paths[item.id] = path
            self.item_status[item.id] = item.status
            for url in item.urls:
                try:
                    self._seed_url(item.id, url)
                except ValueError as e:
                    # One malformed capture URL parks THAT URL; it never
                    # aborts the run (frontmatter is immutable provenance —
                    # garbage in it is judgment work, hence manual).
                    self.park_bad_seed(item.id, url, e)
            for repo_path in item.media:
                self._seed_media_file(item.id, repo_path)

    def _seed_media_file(self, item_id: str, repo_path: str) -> None:
        """Materialized files feed the pipeline: format detect → extract queue.

        Only document formats become work units — images and unknowns are
        described cognitively at ingest, never queued. An unreadable
        file (un-pulled LFS) still seeds by its extension so the file
        driver can park it with the pull hint rather than it vanishing.
        """
        work_key = f"file:{repo_path}"
        unit_hash = work_hash(work_key)
        if unit_hash in self.entries:
            return
        file_path = resolve_repo_path(self.ctx.instance.root, repo_path)
        if file_path is None:
            # An escaping media path is a bad seed, parked before any read —
            # frontmatter is owner-editable data, never a path to trust.
            self.record(
                LedgerEntry(
                    hash=unit_hash,
                    url=work_key,
                    item=item_id,
                    kind=Kind.FILE,
                    status=Status.MANUAL,
                    engine="seed",  # stamped in record
                    date=datetime.date.min,
                    reason="media path points outside the instance root — heal the capture",
                ),
                count=True,
            )
            return
        fmt = sniff_format(_sniff_bytes(file_path), name=repo_path.rsplit("/", 1)[-1])
        if fmt is None:
            return
        self.record(
            LedgerEntry(
                hash=unit_hash,
                url=work_key,
                item=item_id,
                kind=Kind.FILE,
                format=fmt,
                status=Status.QUEUED,
                engine="seed",  # stamped in record
                date=datetime.date.min,
            )
        )

    def park_bad_seed(
        self, item_id: str, url: str, exc: ValueError, *, what: str = "capture URL"
    ) -> None:
        """Park one unfetchable URL manual — the bad-seed containment."""
        unit_hash = work_hash(url)  # canonicalization may have failed — key on the raw URL
        if unit_hash in self.entries:
            return
        self.record(
            LedgerEntry(
                hash=unit_hash,
                url=url,
                item=item_id,
                kind=Kind.WEB,  # pattern-undetectable; the catch-all's kind
                status=Status.MANUAL,
                engine="seed",  # stamped in record
                date=datetime.date.min,
                reason=f"unfetchable {what}: {scrub(str(exc))}",
            ),
            count=True,
        )

    def _seed_url(self, item_id: str, url: str) -> None:
        canonical, unit_hash = self.identify(url)
        if unit_hash in self.entries:
            return  # includes the two-items dedupe edge: first item won
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
        """Canonical form and work hash — delegation lives in detect."""
        canonical = canonical_url(url, self.ctx.drivers)
        return canonical, work_hash(canonical)

    # -- the loop --------------------------------------------------------

    def drain(self, *, limit: int | None = None, only: set[str] | None = None) -> None:
        """Process every drainable entry (or the ``only`` subset), FIFO.

        The full drain (no ``only`` subset) runs in priority order: all
        fresh (non-rerun) work first, uncapped, then rerun entries up to
        :data:`RERUN_DRAIN_CAP` — the remainder stays queued for the next
        run and the report names the cohort position. An explicit ``limit``
        binds the whole run; the rerun cap binds the rerun slice within it.
        Targeted drains (``only``) are owner-directed — no rerun pacing.
        """
        drainable = [
            (unit_hash, entry)
            for unit_hash, entry in self.entries.items()
            if (only is None or unit_hash in only) and is_drainable(entry, self.ctx)
        ]
        if only is None:
            fresh = [unit_hash for unit_hash, entry in drainable if not entry.rerun]
            reruns = [unit_hash for unit_hash, entry in drainable if entry.rerun]
            self.rerun_total = len(reruns)
            self.queue = deque(fresh + reruns[:RERUN_DRAIN_CAP])
        else:
            self.queue = deque(unit_hash for unit_hash, _ in drainable)
        processed = 0
        while self.queue and (limit is None or processed < limit):
            entry = self.entries[self.queue.popleft()]
            if not is_drainable(entry, self.ctx):
                continue  # superseded while queued
            if not self._process(entry):
                continue  # cap-deferred, untouched — a no-op must not spend the limit
            processed += 1
            if only is None and entry.rerun and entry.hash not in self.rerun_counted:
                self.rerun_counted.add(entry.hash)
                self.rerun_drained += 1
        self._note_rerun_cohort()
        self._refresh_items()

    def _note_rerun_cohort(self) -> None:
        if not self.rerun_total:
            return
        note = f"rerun cohort: {self.rerun_drained} of {self.rerun_total} drained"
        remainder = self.rerun_total - self.rerun_drained
        if remainder:
            note += f"; {remainder} queue for the next run"
        self.notes.append(note)

    def _process(self, entry: LedgerEntry) -> bool:
        """Process one unit; False means a cap-deferred no-op (nothing spent)."""
        if entry.via == "media":
            # The ONE via-routed dispatch: the media stage gives blocked downloads
            # normal retry rules, and via is the only mark they carry.
            self._download_media(entry, prior=entry)
            return True
        transcribe_job = _is_transcribe_job(entry)
        if transcribe_job and not self._transcribe_slot():
            return False  # stays waiting untouched; the deferral is noted once
        try:
            # The try spans ALL per-unit processing:
            # fetch/transcribe, output write, media stage, children
            # admission. An engine bug anywhere in it ledgers `error` — the
            # outcome line supersedes any partial one — never aborts the run.
            if transcribe_job:
                self._transcribe_unit(entry)
            else:
                self._drive_unit(entry)
        except ProviderInputError as e:
            self.record_outcome(entry, status=Status.MANUAL, reason=scrub(str(e)))
        except Exception as e:  # noqa: BLE001 — THE one broad catch in the pipeline
            self.record_outcome(entry, status=Status.ERROR, error=scrub(f"{type(e).__name__}: {e}"))
            self.error_events.append(
                issues.error_event(
                    unit_hash=entry.hash, kind=entry.kind, unit_format=entry.format, exc=e
                )
            )
        return True

    def _drive_unit(self, entry: LedgerEntry) -> None:
        driver = driver_for(entry.kind, self.ctx.drivers)
        if driver is None:
            # Every work-unit kind has a driver in the shipped registry;
            # a partial registry (tests, exotic wiring) parks honestly.
            self.record_outcome(
                entry,
                status=Status.MANUAL,
                reason=f"no driver for kind '{entry.kind}' in this registry",
            )
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
            self._apply(entry, driver.fetch(unit))
        finally:
            # Politeness holds even when the fetch failed.
            self.ctx.sleep(driver.sleep)

    # -- the transcribe drain ----------------------------------------

    def _transcribe_slot(self) -> bool:
        """Whether the per-run transcription cap has room for one more."""
        if self.transcribe_budget is not None and self.transcribed >= self.transcribe_budget:
            if self.deferred_transcriptions == 0:
                self.notes.append(
                    f"transcription capped at {self.transcribe_budget} this run — "
                    "the rest of the waiting cohort drains next run "
                    "(or now: `dex enrich transcribe --limit N`)"
                )
            self.deferred_transcriptions += 1
            return False
        return True

    def _transcribe_unit(self, entry: LedgerEntry) -> None:
        """Drain one transcribe job through the capability.

        Call-time capability failures keep the entry ``waiting`` with the
        stated reason (retried next run — no clock); acquisition failures
        take the blocked lifecycle (attempts, manual at 5);
        confirmed-gone sources and judgment cases park honestly;
        ``ProviderInputError`` propagates for the manual mapping.
        """
        transcriber = self.ctx.capabilities.transcriber() if self.ctx.capabilities else None
        if transcriber is None:
            availability = self.ctx.provider_available(Need.TRANSCRIBE, None)
            self.record_outcome(
                entry, status=Status.WAITING, needs=Need.TRANSCRIBE, reason=availability.reason
            )
            return
        self._note_first_run(transcriber)
        acquired = self._acquire_audio(entry)
        if isinstance(acquired, Classification):
            self._apply_acquisition_failure(entry, acquired)
            return
        # The budget is spent HERE — only an attempt that reaches a
        # provider burns the per-run cap: failed acquisitions and
        # provider-missing passes must not starve the drainable cohort.
        self.transcribed += 1
        try:
            transcript = transcriber.transcribe(acquired.audio, acquired.prompt)
        except ProviderUnavailableError as e:
            # An available() failure discovered at call time — the
            # mechanical provider is, in truth, not available. Audio stays
            # cached (retries don't re-download).
            self.record_outcome(
                entry, status=Status.WAITING, needs=Need.TRANSCRIBE, reason=scrub(str(e))
            )
            return
        except OSError as e:
            self.record_outcome(
                entry,
                status=Status.WAITING,
                needs=Need.TRANSCRIBE,
                reason=f"transcription failed: {classify_connection(e).reason}",
            )
            return
        meta = dict(acquired.meta)
        meta["via"] = transcriber.name  # raw transcript, stamped via/model
        meta["model"] = transcriber.model
        # Total over the transcribable kinds, mirroring _acquire_audio — a
        # kind added to acquisition alone must fail loudly here, never
        # silently take the podcast body.
        match entry.kind:
            case Kind.YOUTUBE:
                body = youtube_body(acquired.prefix, transcript)
            case Kind.PODCAST:
                body = podcast_body(acquired.prefix, transcript)
            case _:
                raise RuntimeError(
                    f"no transcript body for kind '{entry.kind}' — _acquire_audio "
                    "and the body dispatch must cover the same kinds"
                )
        result = Result(status=Status.DONE, meta=meta, body=body)
        path = self._write_output(entry, result)
        title = meta.get("title")
        self.record_outcome(
            entry, status=Status.DONE, path=path, title=title if isinstance(title, str) else None
        )
        # Audio lifecycle: the transcript supersedes the audio — delete on
        # success only; pending/failed audio stays cached for the retry.
        acquired.audio.unlink(missing_ok=True)

    def _note_first_run(self, transcriber: Transcriber) -> None:
        """Surface an ok-with-caveat availability (the slow first run, explained)."""
        availability = transcriber.available()
        if availability.ok and availability.reason:
            note = f"{transcriber.name}: {availability.reason}"
            if note not in self.notes:
                self.notes.append(note)

    def _acquire_audio(self, entry: LedgerEntry) -> Acquired | Classification:
        """Audio acquisition belongs to the drain, per kind."""
        audio_dir = self.ctx.instance.cache_dir / "audio"
        match entry.kind:
            case Kind.YOUTUBE:
                return acquire_youtube_audio(entry, audio_dir, self.ctx.download_audio)
            case Kind.PODCAST:
                name = f"{entry.kind.value}-{entry.hash[:6]}.md"
                enrichment = self.ctx.instance.enrichment_dir / entry.item / name
                return acquire_podcast_audio(entry, enrichment, audio_dir, self.ctx.transport)
            case _:
                return Classification(
                    status=Status.MANUAL,
                    reason=(
                        f"no audio-acquisition path for kind '{entry.kind}' — "
                        "transcription covers youtube and podcast work"
                    ),
                )

    def _apply_acquisition_failure(self, entry: LedgerEntry, failure: Classification) -> None:
        match failure.status:
            case Status.BLOCKED:
                # Acquisition failures are not provider failures — the
                # normal blocked lifecycle applies, attempts and all,
                # escalating manual at 5. The reason prefix routes the
                # retry back here (see _is_transcribe_job).
                self._apply_blocked(entry, f"{_ACQUISITION_FAILED_PREFIX}{failure.reason}")
            case Status.DEAD if entry.kind is Kind.PODCAST:
                # A 404ing enclosure is often just an expired signed URL —
                # the EPISODE is not confirmed gone. Manual, with the
                # re-resolve route stated.
                self.record_outcome(
                    entry,
                    status=Status.MANUAL,
                    reason=(
                        f"audio enclosure gone ({failure.reason}) — requeue the unit so "
                        "the podcast driver re-resolves it, or rescue by hand"
                    ),
                )
            case Status.DEAD | Status.MANUAL:
                self.record_outcome(entry, status=failure.status, reason=failure.reason)
            case _:
                raise RuntimeError(f"unclassifiable acquisition failure {failure.status!r}")

    def _apply(self, entry: LedgerEntry, result: Result) -> None:
        if result.redetect is not None:
            self._apply_redetection(entry, result.redetect)
            return
        match result.status:
            case Status.DONE:
                self._apply_done(entry, result)
            case Status.WAITING:
                if result.body is not None or result.meta.get("enclosure") is not None:
                    # A parking driver may still have real content (a
                    # podcast's show notes, the enclosure pointer in meta) —
                    # written now, completed by the drain; the item is not
                    # yet cognitive work, so the write is not an outcome.
                    # A waiting-transcribe park carrying an enclosure ALWAYS
                    # writes its park file: the drain re-fetches the
                    # audio from that frontmatter pointer, show notes or
                    # not — a no-notes episode without it would loop manual.
                    self._write_output(entry, result, count=False)
                self.record_outcome(
                    entry, status=Status.WAITING, needs=result.needs, reason=result.reason
                )
            case Status.BLOCKED:
                self._apply_blocked(entry, result.reason)
            case Status.DEAD | Status.SKIPPED | Status.MANUAL:
                self.record_outcome(entry, status=result.status, reason=result.reason)
            case Status.QUEUED | Status.ERROR:
                # Result.__post_init__ forbids both here: queued travels
                # only with a redetection (routed above); errors are
                # raised, never returned.
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
        if result.assets:
            self._write_assets(self.entries[entry.hash], result.assets)
        if result.media and self.ctx.config.media_fetch is not MediaFetch.NONE:
            self._media_stage(self.entries[entry.hash], result.media)
        self._admit_children(self.entries[entry.hash], result.children)

    def _apply_redetection(self, entry: LedgerEntry, redetect: Redetection) -> None:
        """Re-route a mid-fetch kind discovery through the queue, once per run.

        The corrected unit has the SAME URL and therefore the same hash — a
        child emission would dedupe against the existing entry and vanish.
        The mechanics are a superseding ledger line instead: same hash,
        corrected kind (+format), ``status: queued``, ``via: "sniff"`` —
        last-per-hash means the unit simply BECOMES the corrected kind and
        drains through its driver in this same run. Once per run: a second
        correction of the same hash within one run is two drivers
        re-detecting each other's content — park for judgment, never
        ping-pong. Across runs a unit may correct again: the world
        genuinely changes, and ``via`` is provenance history, not a lock.

        The previous kind's output is unlinked: the correction supersedes
        it, and leaving it beside the new output would hand the digest
        layer superseded content. The ledger's audit trail preserves the
        history; the disk does not.
        """
        if entry.hash in self.redetected_hashes:
            self.record_outcome(
                entry,
                status=Status.MANUAL,
                reason=(
                    f"re-detection loop — {entry.kind.value} and {redetect.kind.value} "
                    "keep re-detecting each other's content; inspect by hand"
                ),
            )
            return
        self.redetected_hashes.add(entry.hash)
        stale = (
            self.ctx.instance.enrichment_dir
            / entry.item
            / f"{entry.kind.value}-{entry.hash[:6]}.md"
        )
        if entry.kind is not redetect.kind and stale.exists():
            stale.unlink()
        self.record(
            LedgerEntry(
                hash=entry.hash,
                url=entry.url,
                item=entry.item,
                kind=redetect.kind,
                format=redetect.format,
                status=Status.QUEUED,
                engine="seed",  # stamped in record
                date=datetime.date.min,
                via="sniff",
                parent=entry.parent,
                depth=entry.depth,
                rerun=entry.rerun,
            )
        )
        self.queue.append(entry.hash)
        corrected = redetect.kind.value + (f"/{redetect.format.value}" if redetect.format else "")
        self.notes.append(f"re-detected: {entry.url} — {entry.kind.value} → {corrected}")

    def _apply_blocked(self, entry: LedgerEntry, reason: str | None) -> None:
        attempts = (entry.attempts or 0) + 1
        if attempts >= MAX_BLOCKED_ATTEMPTS:
            # Escalation appends attempt context to what the driver knew —
            # it never invents a reason.
            detail = reason or "no reason recorded"
            self.record_outcome(
                entry,
                status=Status.MANUAL,
                reason=f"still blocked after {attempts} attempts — {detail}",
            )
            return
        self.record_outcome(entry, status=Status.BLOCKED, attempts=attempts, reason=reason)

    # -- outputs ---------------------------------------------------------

    def _write_output(self, entry: LedgerEntry, result: Result, *, count: bool = True) -> str:
        """Write ``<kind>-<hash6>.md`` deterministically; byte-compare reruns.

        The ``fetched:`` stamp is masked out of the comparison — it changes
        every run by definition, and an unchanged rerun must not rewrite the
        file (nor report the item as changed). ``count=False`` writes
        without registering an item outcome (a waiting park's partial
        content is not cognitive work yet).
        """
        name = f"{entry.kind.value}-{entry.hash[:6]}.md"
        out = self.ctx.instance.enrichment_dir / entry.item / name
        body = result.body or ""
        content = _render_enrichment(entry.url, self.ctx.today(), result.meta, body)
        existed = out.exists()
        if existed and _mask_fetched(out.read_text(encoding="utf-8")) == _mask_fetched(content):
            return str(out.relative_to(self.ctx.instance.root))
        if count:
            outcome = self.outcomes.setdefault(entry.item, _ItemOutcome())
            if existed:
                outcome.changed += 1
            else:
                outcome.new += 1
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
        return str(out.relative_to(self.ctx.instance.root))

    # -- extraction assets ----------------------------------------

    def _write_assets(self, entry: LedgerEntry, assets: list) -> None:
        """Write embedded assets under the media caps, ledgered via: extract-asset.

        Deterministic names (``<hash6>-asset-<n>.<ext>``) make reruns
        overwrite, never duplicate; the caps (4 files per item, 10MB per
        file) are shared with the media stage's downloads.
        """
        for index, asset in enumerate(assets):
            name = f"{entry.hash[:6]}-asset-{index}.{asset.suggested_ext}"
            out = self.ctx.instance.enrichment_dir / entry.item / name
            rel = f"enrichment/{entry.item}/{name}"
            if len(asset.data) > MEDIA_MAX_BYTES:
                self._asset_outcome(
                    entry, rel, status=Status.SKIPPED, reason="asset exceeds 10MB ceiling"
                )
                continue
            if not out.exists() and self._media_file_count(entry.item) >= MEDIA_MAX_FILES:
                self._asset_outcome(
                    entry,
                    rel,
                    status=Status.SKIPPED,
                    reason=f"media cap ({MEDIA_MAX_FILES} files) reached",
                )
                continue
            changed = not out.exists() or out.read_bytes() != asset.data
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(asset.data)
            if changed:
                self.outcomes.setdefault(entry.item, _ItemOutcome()).media += 1
            self._asset_outcome(entry, rel, status=Status.DONE, path=rel)

    def _asset_outcome(
        self,
        parent: LedgerEntry,
        rel: str,
        *,
        status: Status,
        path: str | None = None,
        reason: str | None = None,
    ) -> None:
        unit_hash = work_hash(rel)
        existing = self.entries.get(unit_hash)
        if existing is not None and existing.status is status and existing.path == path:
            return  # a rerun's unchanged asset line adds nothing to the audit trail
        self.record(
            LedgerEntry(
                hash=unit_hash,
                url=rel,  # embedded assets have no URL — the repo path is the identity
                item=parent.item,
                kind=parent.kind,
                status=status,
                engine="seed",  # stamped in record
                date=datetime.date.min,
                via="extract-asset",
                parent=parent.hash,
                depth=(parent.depth or 0) + 1,
                path=path,
                reason=reason,
            ),
            count=True,
        )

    def _media_file_count(self, item_id: str) -> int:
        """Media-family files for the item — downloads and extraction assets."""
        item_dir = self.ctx.instance.enrichment_dir / item_id
        if not item_dir.is_dir():
            return 0
        return sum(
            1
            for path in item_dir.iterdir()
            if path.is_file()
            and path.suffix != ".md"
            and (path.name.startswith("media-") or "-asset-" in path.name)
        )

    # -- media stage ------------------------------------------------

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
            # The birth line lands BEFORE the download (entries exist from
            # birth) — a crash mid-download leaves a queued via:media entry
            # the next run's redrain path picks up.
            self.record(child)
            self._download_media(self.entries[unit_hash], prior=None)

    def _download_media(self, entry: LedgerEntry, *, prior: LedgerEntry | None) -> None:
        # Cap and oversize skips below land in the report's unit counts —
        # deliberately: they are unit outcomes, unlike the re-entry cap
        # fires, which never surface.
        slot = self._media_slot(entry.item)
        if slot is None:
            # Capacity checked BEFORE any fetch — no bandwidth spent on a
            # file the cap would refuse. Downloads are synchronous, so
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
        """The next free media index for the item, or None at the cap.

        The cap is shared with extraction assets — 4 media-family files per
        item total, whichever route wrote them.
        """
        if self._media_file_count(item_id) >= MEDIA_MAX_FILES:
            return None
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

    # -- children -----------------------------------------------

    def _admit_children(self, parent: LedgerEntry, children: list[Child]) -> None:
        for child in children:
            canonical, unit_hash = self.identify(child.url)
            if unit_hash in self.entries:
                continue  # dedupe by hash — a URL under two items enriches under the first
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
        """The re-entry cap check: recorded in the ledger, never user-surfaced."""
        if depth > MAX_DEPTH:
            return f"depth cap ({MAX_DEPTH}) reached"
        if self.fetched_count(item_id) >= MAX_URLS_PER_ITEM:
            return f"url cap ({MAX_URLS_PER_ITEM} per item) reached"
        return None

    def fetched_count(self, item_id: str) -> int:
        """Fetched-page entries for the item — what the 12-URL cap bounds.

        Media downloads, extraction-asset byte-writes, and cap-skip marker
        lines don't count: none of them is a fetched page, and a document
        rich in embedded images must not spend the item's URL budget.
        """
        return sum(
            1
            for entry in self.entries.values()
            if entry.item == item_id
            and entry.via not in ("media", "extract-asset")
            and entry.status is not Status.SKIPPED
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

    def record_outcome(  # noqa: PLR0913 — one keyword per ledger schema slot, all optional
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
        """Reconcile EVERY item — enrichment also lands outside the drain.

        Media descriptions and cognitive heals are session-written files the
        drain never touched; a full sweep converges them all, and the
        write-only-on-change rule keeps untouched items untouched.
        """
        for item_id, path in self.item_paths.items():
            _refresh_item_frontmatter(self.ctx.instance, item_id, path)

    # -- report ----------------------------------------------------------

    def derive_no_source_items(self) -> None:
        """List fresh text/image-only items the unit-driven report cannot see.

        A capture with no URLs and no document media seeds nothing, so it
        never appears among the drained units — yet its description and
        digest are exactly the session's work. Derived on demand, never
        seeded (a fact that shows, not work to queue): a raw item with zero
        ledger units and no digest is cognitive work until digested.
        """
        sourced = {entry.item for entry in self.entries.values()}
        self.no_source_items = [
            item_id
            for item_id, status in sorted(self.item_status.items())
            if status == "raw"
            and item_id not in sourced
            and not (self.ctx.instance.digests_dir / f"{item_id}.md").exists()
        ]

    def report_payload(self) -> dict[str, object]:
        items = [
            {"id": item_id, "reason": outcome.reason()}
            for item_id, outcome in sorted(self.outcomes.items())
            if outcome.reason()
        ]
        items += [
            {"id": item_id, "reason": "no-source item — awaiting description + digest"}
            for item_id in self.no_source_items
        ]
        payload: dict[str, object] = {
            "counts": {status.value: n for status, n in self.counts.items()},
            "items": items,
            "parked": self.parked,
        }
        cognitive = self._cognitive_jobs()
        if cognitive:
            payload["cognitive"] = cognitive
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload

    def _cognitive_jobs(self) -> list[dict[str, str]]:
        """Waiting jobs that resolve to the cognitive floor.

        Listed on the report for the session — never drained by the run.
        A waiting transcribe job is NOT one of these: transcription has no
        cognitive floor, it waits for a mechanical provider.
        """
        capabilities = self.ctx.capabilities
        if capabilities is None:
            return []
        return [
            {"item": entry.item, "url": entry.url, "need": entry.needs.value}
            for entry in self.entries.values()
            if entry.status is Status.WAITING
            and entry.needs is not None
            and capabilities.is_cognitive(entry.needs, entry.format)
        ]


def _refresh_item_frontmatter(instance: Instance, item_id: str, path: Path | None = None) -> None:
    """Derive one item's ``status``/``enrichment:`` from disk; write only on change.

    Silent when the corpus file is gone or unreadable: a heal may outlive
    its item (excluded after capture), an unreadable item is seeding's to
    name — and the ledger write this follows must stand either way.
    """
    if path is None:
        path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    try:
        item = corpus.read_item(path)
    except (OSError, corpus.CorpusSchemaError):
        return
    files = sorted(p.name for p in (instance.enrichment_dir / item_id).glob("*.md"))
    updated = dataclasses.replace(item, status="enriched" if files else "raw", enrichment=files)
    if updated != item:
        corpus.write_item(path, updated)


# ---------------------------------------------------------------------------
# Verbs (called by the CLI; zero business logic lives there).
# ---------------------------------------------------------------------------


def _finish(drain: _Drain) -> str:
    """File this run's errors upstream, then render the enrich-report.

    The filer runs after the drain — it fires only on ``status: error``
    outcomes already ledgered — and its notes/failures land on the same
    report, so the human always knows what was reported.
    """
    ctx = drain.ctx
    outcome = issues.report_errors(
        drain.error_events,
        enabled=ctx.config.report_issues,
        state_dir=ctx.instance.state_dir,
        engine_version=ctx.engine_version,
        command=ctx.command,
        today=ctx.today,
        gh=ctx.gh,
    )
    drain.notes.extend(outcome.notes)
    payload = drain.report_payload()
    if outcome.filed:
        payload["issues_filed"] = outcome.filed
    return surfaces.render("enrich-report", payload)


def run(ctx: RunContext, *, limit: int | None = None) -> str:
    """One full run: seed from the corpus, drain, report.

    Big rerun cohorts pace themselves: fresh work drains first, then at
    most :data:`RERUN_DRAIN_CAP` rerun entries — no ``--limit`` babysitting
    needed, and the report states the cohort position.

    Args:
        ctx: The run context.
        limit: Optional cap on TOTAL units processed this run (the rerun
            cap still binds the rerun slice within it).

    Returns:
        The rendered enrich-report.
    """
    drain = _Drain(ctx=ctx)
    drain.seed_from_corpus()
    drain.drain(limit=limit)
    drain.derive_no_source_items()
    return _finish(drain)


def run_transcribe(ctx: RunContext, *, limit: int = TRANSCRIBE_RUN_CAP) -> str:
    """Drain the waiting/transcribe cohort through the capability.

    Args:
        ctx: The run context (``--model`` overrides arrive already built
            into ``ctx.capabilities``).
        limit: Per-run cap on attempts that REACH a provider — default 10,
            so a resurrected backlog never monopolizes a machine.
            Failed acquisitions and provider-missing passes don't burn it.

    Returns:
        The rendered enrich-report for the drained units.
    """
    drain = _Drain(ctx=ctx)
    # The dedicated verb is bounded by --limit alone — wired through the
    # same budget the full run uses, so both count provider-reaching
    # attempts only.
    drain.transcribe_budget = limit
    drain.seed_from_corpus()
    availability = ctx.provider_available(Need.TRANSCRIBE, None)
    if not availability.ok:
        drain.notes.append(f"no transcription provider available — {availability.reason}")
        return _finish(drain)
    targets = {
        unit_hash for unit_hash, entry in drain.entries.items() if _is_transcribe_job(entry)
    }
    drain.drain(only=targets)
    return _finish(drain)


def fetch_urls(
    ctx: RunContext,
    item_id: str,
    urls: Sequence[str],
    *,
    parent: str | None = None,
    force: bool = False,
) -> str:
    """Fetch specific extra URLs into an existing item, ledgered as children.

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
    return _finish(drain)


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


# Cap-refusal reasons open with these — the one place they are matched.
_CAP_REASON_PREFIXES = ("url cap (", "depth cap (")


def _is_cap_refusal(entry: LedgerEntry) -> bool:
    """True for a cap-fire marker line — refused work, not an admitted unit."""
    return entry.status is Status.SKIPPED and (entry.reason or "").startswith(_CAP_REASON_PREFIXES)


def _requeue_in_place(drain: _Drain, existing: LedgerEntry) -> str:
    """Re-fetch a unit the item already owns: same depth/parent/via, rerun.

    Never re-parents (re-fetching the item's primary URL must not nest it
    under itself) and never re-spends the URL cap — the unit is already
    counted.
    """
    requeued = dataclasses.replace(
        existing,
        status=Status.QUEUED,
        needs=None,
        attempts=None,
        path=None,
        title=None,
        error=None,
        reason=None,
        rerun=True,
    )
    drain.record(requeued)
    return existing.hash


def _admit_fetch(
    drain: _Drain, item_id: str, url: str, parent: LedgerEntry, *, force: bool
) -> str | None:
    try:
        canonical, unit_hash = drain.identify(url)
    except ValueError as e:
        # An uncanonicalizable URL is the same bad-seed class the corpus
        # seeding path parks — one bad URL never aborts the batch.
        drain.park_bad_seed(item_id, url, e, what="fetch URL")
        return None
    existing = drain.entries.get(unit_hash)
    if existing is not None:
        if existing.item != item_id:
            raise ValueError(
                f"{canonical} already enriches under item {existing.item!r} — one URL "
                f"enriches under one item; it cannot be fetched into {item_id!r}"
            )
        if _is_cap_refusal(existing):
            # A cap-refusal marker is not an admitted unit: it falls through
            # to the cap logic below, so a repeat fetch without --force is
            # refused AGAIN rather than sneaking in as a "rerun".
            pass
        else:
            return _requeue_in_place(drain, existing)
    capped = drain.fetched_count(item_id) >= MAX_URLS_PER_ITEM
    depth = (parent.depth or 0) + 1
    if capped:
        # The cap fire is recorded either way; --force supersedes it with a
        # queued line (the skipped line stays in the audit trail).
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
    try:
        detection = detect(url, drain.ctx.drivers, sniff=drain.sniff)
    except ValueError as e:
        # The sniff HEAD refuses non-http(s) URLs; park the URL rather
        # than abort a batch whose earlier URLs are already ledgered.
        drain.park_bad_seed(item_id, url, e, what="fetch URL")
        return None
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
        )
    )
    return unit_hash


def status_report(ctx: RunContext, *, item_id: str | None = None) -> str:
    """Ledger summary, digest backstop, and the capability report.

    With ``item_id``, renders that item's ledger view instead: every unit
    the item owns, with status, provenance, and outputs. "What was fetched
    for this item" is a ledger query — promoted URLs never live in corpus
    frontmatter.
    """
    entries = ledger.load(ctx.instance.ledger_path)
    if item_id is not None:
        return _item_status(entries, item_id)
    counts: dict[str, int] = {}
    waiting: dict[str, int] = {}
    for entry in entries.values():
        counts[entry.status.value] = counts.get(entry.status.value, 0) + 1
        if entry.status is Status.WAITING and entry.needs is not None:
            waiting[entry.needs.value] = waiting.get(entry.needs.value, 0) + 1
    payload: dict[str, object] = {"counts": counts}
    if waiting:
        payload["waiting"] = waiting
    orphans = digest_orphans(ctx.instance)
    if orphans:
        payload["orphans"] = orphans
    report = surfaces.render("status", payload)
    if ctx.capabilities is not None:
        report += "\n" + surfaces.render("capability-report", ctx.capabilities.report_payload())
    return report


def _item_status(entries: dict[str, LedgerEntry], item_id: str) -> str:
    units: list[dict[str, object]] = []
    for entry in entries.values():
        if entry.item != item_id:
            continue
        unit: dict[str, object] = {"url": entry.url, "status": entry.status.value}
        if entry.via is not None:
            unit["via"] = entry.via
        if entry.depth is not None:
            unit["depth"] = entry.depth
        if entry.needs is not None:
            unit["needs"] = entry.needs.value
        if entry.reason is not None:
            unit["reason"] = entry.reason
        elif entry.error is not None:
            unit["reason"] = entry.error
        if entry.path is not None:
            unit["path"] = entry.path
        units.append(unit)
    return surfaces.render("item-status", {"item": item_id, "units": units})


def digest_orphans(instance: Instance) -> list[str]:
    """Items whose enrichment is newer than their digest — the backstop.

    Shared by ``enrich status`` and lint's health check: both list the same
    interrupted-session orphans, computed in one place.

    Args:
        instance: The instance.

    Returns:
        Item ids whose enrichment outputs are newer than their digest (or
        that have outputs and no digest at all).
    """
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


def mark(  # noqa: PLR0913 — the verb mirrors its CLI flags
    ctx: RunContext,
    url: str,
    status: Status,
    *,
    reason: str | None = None,
    path: str | None = None,
    needs: Need | None = None,
) -> str:
    """Heal one ledger entry through the sanctioned verb.

    A heal never erases what it does not correct: done heals carry the
    prior entry's path/title forward unless a new ``path`` overrides them.
    The owning item's derived frontmatter is refreshed in the same call, so
    a hand-written enrichment file is listed the moment its heal lands.

    Args:
        ctx: The run context.
        url: The unit's URL (canonicalized here).
        status: The corrected status.
        reason: The stated reason (required for manual/skipped).
        path: Output path, for done heals that wrote a file.
        needs: The needed capability, for waiting heals (defaults to the
            prior entry's).

    Returns:
        A one-line confirmation.

    Raises:
        ValueError: No ledger entry exists for the URL, ``error`` was
            requested (errors are engine outcomes, not heals), or the
            status/field combination violates the ledger schema.
    """
    if status is Status.ERROR:
        raise ValueError("mark cannot write 'error' — errors are engine outcomes, not heals")
    if reason is not None:
        # Session-typed reasons arrive with tabs and newlines; the ledger
        # schema is single-line, so the verb normalizes exactly as the
        # scrubber does for engine-produced reasons.
        reason = " ".join(reason.split()) or None
    drain = _Drain(ctx=ctx)
    canonical, unit_hash = drain.identify(url)
    prior = drain.entries.get(unit_hash)
    if prior is None:
        raise ValueError(f"no ledger entry for {canonical!r} — mark heals existing state")
    effective_path = path if path is not None else (prior.path if status is Status.DONE else None)
    healed = LedgerEntry(
        hash=prior.hash,
        url=prior.url,
        item=prior.item,
        kind=prior.kind,
        format=prior.format,
        status=status,
        # A stray --needs on a non-waiting status passes through and fails
        # schema validation loudly rather than being silently dropped.
        needs=(needs or prior.needs) if status is Status.WAITING else needs,
        attempts=max(prior.attempts or 0, 1) if status is Status.BLOCKED else None,
        engine="seed",  # stamped in record
        date=datetime.date.min,
        via=prior.via,
        parent=prior.parent,
        depth=prior.depth,
        rerun=prior.rerun,
        path=effective_path,
        title=prior.title if status is Status.DONE and effective_path is not None else None,
        error=None,
        reason=reason,
    )
    drain.record(healed)
    _refresh_item_frontmatter(ctx.instance, prior.item)
    return f"marked {canonical} ({unit_hash}) {status.value}"


def record_pass(ctx: RunContext, item_id: str, stage: str) -> str:
    """Record a stage completion in ``state/passes.jsonl``.

    "Ran and promoted nothing" must be distinguishable from "never ran" —
    hence a record even when a pass changed nothing. The item's derived
    frontmatter is refreshed in the same call — a pass often follows
    session-written enrichment the engine never saw.

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
    _refresh_item_frontmatter(ctx.instance, item_id)
    return f"recorded {stage} pass for {item_id}"


def compact(ctx: RunContext) -> str:
    """Compact the ledger to the latest line per hash."""
    removed = ledger.compact(ctx.instance.ledger_path)
    noun = "line" if removed == 1 else "lines"
    return f"compacted: {removed} superseded {noun} removed"


# ---------------------------------------------------------------------------
# Enrichment file rendering.
# ---------------------------------------------------------------------------

_YAML_UNSAFE = ":#[]{}&*!|>%@`\"'"

# Values a YAML 1.1 reader (Obsidian's among them) would retype away from
# the intended string: boolean/null words, and int/float lookalikes in
# every 1.1 spelling (sign, underscores, hex/octal/binary, exponent,
# .inf/.nan). Leading `-`/`?`/`,` are indicator characters that make the
# line invalid or restructure it. All are emitted JSON-quoted.
_YAML_KEYWORDS = frozenset({"true", "false", "yes", "no", "on", "off", "null", "~"})
_YAML_NUMBER_RE = re.compile(
    r"[-+]?(?:\.inf|\.nan|0x[0-9a-f_]+|0b[01_]+|0o?[0-7_]+"
    r"|[0-9][0-9_]*(?:\.[0-9_]*)?(?:e[-+]?[0-9]+)?"
    r"|\.[0-9][0-9_]*(?:e[-+]?[0-9]+)?)",
    re.IGNORECASE,
)


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
        value != value.strip()
        or "\n" in value
        or value[:1] in ("-", "?", ",")
        or any(ch in value for ch in _YAML_UNSAFE)
        or value.lower() in _YAML_KEYWORDS
        or _YAML_NUMBER_RE.fullmatch(value) is not None
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
