"""The orchestrator: seed, drain, write, re-enter — and report verbatim.

The ledger is the work queue. A run seeds queued entries for new corpus
URLs, drains everything :func:`is_drainable`, hands units to drivers, writes
deterministically named outputs (reruns overwrite, never duplicate),
downloads media, re-enters children with provenance and caps, and
renders its report through the ``enrich-report`` surface.

Exactly ONE ``except Exception`` exists in the whole pipeline: the per-unit
loop here. Every ledger write goes through ``ledger.stamp`` with the
injected clocks and engine version.
"""

import dataclasses
import datetime
import json
import re
import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import assert_never

from dex_engine import atomic, corpus
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
from .ownership import corpus_claims
from .registry import DRIVERS, driver_for
from .transcribe import (
    TRANSCRIBE_RUN_CAP,
    Acquired,
    DownloadAudio,
    acquire_podcast_audio,
    acquire_youtube_audio,
    podcast_body,
    read_enrichment,
    youtube_body,
    yt_dlp_audio,
)
from .types import (
    Availability,
    Cap,
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
    "CAP_BOUNDS",
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

# Re-entry caps: mechanical backstops, not targets. Fires are recorded in
# the ledger — a skipped entry naming the bound in `cap` — and a harvest-time
# fire is never user-surfaced.
MAX_DEPTH = 4
MAX_URLS_PER_ITEM = 12

# What each bound is called wherever a fire is counted or read. Every
# reader takes its wording from here, so one bound can never read as two.
CAP_BOUNDS: dict[Cap, str] = {
    Cap.DEPTH: f"depth cap ({MAX_DEPTH})",
    Cap.URL: f"url cap ({MAX_URLS_PER_ITEM} per item)",
    Cap.URL_REQUESTED: f"url cap ({MAX_URLS_PER_ITEM} per item)",
}

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
# Work still owed on an item: queued and every parked status. Its complement
# — done, dead, skipped — is a unit that has landed, is confirmed gone, or
# was deliberately closed out; none of the three is owed anything further.
_OUTSTANDING = _PARKED | {Status.QUEUED}
_PASS_STAGES = frozenset({"harvest", "digest", "wiki"})

# Whitespace and control characters anywhere in a URL make the HTTP client
# refuse the request outright (as a ValueError, outside the
# connection-failure lifecycle every other fetch failure travels).
_UNFETCHABLE_CHARS = re.compile(r"[\s\x00-\x1f\x7f]")


def _unfetchable_media(url: str) -> str | None:
    """Why the transport could never fetch ``url``, or None when it can.

    Media URLs are fetched verbatim — they are checked before they are
    ledgered, so a URL that cannot be a request never enters the queue.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "the URL cannot be parsed"
    if parts.scheme not in ("http", "https"):
        return "the scheme is not http(s)"
    if not parts.netloc:
        return "the URL names no host"
    if _UNFETCHABLE_CHARS.search(url):
        return "the URL contains whitespace or control characters"
    return None


def _is_media_file(path: Path) -> bool:
    """Whether ``path`` is one of the item's media-family files.

    Downloads (``media-<n>.<ext>``) and extraction assets
    (``<hash6>-asset-<n>.<ext>``) — the files the 4-per-item cap counts.
    Markdown is never one of them: ``media-<n>.md`` is the session's
    written description of a media capture, and reading it as a file the
    cap counts (or as a slot already taken) let a download land beyond the
    cap, beside a description of something else entirely.
    """
    return (
        path.is_file()
        and path.suffix != ".md"
        and (path.name.startswith("media-") or "-asset-" in path.name)
    )


def _is_transcribe_job(entry: LedgerEntry) -> bool:
    """Transcribe-drain work: a waiting park, or a blocked acquisition retry."""
    return entry.status in (Status.WAITING, Status.BLOCKED) and entry.needs is Need.TRANSCRIBE


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
    # the UTC write-instant clock — ledger lines carry it so two machines'
    # writes for one hash order by write time, not by union-merge order
    now: Callable[[], datetime.datetime]
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
        """What landed, named — "3 new" never said new *what*."""
        parts = [
            f"{self.new} new enrichment {'file' if self.new == 1 else 'files'}"
            if self.new
            else "",
            f"{self.changed} rewritten" if self.changed else "",
            f"{self.media} media {'file' if self.media == 1 else 'files'}" if self.media else "",
        ]
        return ", ".join(part for part in parts if part)


@dataclass(slots=True)
class _Drain:
    """One run's mutable state. Internal to this module."""

    ctx: RunContext
    entries: dict[str, LedgerEntry] = field(default_factory=dict)
    # The last line this run appended per hash — what the ledger must
    # resolve those hashes to when the run reports its outcomes
    # (:func:`_writes_that_did_not_land`).
    written: dict[str, LedgerEntry] = field(default_factory=dict)
    item_paths: dict[str, Path] = field(default_factory=dict)
    item_status: dict[str, str] = field(default_factory=dict)
    # Which live corpus items own each unit — the corpus's answer, never the
    # ledger line's stored string. Rebuilt after the drain, when this run's
    # children exist to be attributed.
    owners: dict[str, tuple[str, ...]] = field(default_factory=dict)
    no_source_items: list[str] = field(default_factory=list)
    # Items this run wrote an outcome for — the report's incompleteness
    # section covers exactly these (an item nothing happened to this run has
    # nothing new to say; `enrich status` holds the standing view).
    touched: set[str] = field(default_factory=set)
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
    parked: list[dict[str, object]] = field(default_factory=list)
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
            except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError) as e:
                # An unreadable item — nonconforming frontmatter, non-UTF-8
                # bytes, a directory or broken symlink the glob matched — is
                # judgment work: named on the report, skipped this run,
                # never fatal to seeding.
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
        self.resolve_owners()
        self._reattribute_dead_items()

    def resolve_owners(self) -> None:
        """Re-read which live corpus item owns each unit (one corpus pass)."""
        self.owners = _unit_owners(self.ctx.instance, self.entries, self.ctx.drivers)

    def _reattribute_dead_items(self) -> None:
        """Move a line naming a dead id onto the live item that owns its work.

        Reading ownership off the corpus (:func:`_unit_owners`) is enough
        for the surfaces, which derive fresh every time — but not for the
        drain, which carries ``entry.item`` forward into the next line and
        into the output path it writes. A renamed item's waiting unit
        drained on the stored string writes its enrichment into
        ``enrichment/<dead-id>/``, so the string itself has to heal, and
        seeding is where the corpus has just been read.

        The status is untouched: a ``manual`` verdict is still ``manual``
        under its new name, exactly as migration 2's re-key pass holds. So
        is the output, once found — a renamed item's enrichment directory
        moves with it, so the recorded path names a directory that is gone
        while the file sits under the new id.

        One status does move. A ``done`` line whose output is nowhere on
        disk is not done for the item that owes it: ``exclude`` keeps the
        line where another live item claims the work, but deletes the
        enrichment along with the item that produced it — leaving the
        survivor's URL ``done`` with nothing to show, and seeding's
        already-a-unit short-circuit means nothing ever fetches it again.
        That one requeues, and the run it re-enters fetches it under the
        item that claims it.

        Idempotent by construction: once the line names a live item the
        first condition stops matching, and once the fetch lands the
        second does too.
        """
        live = {path.stem for path in self.ctx.instance.corpus_dir.glob("*/*.md")}
        moved = 0
        requeued = 0
        for entry in list(self.entries.values()):
            if entry.item in live:
                continue
            owner = next((item for item in self.owners.get(entry.hash, ()) if item in live), None)
            if owner is None:
                continue  # nothing live claims it — lint's ghost-item finding
            # Only a landing has an output to find: on every other status a
            # path is a schema violation, not a repair.
            product = self._unit_product(entry, owner) if entry.status is Status.DONE else None
            lost = entry.status is Status.DONE and entry.path is not None and product is None
            self.record(
                dataclasses.replace(
                    entry,
                    item=owner,
                    status=Status.QUEUED if lost else entry.status,
                    path=None if lost else (product or entry.path),
                    title=None if lost else entry.title,
                )
            )
            moved += 1
            if lost:
                requeued += 1
        if not moved:
            return
        note = (
            f"re-attributed {moved} ledger {'entry' if moved == 1 else 'entries'} to the "
            "live items whose corpus files claim the work"
        )
        if requeued:
            note += (
                f"; {requeued} queued to re-fetch — the enrichment went with the item "
                "that produced it"
            )
        self.notes.append(note)
        self.resolve_owners()

    def _unit_product(self, entry: LedgerEntry, item_id: str) -> str | None:
        """This unit's output on disk — the recorded path, else ``item_id``'s.

        A renamed item's enrichment directory moves with it, so the path
        the line recorded names a directory that is gone while the file
        sits under the new id. The candidate there must PROVE it is this
        unit's by the URL it records, the same test
        :func:`_drop_superseded_outputs` applies before unlinking.
        """
        if entry.path is not None and (self.ctx.instance.root / entry.path).is_file():
            return entry.path
        name = f"{entry.kind.value}-{entry.hash[:6]}.md"
        candidate = self.ctx.instance.enrichment_dir / item_id / name
        if not candidate.is_file():
            return None
        fields, _body = read_enrichment(candidate)
        return f"enrichment/{item_id}/{name}" if fields.get("url") == entry.url else None

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
        try:
            data = _sniff_bytes(file_path)
        except OSError as e:
            # Seeding's one file read: an unreadable media file (permissions,
            # a vanished LFS object) is named on the report, never fatal.
            detail = " ".join(str(e).split())
            self.notes.append(
                f"media file {repo_path} could not be read — {detail} — skipped this run"
            )
            return
        fmt = sniff_format(data, name=repo_path.rsplit("/", 1)[-1])
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
        # The ONE via-routed dispatch: the media stage gives blocked downloads
        # normal retry rules, and via is the only mark they carry.
        media_job = entry.via == "media"
        transcribe_job = not media_job and _is_transcribe_job(entry)
        if transcribe_job and not self._transcribe_slot():
            return False  # stays waiting untouched; the deferral is noted once
        try:
            # The try spans ALL per-unit processing:
            # fetch/transcribe/download, output write, media stage, children
            # admission. An engine bug anywhere in it ledgers `error` — the
            # outcome line supersedes any partial one — never aborts the run.
            if media_job:
                self._download_media(entry)
            elif transcribe_job:
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
        # A unit corrected to a transcribable kind (web → podcast) lands its
        # own output HERE, never through _apply_done — the pre-correction
        # kind's file leaves on the same rule, or the item carries two views
        # of one unit forever.
        _drop_superseded_outputs(self.ctx.instance, entry, path)
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
        # Both kinds park with content already in hand (a description, show
        # notes) and both compose the transcript onto what that park wrote,
        # so both acquisitions read the unit's own output file.
        name = f"{entry.kind.value}-{entry.hash[:6]}.md"
        enrichment = self.ctx.instance.enrichment_dir / entry.item / name
        match entry.kind:
            case Kind.YOUTUBE:
                return acquire_youtube_audio(
                    entry, enrichment, audio_dir, self.ctx.download_audio
                )
            case Kind.PODCAST:
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
                # escalating manual at 5. `needs` keeps the retry routed
                # back through the transcribe drain, never the driver.
                self._apply_blocked(
                    entry,
                    f"audio acquisition failed: {failure.reason}",
                    needs=Need.TRANSCRIBE,
                )
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
            _drop_superseded_outputs(self.ctx.instance, entry, path)
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

        The previous kind's output stays on disk until the corrected unit
        lands one of its own (:func:`_drop_superseded_outputs`). Unlinking
        it here would strip an item of the enrichment it already had the
        moment a corrected fetch parked — status back to raw, digest
        orphaned, nothing left to re-derive from.
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

    def _apply_blocked(
        self, entry: LedgerEntry, reason: str | None, *, needs: Need | None = None
    ) -> None:
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
        self.record_outcome(
            entry, status=Status.BLOCKED, needs=needs, attempts=attempts, reason=reason
        )

    # -- outputs ---------------------------------------------------------

    def _write_output(self, entry: LedgerEntry, result: Result, *, count: bool = True) -> str:
        """Write ``<kind>-<hash6>.md`` deterministically; byte-compare reruns.

        The ``fetched:`` stamp is masked out of the comparison — it changes
        every run by definition, and an unchanged rerun must not rewrite the
        file (nor report the item as changed). ``count=False`` writes
        without registering an item outcome (a waiting park's partial
        content is not cognitive work yet).

        Atomic, like every other state write: an interrupted run must not
        leave a half-file behind. A truncated enrichment file is the worst
        of them to lose — its frontmatter carries the thread-completeness
        markers and the re-fetch pointer, and lint's marker scan is the
        only thing that ever opens one.
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
        atomic.write_text(out, content)
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
        return sum(1 for path in item_dir.iterdir() if _is_media_file(path))

    # -- media stage ------------------------------------------------

    def _media_stage(self, parent: LedgerEntry, urls: list[str]) -> None:
        for url in urls:
            refusal = _unfetchable_media(url)
            if refusal is not None:
                self._park_unfetchable_media(parent, url, refusal)
                continue
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
            # the next run's redrain path picks up. The download itself goes
            # through _process, so a failure of any class is charged to the
            # media unit rather than to the page that pointed at it.
            self.record(child)
            self._process(self.entries[unit_hash])

    def _park_unfetchable_media(self, parent: LedgerEntry, url: str, why: str) -> None:
        """Park a media URL nothing could fetch — as its own unit, manual.

        The bad-seed pattern, one layer in: garbage never enters the queue,
        and the refusal is charged to the media unit, never to the page
        whose markup merely named it. Keyed on the raw URL like every other
        media line, so ``enrich mark`` can heal it.
        """
        if not url or "\n" in url or "\r" in url:
            # A multi-line value cannot be a ledger identity at all (the
            # schema is single-line) — the report is the only place it fits.
            self.notes.append(
                f"item {parent.item}: a media URL found on {parent.url} spans "
                "multiple lines and was dropped — it cannot become a work unit"
            )
            return
        unit_hash = work_hash(url)
        if unit_hash in self.entries:
            return
        self.record(
            LedgerEntry(
                hash=unit_hash,
                url=url,
                item=parent.item,
                kind=parent.kind,
                status=Status.MANUAL,
                engine="seed",  # stamped in record
                date=datetime.date.min,
                via="media",
                parent=parent.hash,
                depth=(parent.depth or 0) + 1,
                reason=f"unfetchable media URL — {why}",
            ),
            count=True,
        )

    def _download_media(self, entry: LedgerEntry) -> None:
        # Cap and oversize skips below land in the report's unit counts —
        # deliberately: they are unit outcomes, unlike the re-entry cap
        # fires, which never surface.
        slot = self._media_slot(entry)
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
            self._media_failure(entry, failure.status, failure.reason)
            return
        if not response.ok:
            failure = classify_http(response.status)
            self._media_failure(entry, failure.status, failure.reason)
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

    def _media_failure(self, entry: LedgerEntry, status: Status, reason: str) -> None:
        match status:
            case Status.BLOCKED:
                # The entry IS the prior line — a birth line carries no
                # attempts, a redrained one carries what it has earned.
                self._apply_blocked(entry, reason)
            case Status.MANUAL | Status.DEAD:
                self.record_outcome(entry, status=status, reason=reason)
            case _:
                # The classifier returns blocked/manual/dead only — anything
                # else is an engine bug, not a quiet default.
                raise RuntimeError(f"classifier returned unexpected status {status!r}")

    def _media_slot(self, entry: LedgerEntry) -> int | None:
        """This unit's media index — stable across runs — or None at the cap.

        The index is the unit's position among the item's media units in
        ledger order, NOT the next free index on disk: a crash between the
        file write and the outcome line leaves an orphaned file, and a
        next-free scan would then write a second copy beside it. Reruns
        overwrite, never duplicate.

        The index NAMES the file; it never decides the cap. The cap counts
        media-family files that exist — 4 per item, shared with extraction
        assets, whichever route wrote them — so a unit's parked, dead or
        skipped siblings spend nothing, and an index past the cap is
        ordinary (a thread pooling six photos whose first two 404 still
        lands four files, at ``media-2`` through ``media-5``). Counting
        positions instead recorded a terminal "media cap reached" on units
        no file had displaced, losing them permanently under a false
        reason. A slot already on disk is this unit's own file, overwritten
        in place, and likewise spends nothing new.
        """
        slot = self._media_position(entry)
        item_dir = self.ctx.instance.enrichment_dir / entry.item
        if any(_is_media_file(path) for path in item_dir.glob(f"media-{slot}.*")):
            return slot
        if self._media_file_count(entry.item) >= MEDIA_MAX_FILES:
            return None
        return slot

    def _media_position(self, entry: LedgerEntry) -> int:
        """How many of the item's media units were ledgered before this one.

        Counted regardless of status: the position is an identity, so a
        parked or skipped sibling still holds its place — a position that
        moved as statuses changed would rename files under the item.
        """
        seen = 0
        for unit_hash, other in self.entries.items():
            if unit_hash == entry.hash:
                break
            if other.item == entry.item and other.via == "media":
                seen += 1
        return seen

    # -- children -----------------------------------------------

    def _admit_children(self, parent: LedgerEntry, children: list[Child]) -> None:
        for child in children:
            canonical, unit_hash = self.identify(child.url)
            if unit_hash in self.entries:
                continue  # dedupe by hash — a URL under two items enriches under the first
            depth = (parent.depth or 0) + 1
            cap = self._cap_fired(parent.item, depth)
            detection = None if cap else detect(child.url, self.ctx.drivers, sniff=self.sniff)
            entry = LedgerEntry(
                hash=unit_hash,
                url=canonical,
                item=parent.item,
                kind=detection.kind if detection else detect_kind(child.url, self.ctx.drivers),
                format=detection.format if detection else None,
                status=Status.SKIPPED if cap else Status.QUEUED,
                cap=cap,
                engine="seed",
                date=datetime.date.min,
                via=child.via,
                parent=parent.hash,
                depth=depth,
                reason=None if cap is None else f"{CAP_BOUNDS[cap]} reached",
            )
            self.record(entry)
            if cap is None:
                self.queue.append(unit_hash)

    def _cap_fired(self, item_id: str, depth: int) -> Cap | None:
        """The re-entry cap check: recorded in the ledger, never user-surfaced."""
        if depth > MAX_DEPTH:
            return Cap.DEPTH
        if self.fetched_count(item_id) >= MAX_URLS_PER_ITEM:
            return Cap.URL
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

    def record(self, entry: LedgerEntry, *, count: bool = False) -> LedgerEntry:
        stamped = ledger.stamp(
            entry,
            today=self.ctx.today,
            now=self.ctx.now,
            engine_version=self.ctx.engine_version,
        )
        ledger.append(self.ctx.instance.ledger_path, stamped)
        self.entries[stamped.hash] = stamped
        self.written[stamped.hash] = stamped
        if count:
            self.counts[stamped.status] = self.counts.get(stamped.status, 0) + 1
            self.touched.add(stamped.item)
        if count and stamped.status in _PARKED:
            row: dict[str, object] = {
                "item": stamped.item,
                "url": stamped.url,
                "status": stamped.status.value,
                "reason": stamped.reason
                or stamped.error
                or (f"waiting on {stamped.needs}" if stamped.needs else "no reason recorded"),
            }
            if stamped.attempts is not None:
                # The report splits parked entries by who owns the next
                # action, and for a blocked one the answer is "the engine,
                # this many more times" — which only the cap makes readable.
                row["attempts"] = stamped.attempts
                row["attempt_cap"] = MAX_BLOCKED_ATTEMPTS
            self.parked.append(row)
        return stamped

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
        self.resolve_owners()  # this run's children exist now, and want owners
        for item_id, path in self.item_paths.items():
            detail = _refresh_item_frontmatter(
                self.ctx.instance, item_id, path, entries=self.entries, owners=self.owners
            )
            if detail is not None:
                self.notes.append(
                    f"item {item_id}: an enrichment file cannot be listed in "
                    f"frontmatter ({detail}) — rename it, then any verb refreshes "
                    "the listing"
                )

    # -- report ----------------------------------------------------------

    def derive_no_source_items(self) -> None:
        """List fresh text/image-only items the unit-driven report cannot see.

        A capture with no URLs and no document media seeds nothing, so it
        never appears among the drained units — yet its description and
        digest are exactly the session's work. Derived on demand, never
        seeded (a fact that shows, not work to queue): a raw item with zero
        ledger units and no digest is cognitive work until digested.

        "Has no units" is the corpus's question too: an item sharing its
        one URL with another lost the seeding dedupe, so the only line for
        its work names the other item — and calling it source-less says
        the opposite of what its frontmatter says.
        """
        sourced = {item_id for items in self.owners.values() for item_id in items}
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
        incomplete = self._incomplete_items()
        if incomplete:
            payload["incomplete"] = incomplete
        cognitive = self._cognitive_jobs()
        if cognitive:
            payload["cognitive"] = cognitive
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload

    def _incomplete_items(self) -> list[dict[str, object]]:
        """Why each touched item is still raw — the shape of what is missing.

        An item is one unit of knowledge, and none of it advances to digest
        or wiki until every part has landed. The report states that shape
        per item so a session never has to infer completeness by reading a
        list of units.

        Cap-fire markers are not units: a harvest that overran the URL cap
        recorded refused work, which no user surface reports (§12). Counted
        as landed they inflate both halves of the shape — "15 of 16 units
        landed" for an item that admitted twelve.

        Which units are the item's is the corpus's answer, not the stored
        string's, so the shape here and the ``raw`` its frontmatter derives
        are read off one map.
        """
        rows: list[dict[str, object]] = []
        for item_id in sorted(self.touched):
            units = [
                entry
                for entry in self.entries.values()
                if item_id in self.owners.get(entry.hash, ()) and not _is_cap_refusal(entry)
            ]
            outstanding = [entry for entry in units if entry.status in _OUTSTANDING]
            if not outstanding:
                continue
            groups: dict[tuple[str, str | None], int] = {}
            for entry in outstanding:
                key = (entry.status.value, entry.needs.value if entry.needs else None)
                groups[key] = groups.get(key, 0) + 1
            rows.append(
                {
                    "item": item_id,
                    "landed": len(units) - len(outstanding),
                    "total": len(units),
                    "outstanding": [
                        {"status": status, "count": count}
                        if needs is None
                        else {"status": status, "needs": needs, "count": count}
                        for (status, needs), count in groups.items()
                    ],
                }
            )
        return rows

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


def _drop_superseded_outputs(instance: Instance, entry: LedgerEntry, path: str) -> None:
    """Drop the unit's earlier-kind output — once ``path`` replaces it.

    A redetection relabels a unit, so its output file is renamed by kind.
    The stale file leaves the disk here, on the success that replaces it,
    and never earlier: a corrected fetch that parks must leave the item
    exactly as enriched as it found it. The ledger's audit trail keeps the
    history either way. Every route to a unit's own output comes through
    here — the drain's fetch write, the transcribe drain's landing, and a
    hand-written file closed by ``mark``.

    Candidate names are the closed ``<kind>-<hash6>.md`` set, and each
    candidate must PROVE it belongs to this unit by the URL it records:
    ``hash6`` is six hex digits, so two units under one item share one often
    enough that a real pair was found by hand, and matching on the name
    alone unlinked the neighbour's enrichment while its ledger line still
    read ``done``. Nothing is dropped for a replacement that is not on disk
    — a mistyped ``mark --path`` must not cost the item its enrichment.
    """
    if not (instance.root / path).is_file():
        return
    item_dir = instance.enrichment_dir / entry.item
    current = Path(path).name
    for kind in Kind:
        stale = item_dir / f"{kind.value}-{entry.hash[:6]}.md"
        if stale.name == current or not stale.is_file():
            continue
        fields, _body = read_enrichment(stale)
        if fields.get("url") == entry.url:
            stale.unlink()


def _refresh_item_frontmatter(
    instance: Instance,
    item_id: str,
    path: Path | None = None,
    *,
    entries: dict[str, LedgerEntry] | None = None,
    owners: Mapping[str, tuple[str, ...]] | None = None,
) -> str | None:
    """Derive one item's ``status``/``enrichment:``; write only on change.

    The listing is the enrichment directory's markdown files; the status is
    the ledger's answer to "has the whole item landed" — ``enriched`` only
    when no unit the item owns is still outstanding, ``raw`` while one is.
    Callers holding the ledger pass ``entries``, and the ownership map with
    it where they have one (it costs a corpus read); the rest read both
    here.

    Silent when the corpus file is gone or unreadable: a heal may outlive
    its item (excluded after capture), an unreadable item is seeding's to
    name — and the ledger write this follows must stand either way. A
    refresh the derived listing itself refuses — an on-disk enrichment
    filename the corpus schema cannot hold (a hand-written name with a
    comma, bracket, or edge whitespace) — comes back as the failure detail
    for callers with a report to surface; quiet callers (the mark and pass
    verbs) ignore it, and no caller ever dies on it.
    """
    if path is None:
        path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    try:
        item = corpus.read_item(path)
    except (OSError, UnicodeDecodeError, corpus.CorpusSchemaError):
        # UnicodeDecodeError is a ValueError, not an OSError: without it a
        # single non-UTF-8 item made mark and pass exit 1 AFTER their write
        # had landed, and the retry duplicated the record.
        return None
    files = sorted(p.name for p in (instance.enrichment_dir / item_id).glob("*.md"))
    units = _item_units(instance, item_id, entries, owners)
    try:
        updated = dataclasses.replace(
            item, status=_derived_status(item, files, units), enrichment=files
        )
        if updated != item:
            corpus.write_item(path, updated)
    except (OSError, corpus.CorpusSchemaError) as e:
        return str(e)
    return None


def _unit_owners(
    instance: Instance,
    entries: Mapping[str, LedgerEntry],
    drivers: Sequence[SourceDriver] = DRIVERS,
) -> dict[str, tuple[str, ...]]:
    """Which live corpus items each work unit belongs to.

    A ledger line's ``item`` is the attribution as of the day it was
    written, and migration 1 carries a stated item verbatim forever — so a
    unit of an item RENAMED since then names an id no corpus file answers
    to, and reading ownership off that string hands the work to a dead id.
    The corpus is the source of truth, so the corpus is asked first
    (:func:`corpus_claims`), exactly as ``exclude``, ``lint`` and migration
    2 already ask it.

    Three answers, in the order migration 2 established:

    1. Every live item that lists the URL — plus the entry's own item where
       its corpus file exists, which answers first for a unit an owner has
       since removed from the frontmatter it was seeded from.
    2. Failing that, the parent's owners: a harvest-promoted child, a media
       download and an extracted asset are never listed in any frontmatter,
       so the only corpus answer they have is the one their parent gets.
    3. Failing that, the stored string as written. A unit no live item
       claims belongs to whoever the line says — which is lint's ghost-item
       finding to report, not this map's to silently reassign.

    Args:
        instance: The instance.
        entries: The ledger, hash -> latest entry.
        drivers: The driver registry that owns canonicalization.

    Returns:
        Work hash -> the owning item ids, in id order. Every hash in
        ``entries`` has an answer; none is ever empty.
    """
    claims = corpus_claims(instance.root, drivers)
    live = {path.stem for path in instance.corpus_dir.glob("*/*.md")}
    owners: dict[str, tuple[str, ...]] = {}

    def resolve(unit_hash: str, seen: frozenset[str]) -> tuple[str, ...]:
        cached = owners.get(unit_hash)
        if cached is not None:
            return cached
        entry = entries.get(unit_hash)
        if entry is None:
            return claims.get(unit_hash, ())
        claimants = set(claims.get(unit_hash, ()))
        if entry.item in live:
            claimants.add(entry.item)
        if claimants:
            resolved = tuple(sorted(claimants))
        elif entry.parent is not None and entry.parent not in seen:
            # Cycle-guarded: a hand-edited `parent` pointing back into its
            # own chain must cost a lookup, never the run.
            resolved = resolve(entry.parent, seen | {unit_hash}) or (entry.item,)
        else:
            resolved = (entry.item,)
        owners[unit_hash] = resolved
        return resolved

    for unit_hash in entries:
        resolve(unit_hash, frozenset())
    return owners


def _item_units(
    instance: Instance,
    item_id: str,
    entries: dict[str, LedgerEntry] | None,
    owners: Mapping[str, tuple[str, ...]] | None = None,
) -> list[LedgerEntry] | None:
    """The item's ledger units — from the caller's map, or read here.

    Ownership is the corpus's answer, not the line's stored string
    (:func:`_unit_owners`): a renamed item's units are its own, and a URL
    two items list is owed by both.

    None means the ledger could not be read at all: the quiet callers write
    first and refresh after, so a nonconforming line must leave the status
    alone rather than raise past a write that already landed.
    """
    if entries is None:
        entries = _ledger_or_none(instance)
        if entries is None:
            return None
    if owners is None:
        owners = _unit_owners(instance, entries)
    return [entry for entry in entries.values() if item_id in owners.get(entry.hash, ())]


def _ledger_or_none(instance: Instance) -> dict[str, LedgerEntry] | None:
    """The whole ledger, or None when it cannot be read at all."""
    try:
        return ledger.load(instance.ledger_path)
    except (OSError, UnicodeDecodeError, ledger.LedgerSchemaError):
        return None


def _derived_status(
    item: corpus.CorpusItem, files: list[str], units: list[LedgerEntry] | None
) -> str:
    """``enriched`` iff the item holds enrichment and owes no further work.

    An item is one unit of knowledge — the post, the thread parents above
    it, the links harvest promoted, the media, the video awaiting its
    transcript — so one outstanding unit keeps the whole item ``raw`` and
    out of digest/wiki. A dead link or a deliberately skipped unit owes
    nothing and holds nothing hostage.
    """
    if not files:
        return "raw"
    if units is None:
        return item.status
    return "raw" if any(unit.status in _OUTSTANDING for unit in units) else "enriched"


# ---------------------------------------------------------------------------
# Verbs (called by the CLI; zero business logic lives there).
# ---------------------------------------------------------------------------


def _finish(drain: _Drain) -> str:
    """File this run's errors upstream, then render the enrich-report.

    The filer runs after the drain — it fires only on ``status: error``
    outcomes already ledgered — and its notes/failures land on the same
    report, so the human always knows what was reported.

    The run's own writes are read back first: the report states outcomes on
    the strength of having written them, so a write the ledger did not
    resolve to is named here rather than reported as work that landed
    (:func:`_writes_that_did_not_land`).
    """
    ctx = drain.ctx
    inert = _writes_that_did_not_land(ctx.instance, drain.written)
    if inert:
        drain.notes.append(
            f"{len(inert)} of this run's ledger writes did not take effect — the ledger "
            "still resolves those units to an earlier line. Lines are ordered by this "
            "machine's wall clock, so a clock set BACKWARDS writes work into the past, "
            "where it loses and `compact` deletes it: check the clock, then re-run. The "
            f"units are {', '.join(sorted(inert))}."
        )
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


def _is_cap_refusal(entry: LedgerEntry) -> bool:
    """True for a cap-fire marker line — refused work, not an admitted unit."""
    return entry.cap is not None


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
            # One URL enriches under one item — but refusing the BATCH would
            # abort the URLs already ledgered ahead of this one and swallow
            # the report with them. The refusal is reported; the rest fetch.
            drain.notes.append(
                f"{canonical} already enriches under item {existing.item} — one URL "
                f"enriches under one item; not fetched into {item_id}"
            )
            return None
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
        bound = CAP_BOUNDS[Cap.URL_REQUESTED]
        reason = (
            f"{bound} exceeded by --force"
            if force
            else f"{bound} reached — rerun with --force to exceed"
        )
        repeat = existing is not None and _is_cap_refusal(existing) and existing.reason == reason
        if not repeat:
            # A refusal that says byte-for-byte what the last one said adds
            # nothing to the audit trail.
            drain.record(
                LedgerEntry(
                    hash=unit_hash,
                    url=canonical,
                    item=item_id,
                    kind=detect_kind(url, drain.ctx.drivers),
                    status=Status.SKIPPED,
                    # The owner asked for this URL: the bound is the same
                    # one harvest hits, the reading is not.
                    cap=Cap.URL_REQUESTED,
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
            # An owner-requested refusal IS surfaced: the owner asked, and a
            # bare "skipped 1" leaves the --force route unreachable.
            drain.notes.append(f"not fetched — {canonical}: {reason}")
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
        return _item_status(ctx, entries, item_id)
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


def _item_status(ctx: RunContext, entries: dict[str, LedgerEntry], item_id: str) -> str:
    """One item's units — the ones the corpus says are its.

    Asking the stored string told a renamed item it had "no ledger work
    units: a no-source capture, or `enrich run` has not seeded it yet",
    naming two causes that were not the cause while its work sat in the
    ledger under a dead id.
    """
    owners = _unit_owners(ctx.instance, entries, ctx.drivers)
    units: list[dict[str, object]] = []
    for entry in entries.values():
        if item_id not in owners.get(entry.hash, ()):
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

    Staleness is read from committed dates, never file mtimes: git stamps
    every file at checkout, so on a second machine the whole tree carries
    one mtime and staleness would be undetectable. The two dates are the
    item's newest ``done`` ledger line and its digest pass record — both
    travel in git, and day granularity is the intent (enriching and
    digesting in one session is not stale).

    An item still owing a unit is never listed, however long it has owed
    it. Such an item derives ``raw``, and a raw item is one the ingest
    procedure forbids digesting — so listing it would report the same
    permanently-parked item as work to do on every run forever. A
    ``manual`` unit awaiting judgment, an ``error`` unit on an unchanged
    engine, a ``blocked`` unit past its attempts and a ``waiting`` unit
    with no provider all sit there indefinitely by design.

    Args:
        instance: The instance.

    Returns:
        Item ids that owe no further work and whose enrichment landed
        after their digest pass (or that have enrichment and no digest at
        all).
    """
    orphans = []
    if not instance.enrichment_dir.is_dir():
        return orphans
    entries = _ledger_or_none(instance)
    owners = _unit_owners(instance, entries) if entries is not None else {}
    owing = _items_owing_work(entries, owners)
    enriched_on = _last_enriched(entries, owners)
    digested_on = _last_digested(instance)
    for item_dir in sorted(instance.enrichment_dir.iterdir()):
        if not item_dir.is_dir():
            continue
        if not any(item_dir.glob("*.md")):
            continue
        if item_dir.name in owing:
            continue
        if not (instance.digests_dir / f"{item_dir.name}.md").exists():
            orphans.append(item_dir.name)
            continue
        landed = enriched_on.get(item_dir.name)
        digested = digested_on.get(item_dir.name)
        # No dates on either side is no claim: enrichment written outside
        # the ledger (a media description) and digests older than the pass
        # record are facts about history, not staleness.
        if landed is not None and digested is not None and digested < landed:
            orphans.append(item_dir.name)
    return orphans


def _items_owing_work(
    entries: dict[str, LedgerEntry] | None, owners: Mapping[str, tuple[str, ...]]
) -> set[str]:
    """The items a unit is still outstanding on — the ones deriving ``raw``.

    Read off the same ownership map the frontmatter's ``raw`` is derived
    from (:func:`_unit_owners`): an item held out of digest by an
    outstanding unit and an item listed as owing one have to be the same
    item, or the backstop names work the ingest procedure forbids doing.

    An unreadable ledger claims nothing: no item is held back, exactly as
    no item is called stale.
    """
    if entries is None:
        return set()
    return {
        item_id
        for entry in entries.values()
        if entry.status in _OUTSTANDING
        for item_id in owners.get(entry.hash, (entry.item,))
    }


def _last_enriched(
    entries: dict[str, LedgerEntry] | None, owners: Mapping[str, tuple[str, ...]]
) -> dict[str, datetime.date]:
    """Each item's newest ``done`` ledger date.

    A nonconforming ledger (``entries`` None) is lint's own loud finding;
    the staleness backstop simply has nothing to compare and says nothing.
    """
    if entries is None:
        return {}
    newest: dict[str, datetime.date] = {}
    for entry in entries.values():
        if entry.status is not Status.DONE:
            continue
        for item_id in owners.get(entry.hash, (entry.item,)):
            newest[item_id] = max(newest.get(item_id, entry.date), entry.date)
    return newest


def _last_digested(instance: Instance) -> dict[str, datetime.date]:
    """Each item's newest recorded digest pass date."""
    path = instance.passes_path
    if not path.exists():
        return {}
    newest: dict[str, datetime.date] = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # lint parses this file loudly; the backstop skips the line
        if not isinstance(record, dict) or record.get("stage") != "digest":
            continue
        item, raw_date = record.get("item"), record.get("date")
        if not isinstance(item, str) or not isinstance(raw_date, str):
            continue
        try:
            date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            continue
        newest[item] = max(newest.get(item, date), date)
    return newest


def _writes_that_did_not_land(
    instance: Instance, written: Mapping[str, LedgerEntry]
) -> dict[str, LedgerEntry | None]:
    """The appended lines the ledger does not resolve to, and what it resolves to instead.

    Appending is not landing. ``at`` decides which line of a hash is live
    and it comes from the writing machine's wall clock: a clock set
    BACKWARDS — a dead RTC, a container with no NTP, a restored snapshot —
    stamps the write in the past, where it loses to the very line it was
    meant to supersede, and ``compact`` then deletes it as superseded with
    no audit trail. The ledger's cost model for a wrong clock is one line's
    ordering, never the hash, and it holds only while a verb that reports
    an outcome reads its own write back rather than trusting the append.

    Args:
        instance: The instance whose ledger is re-read.
        written: The lines just appended, keyed by hash.

    Returns:
        One entry per write that is not live, mapped to the line that is —
        ``None`` where the hash carries no line at all. Empty is the
        healthy case.
    """
    if not written:
        return {}
    live = ledger.load(instance.ledger_path)
    return {
        unit_hash: live.get(unit_hash)
        for unit_hash, entry in written.items()
        if live.get(unit_hash) != entry
    }


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

    A unit is found by its canonical identity, or — failing that — by the
    exact key it was stored under: bad seeds and every ``via: media`` line
    are keyed on the URL verbatim (canonicalization failed, or never ran),
    so a canonical-only lookup could never reach them, and the contract
    forbids healing them by hand.

    Args:
        ctx: The run context.
        url: The unit's URL — canonicalized first, then matched verbatim.
        status: The corrected status.
        reason: The stated reason (required for manual/skipped).
        path: Output path, for done heals that wrote a file.
        needs: The needed capability, for waiting/blocked heals (defaults
            to the prior entry's).

    The heal is read back before it is reported: a line is only a
    correction once the ledger resolves its hash to it, and on a machine
    whose clock runs behind it does not (:func:`_writes_that_did_not_land`).

    Returns:
        A one-line confirmation.

    Raises:
        ValueError: No ledger entry exists for the URL, ``error`` was
            requested (errors are engine outcomes, not heals), the
            status/field combination violates the ledger schema, or the
            written line did not become the unit's live line.
    """
    if status is Status.ERROR:
        raise ValueError("mark cannot write 'error' — errors are engine outcomes, not heals")
    if reason is not None:
        # Session-typed reasons arrive with tabs and newlines; the ledger
        # schema is single-line, so the verb normalizes exactly as the
        # scrubber does for engine-produced reasons.
        reason = " ".join(reason.split()) or None
    drain = _Drain(ctx=ctx)
    try:
        canonical, _ = drain.identify(url)
    except ValueError:
        # The URL a bad seed was parked for cannot be canonicalized — that
        # is why it parked. The verbatim key is the only one it ever had.
        canonical = url
    prior = drain.entries.get(work_hash(canonical))
    if prior is None:
        prior = drain.entries.get(work_hash(url))
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
        # A stray --needs on a status that cannot carry it passes through
        # and fails schema validation loudly rather than being silently
        # dropped.
        needs=(needs or prior.needs) if status in (Status.WAITING, Status.BLOCKED) else needs,
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
    stamped = drain.record(healed)
    # Before anything acts on the heal: an append that lost its hash is a
    # correction that did not happen, and dropping the superseded outputs
    # or refreshing the item's frontmatter on the strength of it would
    # write the heal's view of the world over state the ledger disagrees
    # with. The line stays in the file as the audit trail of the attempt.
    live = _writes_that_did_not_land(ctx.instance, {stamped.hash: stamped})
    if stamped.hash in live:
        resolves = live[stamped.hash]
        instead = "no line at all" if resolves is None else f"{resolves.status.value}"
        raise ValueError(
            f"the {status.value} line for {prior.url} ({prior.hash}) was appended but did "
            f"not take effect — the ledger still resolves that unit to {instead}. Ledger "
            "lines are ordered by the writing machine's wall clock, so a clock set BACKWARDS "
            "stamps the correction in the past, where it loses to the line it was meant to "
            "supersede and `compact` deletes it. Check this machine's clock, then mark again."
        )
    if status is Status.DONE and effective_path is not None:
        # A hand-written enrichment closed by mark is one of the unit's own
        # outputs, so it supersedes an earlier kind's exactly as a drained
        # one does. This is the prescribed route out of a redetection whose
        # corrected fetch parks (a scanned PDF, no extractor); without the
        # drop the item keeps two files for one unit and serves the stale
        # pre-correction view to the digest and query layers forever.
        _drop_superseded_outputs(ctx.instance, healed, effective_path)
    # The heal's own line included: the drain's map is the ledger as of this
    # write, so a heal that completes an item flips its status in the same call.
    # Every item the corpus says owns the unit is refreshed, not the one the
    # line names: a shared URL completes two items at once, and a renamed
    # item's line names an id with no file to refresh at all.
    drain.resolve_owners()
    for item_id in drain.owners.get(prior.hash, (prior.item,)):
        _refresh_item_frontmatter(ctx.instance, item_id, entries=drain.entries, owners=drain.owners)
    return f"marked {prior.url} ({prior.hash}) {status.value}"


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
# the intended string: boolean/null words, int/float lookalikes in every
# 1.1 spelling (sign, underscores, hex/octal/binary, exponent, .inf/.nan),
# and date/timestamp lookalikes. Leading `-`/`?`/`,` are indicator
# characters that make the line invalid or restructure it. All are
# emitted JSON-quoted.
_YAML_KEYWORDS = frozenset({"true", "false", "yes", "no", "on", "off", "null", "~"})
_YAML_NUMBER_RE = re.compile(
    r"[-+]?(?:\.inf|\.nan|0x[0-9a-f_]+|0b[01_]+|0o?[0-7_]+"
    r"|[0-9][0-9_]*(?:\.[0-9_]*)?(?:e[-+]?[0-9]+)?"
    r"|\.[0-9][0-9_]*(?:e[-+]?[0-9]+)?)",
    re.IGNORECASE,
)
# The 1.1 timestamp resolver's shape, mirrored: a bare YYYY-MM-DD retypes
# to a date and a full timestamp to a datetime — and a shape-matching but
# calendar-invalid value (2026-08-99) makes those readers RAISE, so the
# match is on shape alone, never calendar validity.
_YAML_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}"
    r"(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?"
    r"(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?"
)


def _render_enrichment(
    url: str, fetched: datetime.date, meta: dict[str, str | int | None], body: str
) -> str:
    lines = ["---", f"url: {_yaml_value(url)}", f"fetched: {fetched.isoformat()}"]
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
        or "\t" in value  # a tab in a plain scalar invalidates the whole block
        or value[:1] in ("-", "?", ",")
        or any(ch in value for ch in _YAML_UNSAFE)
        or value.lower() in _YAML_KEYWORDS
        or _YAML_NUMBER_RE.fullmatch(value) is not None
        or _YAML_TIMESTAMP_RE.fullmatch(value) is not None
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
