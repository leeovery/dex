"""The orchestrator: seed, drain, write, re-enter — and report verbatim.

The ledger is the work queue. A run seeds queued entries for new corpus
URLs, drains everything :func:`is_drainable`, hands units to drivers, writes
deterministically named outputs (reruns overwrite, never duplicate),
downloads media, and renders its report through the ``enrich-report``
surface. Everything enters the queue through one door
(:meth:`_Drain.admit`) — a capture at depth 0, or a promotion the session
named with ``enrich fetch``, bounded there by the re-entry caps.

TWO ``except Exception`` exist in the whole pipeline, and both are named
where they sit: the per-unit loop here, and the canonicalization seam in
``detect``, which types whatever a driver's ``canonical`` raises as the one
refusal every caller already handles. Every ledger write goes through
``ledger.stamp`` with the injected clocks and engine version.
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
from dex_engine.drivers.fetch import FetchFailure, fetch_classified
from dex_engine.drivers.transport import Transport, urllib_transport
from dex_engine.drivers.ytdlp import DownloadAudio, yt_dlp_audio
from dex_engine.render import surfaces

from . import issues, ledger
from .classify import (
    Classification,
    ProviderInputError,
    ProviderUnavailableError,
    classify_connection,
    scrub,
)
from .detect import Sniff, canonical_url, detect, detect_kind, sniff_format
from .enrichment import (
    instagram_body,
    mask_fetched,
    podcast_body,
    read_enrichment,
    render_enrichment,
    youtube_body,
)
from .ownership import unit_owners
from .registry import default_drivers, driver_for
from .transcribe import (
    TRANSCRIBE_RUN_CAP,
    Acquired,
    acquire_instagram_audio,
    acquire_podcast_audio,
    acquire_youtube_audio,
)
from .types import (
    Availability,
    Cap,
    Config,
    Content,
    Format,
    Instance,
    Job,
    Kind,
    LedgerEntry,
    MediaFetch,
    Missing,
    Need,
    NeedsCapability,
    Outcome,
    Redetected,
    Refused,
    SourceDriver,
    Status,
    Transcriber,
    Unusable,
    WorkUnit,
    version_newer,
)
from .urls import ext_of, resolve_repo_path, work_hash

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
    "digested_items",
    "fetch_urls",
    "head_sniffer",
    "is_drainable",
    "items_owing_work",
    "mark",
    "never_harvested",
    "no_providers",
    "record_pass",
    "run",
    "run_transcribe",
    "status_report",
]

# Re-entry caps: mechanical backstops, not targets. Fires are recorded in
# the ledger — a skipped entry naming the bound in `cap` — answered on the
# run report to the session that asked, and read as a tuning signal by the
# health check unless `--force` waived them.
MAX_DEPTH = 4
MAX_URLS_PER_ITEM = 12

# What each bound is called wherever a fire is counted or read. Every
# reader takes its wording from here, so one bound can never read as two.
CAP_BOUNDS: dict[Cap, str] = {
    Cap.DEPTH: f"depth cap ({MAX_DEPTH})",
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


def _provider_input_reason(entry: LedgerEntry, error: ProviderInputError) -> str:
    """The manual-park reason for input a provider could not use.

    The provider's own words lead, always. An instagram unit then carries
    the disposition it usually needs: silent and music-only reels are a
    large share of what Instagram holds, so "heard no speech" is a standing
    outcome there rather than a fault to chase — and the caption the park
    already stored is a record of the post on its own.
    """
    reason = scrub(str(error))
    if entry.kind is Kind.INSTAGRAM:
        reason += (
            " — the caption is already stored; mark done to keep it as the record, "
            "or rescue by hand"
        )
    return reason


def _spends_url_budget(entry: LedgerEntry) -> bool:
    """Whether this line spends its item's 12-URL budget.

    Fetched pages only: media downloads and extraction-asset byte-writes
    (the ``job`` field) are not pages, and a skipped line — the cap's own
    markers included — holds no unit.
    """
    return entry.job is None and entry.status is not Status.SKIPPED


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


@dataclass(frozen=True, slots=True, kw_only=True)
class _CapRefusal:
    """A re-entry bound turning a unit away: which bound, and what it says.

    It carries the re-entry it refused (``parent``, ``depth``) because only
    a re-entry can have one — the caps do not read a capture at all —
    and because the marker line records exactly that provenance.

    ``waived`` is ``enrich fetch --force`` on a bound that HAS an override:
    the fire is recorded either way and the unit still enters, so the
    refusal survives as the audit trail behind the line that supersedes it.
    """

    cap: Cap
    reason: str
    waived: bool
    parent: str
    depth: int


@dataclass(frozen=True, slots=True, kw_only=True)
class _Admission:
    """What admitting one URL settled — its identity, and what became of it.

    ``hash``/``url`` are the unit's canonical identity either way, because
    both refusals answer about a named unit. Exactly one of the rest holds:
    ``entry`` is the queued line when the URL was admitted, ``held`` is the
    unit the ledger already had under that hash, and ``cap``/``reason`` name
    the bound that turned it away.
    """

    hash: str
    url: str
    entry: LedgerEntry | None = None
    held: LedgerEntry | None = None
    cap: Cap | None = None
    reason: str | None = None


@dataclass(slots=True)
class _ItemOutcome:
    """What a run did for one item — feeds the report's cognitive work list."""

    new: int = 0
    changed: int = 0
    media: int = 0
    # Units re-fetched to exactly what was already stored. Deliberately NOT
    # part of reason(): an unchanged re-fetch is not new material and must
    # not join the writing-up queue. It is counted because the report has
    # to account for every unit it says it processed — see `unchanged` in
    # the enrich-report payload.
    unchanged: int = 0

    def reason(self) -> str:
        """What landed, named — "3 new" never said new *what*."""
        parts = [
            f"{self.new} new enrichment {'file' if self.new == 1 else 'files'}" if self.new else "",
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
    # Unit hashes whose URL some live item's frontmatter shares as http.
    # Canonicalization forces https (identity and hashing are unchanged),
    # so this records that the owner actually shared the source over http
    # — one of the TLS-failure fallback's two licenses, covering captured
    # lines however old. The other is the entry's own typed `http_shared`
    # flag, stamped at the admission door: the only record a PROMOTED
    # http URL has (promotions never live in frontmatter), and the one
    # that keeps a redrain licensed.
    http_shared: set[str] = field(default_factory=set)
    # Which live corpus items own each unit — the corpus's answer, never the
    # ledger line's stored string. Rebuilt after the drain, when this run's
    # children exist to be attributed.
    owners: dict[str, tuple[str, ...]] = field(default_factory=dict)
    no_source_items: list[str] = field(default_factory=list)
    # Live items whose every ledger unit closed without content (dead or
    # skipped) and whose digest is not yet recorded — like the no-source
    # items, owed description + digest from the owner's note, and derived
    # on demand for the report.
    closed_unenriched_items: list[str] = field(default_factory=list)
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
    # Ledgered media units left resting because `media_fetch` is `none` —
    # noted once, like the transcription deferral.
    deferred_media: int = 0
    # Per-item fetched-page counts, maintained beside the entries so the
    # URL-cap check is not a full recount per admission (quadratic in the
    # item's ledger). Built lazily from the entries, updated in `record` —
    # the one door entries change through — and dropped whenever ownership
    # re-resolves, because the counts attribute exactly as `owner_of` does.
    _fetched_counts: dict[str, int] | None = None
    # One append handle for the run's many ledger writes (open-per-line
    # cost the drain per unit). Every line is flushed as written, so the
    # read-backs (mark's, the report's) and any concurrent reader see it
    # exactly as they did under open-per-line; the verbs close it when
    # they finish.
    _appender: ledger.Appender = field(init=False)

    def __post_init__(self) -> None:
        self.entries = ledger.load(self.ctx.instance.ledger_path)
        self.sniff = head_sniffer(self.ctx.transport)
        self._appender = ledger.Appender(self.ctx.instance.ledger_path)

    def close_ledger(self) -> None:
        """Release the run's append handle (every line is already flushed)."""
        self._appender.close()

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
                    admission = self.admit(item.id, url)
                except ValueError as e:
                    # One malformed capture URL parks THAT URL; it never
                    # aborts the run (frontmatter is immutable provenance —
                    # garbage in it is judgment work, hence manual).
                    self.park_bad_seed(item.id, url, e)
                    continue
                if url.startswith("http://"):
                    # The owner shared this source as http; canonicalization
                    # forces https anyway, so remember the fact here — it is
                    # what licenses the TLS-failure http fallback.
                    self.http_shared.add(admission.hash)
            for repo_path in item.media:
                self._seed_media_file(item.id, repo_path)
        self.resolve_owners()

    def resolve_owners(self) -> None:
        """Re-read which live corpus item owns each unit (one corpus pass)."""
        self.owners = _unit_owners(self.ctx.instance, self.entries, self.ctx.drivers)
        # The counts attribute by owner, so a new answer to "whose unit is
        # this" (a rename mid-run) invalidates them wholesale — they
        # rebuild from the entries on the next cap check.
        self._fetched_counts = None

    def owner_of(self, entry: LedgerEntry) -> str:
        """Which live item owns this unit's work — the corpus's answer.

        The single-name form of :func:`_unit_owners`, for the write path.
        A line's ``item`` is the attribution as of the day it was written,
        and migration 1 carries a stated item verbatim, so a unit of an
        item RENAMED since names an id no corpus file answers to — and the
        drain carries that string into the next line it records and into
        the ``enrichment/<item>/`` path it writes, filing live work under a
        dead id. Every reader resolves ownership off the corpus; so does
        every write, which is the same question asked once more at the
        moment the answer is committed.

        The stored string wins wherever it is still one of the owners: a
        unit two live items share must not migrate between them run to run.
        It is also the fallback where nothing live claims the work at all,
        exactly as the read side falls back — an unclaimed unit belongs to
        whoever the line says, which is lint's ghost-item finding to
        report, never this to silently reassign. So a stale ``item`` on an
        old line stays what it is, historical attribution, and no persisted
        line is ever rewritten to heal it.
        """
        owners = self.owners_of(entry)
        return entry.item if entry.item in owners else owners[0]

    def owners_of(self, entry: LedgerEntry) -> tuple[str, ...]:
        """Every item the corpus resolves this unit to — never empty.

        The multi-claimant face of :meth:`owner_of`, for membership
        questions: a URL two items list is owed by BOTH, so "is this item
        among the unit's claimants" must be asked of the whole tuple —
        compared against the single-name form it always answered for the
        first claimant alone, and the second claimant of every shared
        unit read as owning nothing.
        """
        return self.owners.get(entry.hash) or (entry.item,)

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

    def identify(self, url: str) -> tuple[str, str]:
        """Canonical form and work hash — delegation lives in detect."""
        canonical = canonical_url(url, self.ctx.drivers)
        return canonical, work_hash(canonical)

    # -- admission -------------------------------------------------------

    def admit(
        self,
        item_id: str,
        url: str,
        *,
        via: str | None = None,
        parent: LedgerEntry | None = None,
        force: bool = False,
    ) -> _Admission:
        """The one road into the queue — every unit this engine works enters here.

        Canonical form keys the ledger; a hash the ledger already holds as a
        unit is never re-admitted; provenance is assigned from the spawning
        entry, never by a caller; the re-entry caps bound anything spawned;
        and what survives is recorded ``queued``, which is what puts it in
        front of the drain — every admission happens before :meth:`drain`
        builds its queue, and the queue is built from the entries.

        What a refusal MEANS is the caller's, because the two callers answer
        to different people: a corpus batch has nobody to answer and seeds
        around it, while ``enrich fetch`` answers the owner who asked, with
        the ``--force`` route where the bound has one.

        Args:
            item_id: The owning corpus item id.
            url: The URL as captured or as named on the command line.
            via: Provenance for a spawned unit; None for a capture.
            parent: The spawning entry — what makes this a re-entry.
            force: Waive a bound that has an override.

        Returns:
            The identity, and what became of it (:class:`_Admission`).

        Raises:
            ValueError: The URL canonicalizes or detects to nothing — the
                bad-seed class both callers park.
        """
        canonical, unit_hash = self.identify(url)
        held = self.entries.get(unit_hash)
        if held is not None and not _is_cap_refusal(held):
            # One URL is one unit: a hash the ledger already holds is never
            # re-admitted (which is also the two-items dedupe edge — the
            # first item won). A cap-fire marker holds no unit, though — it
            # records refused work — so it stands in the way of nothing.
            return _Admission(hash=unit_hash, url=canonical, held=held)
        refusal = self._cap_refusal(item_id, parent, force=force)
        if refusal is not None:
            self._record_cap_fire(
                refusal, unit_hash=unit_hash, url=canonical, item_id=item_id, via=via, held=held
            )
            if not refusal.waived:
                return _Admission(
                    hash=unit_hash, url=canonical, cap=refusal.cap, reason=refusal.reason
                )
        detection = detect(url, self.ctx.drivers, sniff=self.sniff)
        entry = self.record(
            LedgerEntry(
                hash=unit_hash,
                url=canonical,
                item=item_id,
                kind=detection.kind,
                format=detection.format,
                status=Status.QUEUED,
                # The admission door is the ONE place the pre-canonical
                # spelling is in hand, so the http-share fact is recorded
                # here — captured or promoted alike. It licenses the
                # TLS-failure http fallback, and riding the line it
                # survives redrains, which frontmatter cannot cover for a
                # promotion (promoted URLs live in the ledger only).
                http_shared=url.startswith("http://"),
                engine="seed",  # stamped in record
                date=datetime.date.min,
                via=via,
                parent=None if parent is None else parent.hash,
                depth=None if parent is None else (parent.depth or 0) + 1,
            )
        )
        return _Admission(hash=unit_hash, url=canonical, entry=entry)

    def _cap_refusal(
        self, item_id: str, parent: LedgerEntry | None, *, force: bool
    ) -> _CapRefusal | None:
        """Which re-entry bound refuses a unit spawned here, and what it says.

        **The caps bound re-entry and nothing else**, so a capture is
        never asked: it has no parent, comes in at depth 0 through the
        queue's own front door, and what the owner shared is not a
        promotion for a bound to turn away. Making that the first question
        is what keeps an owner's twelve-URL capture out of the health
        check's harvest-drift reading.

        Depth is asked before the budget: a unit past the depth bound is
        out of the queue's reach whatever the item's URL count looks like.

        The two bounds differ in what may be done about them, so their
        refusals do too. The URL bound is a budget on how much of the web
        one item drags in and answers whoever asked, so ``--force`` exceeds
        it deliberately. Depth is a hard bound on how far the queue
        may walk from a shared URL; the design gives it no override, so the
        refusal names no route rather than offering one that does nothing.
        """
        if parent is None:
            return None
        depth = (parent.depth or 0) + 1
        if depth > MAX_DEPTH:
            return _CapRefusal(
                cap=Cap.DEPTH,
                reason=f"{CAP_BOUNDS[Cap.DEPTH]} reached — a hard bound, with no --force route",
                waived=False,
                parent=parent.hash,
                depth=depth,
            )
        if self.fetched_count(item_id) >= MAX_URLS_PER_ITEM:
            bound = CAP_BOUNDS[Cap.URL_REQUESTED]
            return _CapRefusal(
                cap=Cap.URL_REQUESTED,
                reason=(
                    f"{bound} exceeded by --force"
                    if force
                    else f"{bound} reached — rerun with --force to exceed"
                ),
                waived=force,
                parent=parent.hash,
                depth=depth,
            )
        return None

    def _record_cap_fire(  # noqa: PLR0913 — the refused unit's identity, entire
        self,
        refusal: _CapRefusal,
        *,
        unit_hash: str,
        url: str,
        item_id: str,
        via: str | None,
        held: LedgerEntry | None,
    ) -> None:
        """Record the marker line: refused work, named by the bound.

        The kind is pattern-detected — refused work never spends a request
        on the network it was refused from.
        """
        if held is not None and held.reason == refusal.reason:
            # A refusal that says byte-for-byte what the last one said adds
            # nothing to the audit trail.
            return
        self.record(
            LedgerEntry(
                hash=unit_hash,
                url=url,
                item=item_id,
                kind=detect_kind(url, self.ctx.drivers),
                status=Status.SKIPPED,
                cap=refusal.cap,
                forced=refusal.waived,
                engine="seed",  # stamped in record
                date=datetime.date.min,
                via=via,
                parent=refusal.parent,
                depth=refusal.depth,
                reason=refusal.reason,
            ),
            # A waived fire is the audit trail behind the queued line that
            # supersedes it in the same breath, never an outcome to count.
            count=not refusal.waived,
        )

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
        """Process one unit; False means a deferred no-op (nothing spent)."""
        # The media stage gives blocked downloads normal retry rules, and
        # the typed job field is what routes them back to the redrain.
        media_job = entry.job is Job.MEDIA
        if media_job and self.ctx.config.media_fetch is MediaFetch.NONE:
            # `none` gates the download, and THIS is the one gate: every
            # media unit is ledgered at emit, so a freshly emitted child
            # and a redrained one rest identically — same status, no
            # attempts burned, like a waiting unit under an absent
            # provider — and drain again when the owner turns media on.
            if not self.deferred_media:
                self.notes.append(
                    "media_fetch is `none` — media downloads rest where they are "
                    "until it is turned back on"
                )
            self.deferred_media += 1
            return False
        transcribe_job = not media_job and _is_transcribe_job(entry)
        if transcribe_job and not self._transcribe_slot():
            return False  # stays waiting untouched; the deferral is noted once
        try:
            # The try spans ALL per-unit processing:
            # fetch/transcribe/download, output write, media stage, asset
            # writes. An engine bug anywhere in it ledgers `error` — the
            # outcome line supersedes any partial one — never aborts the run.
            if media_job:
                self._download_media(entry)
            elif transcribe_job:
                self._transcribe_unit(entry)
            else:
                self._drive_unit(entry)
        except ProviderInputError as e:
            self.record_outcome(
                entry, status=Status.MANUAL, reason=_provider_input_reason(entry, e)
            )
        except Exception as e:  # noqa: BLE001 — the per-unit broad catch, one of two
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
            item=self.owner_of(entry),
            depth=entry.depth or 0,
            parent=entry.parent,
        )
        try:
            fetched = driver.fetch(unit)
            retry = self._http_retry_unit(entry, unit, fetched)
            if retry is not None:
                self.notes.append(
                    f"{unit.url}: https refused at the TLS layer — refetched over "
                    "http, which is how the owner shared this source"
                )
                self.ctx.sleep(driver.sleep)  # two requests, politeness between them
                fetched = driver.fetch(retry)
            self._apply(entry, fetched)
        finally:
            # Politeness holds even when the fetch failed.
            self.ctx.sleep(driver.sleep)

    def _http_retry_unit(
        self, entry: LedgerEntry, unit: WorkUnit, fetched: Outcome
    ) -> WorkUnit | None:
        """The http-variant retry for a TLS-refused fetch, or None.

        Canonicalization forces https, so an http-only source is asked for
        over a TLS it does not speak and parks blocked forever. When the
        https fetch failed at the TLS layer (the typed ``Refused.tls``
        marker, never the evidence prose) AND the source was actually
        shared as http, the same unit is refetched over http — identity,
        hashing and the ledger URL all unchanged; only the wire request
        downgrades. A TLS failure on a genuinely https-shared URL never
        downgrades: it stays blocked, exactly as before. The check lives
        here, at the run/fetch seam, so drivers stay ignorant of it.

        Two licenses, either one enough: a live frontmatter listing the
        http variant (:attr:`http_shared`, read at seeding — it covers
        every captured line, however old), or the entry's own typed
        ``http_shared`` flag, stamped at the admission door — the only
        record a PROMOTED http URL has, since promoted URLs never live in
        frontmatter, and the record that keeps a redrain licensed.
        """
        if not (isinstance(fetched, Refused) and fetched.tls):
            return None
        licensed = unit.hash in self.http_shared or entry.http_shared
        if not licensed or not unit.url.startswith("https://"):
            return None
        return dataclasses.replace(unit, url="http://" + unit.url.removeprefix("https://"))

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
            case Kind.INSTAGRAM:
                body = instagram_body(acquired.prefix, transcript)
            case _:
                raise RuntimeError(
                    f"no transcript body for kind '{entry.kind}' — _acquire_audio "
                    "and the body dispatch must cover the same kinds"
                )
        path = self._write_output(entry, meta, body)
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
        # Every transcribable kind parks with content already in hand (a
        # description, show notes, a caption) and composes the transcript
        # onto what that park wrote, so each acquisition reads the unit's
        # own output file.
        name = f"{entry.kind.value}-{entry.hash[:6]}.md"
        enrichment = self.ctx.instance.enrichment_dir / self.owner_of(entry) / name
        match entry.kind:
            case Kind.YOUTUBE:
                return acquire_youtube_audio(entry, enrichment, audio_dir, self.ctx.download_audio)
            case Kind.PODCAST:
                return acquire_podcast_audio(entry, enrichment, audio_dir, self.ctx.transport)
            case Kind.INSTAGRAM:
                return acquire_instagram_audio(entry, enrichment, audio_dir, self.ctx.transport)
            case _:
                return Classification(
                    status=Status.MANUAL,
                    reason=(
                        f"no audio-acquisition path for kind '{entry.kind}' — "
                        "transcription covers youtube, podcast and instagram work"
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
            case Status.DEAD if entry.kind is Kind.INSTAGRAM:
                # The URL that died is the media proxy's, not Instagram's —
                # an unmaintained host going dark is its expected end, and
                # says nothing about the reel.
                self.record_outcome(
                    entry,
                    status=Status.MANUAL,
                    reason=(
                        f"the media proxy stopped serving the video ({failure.reason}) — "
                        "requeue the unit so the instagram driver re-resolves it, or point "
                        "instagram_base_url at another host"
                    ),
                )
            case Status.DEAD | Status.MANUAL:
                self.record_outcome(entry, status=failure.status, reason=failure.reason)
            case _:
                raise RuntimeError(f"unclassifiable acquisition failure {failure.status!r}")

    def _apply(self, entry: LedgerEntry, fetched: Outcome) -> None:
        """Map what the driver FOUND onto the unit's lifecycle — the one total match.

        The driver states its finding as a typed outcome; what that means
        for the unit's status is decided here and nowhere else. ``Missing``
        is the only road to ``dead``. The match is total over the union —
        a new outcome variant is a type error at this site until an arm
        says what it means.
        """
        match fetched:
            case Content():
                self._apply_content(entry, fetched)
            case Missing(evidence=evidence):
                self.record_outcome(entry, status=Status.DEAD, reason=evidence)
            case Refused(evidence=evidence, permanent=True):
                # Retrying can never change the answer, so the blocked
                # lifecycle's attempts would teach nothing: the engine
                # gives the unit up for judgment now.
                self.record_outcome(entry, status=Status.MANUAL, reason=evidence)
            case Refused(evidence=evidence):
                self._apply_blocked(entry, evidence)
            case Unusable(evidence=evidence, rescuable=True):
                self.record_outcome(entry, status=Status.MANUAL, reason=evidence)
            case Unusable(evidence=evidence):
                # Nothing there for judgment either: closed out, owing
                # nothing, holding nothing hostage.
                self.record_outcome(entry, status=Status.SKIPPED, reason=evidence)
            case NeedsCapability():
                self._apply_needs(entry, fetched)
            case Redetected():
                self._apply_redetection(entry, fetched)
            case _:
                assert_never(fetched)

    def _apply_content(self, entry: LedgerEntry, content: Content) -> None:
        path = None
        if content.body is not None:
            path = self._write_output(entry, content.meta, content.body)
            _drop_superseded_outputs(self.ctx.instance, entry, path)
        title = content.meta.get("title")
        self.record_outcome(
            entry,
            status=Status.DONE,
            path=path,
            title=title if isinstance(title, str) and path is not None else None,
        )
        if content.assets:
            self._write_assets(self.entries[entry.hash], content.assets)
        if content.media:
            # ALWAYS ledgered at emit, whatever `media_fetch` says: the
            # config gates the download alone (`_process`), so under
            # `none` the child rests queued — the one mechanism the
            # redrain's resting path already is — and drains the moment
            # the owner turns media back on. Gating the emit dropped the
            # URLs outright: no line, no note, no recovery.
            self._media_stage(self.entries[entry.hash], content.media)

    def _apply_needs(self, entry: LedgerEntry, needs: NeedsCapability) -> None:
        if needs.body is not None or needs.meta.get("enclosure") is not None:
            # A parking driver may still have real content (a podcast's
            # show notes, the enclosure pointer in meta) — written now,
            # completed by the drain; the item is not yet cognitive work,
            # so the write is not an outcome. A waiting-transcribe park
            # carrying an enclosure ALWAYS writes its park file: the drain
            # re-fetches the audio from that frontmatter pointer, show
            # notes or not — a no-notes episode without it would loop
            # manual.
            self._write_output(entry, needs.meta, needs.body, count=False)
        self.record_outcome(entry, status=Status.WAITING, needs=needs.need, reason=needs.reason)

    def _apply_redetection(self, entry: LedgerEntry, redetect: Redetected) -> None:
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
                item=self.owner_of(entry),
                kind=redetect.kind,
                format=redetect.format,
                status=Status.QUEUED,
                http_shared=entry.http_shared,
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

    def _apply_blocked(self, entry: LedgerEntry, reason: str, *, needs: Need | None = None) -> None:
        attempts = (entry.attempts or 0) + 1
        if attempts >= MAX_BLOCKED_ATTEMPTS:
            # Escalation appends attempt context to what was found —
            # it never invents a reason.
            self.record_outcome(
                entry,
                status=Status.MANUAL,
                reason=f"still blocked after {attempts} attempts — {reason}",
            )
            return
        self.record_outcome(
            entry, status=Status.BLOCKED, needs=needs, attempts=attempts, reason=reason
        )

    # -- outputs ---------------------------------------------------------

    def _write_output(
        self,
        entry: LedgerEntry,
        meta: dict[str, str | int | None],
        body: str | None,
        *,
        count: bool = True,
    ) -> str:
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

        The directory is the item the corpus says owns the unit
        (:meth:`owner_of`), never the line's stored string: a renamed
        item's waiting unit drained on the string writes its enrichment
        into ``enrichment/<dead-id>/``, where the item that owes the work
        cannot list it.
        """
        owner = self.owner_of(entry)
        name = f"{entry.kind.value}-{entry.hash[:6]}.md"
        out = self.ctx.instance.enrichment_dir / owner / name
        content = render_enrichment(entry.url, self.ctx.today(), meta, body or "")
        existed = out.exists()
        if existed and mask_fetched(out.read_text(encoding="utf-8")) == mask_fetched(content):
            # Nothing to write, but something to account for. Reporting this
            # as no outcome at all had a fetch print "2 units processed,
            # done 2" above "Nothing to report from this run: no new
            # material" — the run contradicting itself over content that
            # was sitting on disk the whole time.
            if count:
                self.outcomes.setdefault(owner, _ItemOutcome()).unchanged += 1
            return str(out.relative_to(self.ctx.instance.root))
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(out, content)
        # Counted AFTER the write lands: the mkdir and the write can both
        # raise (a file squatting where the directory belongs), the unit
        # then ledgers `error` — and an outcome registered first had the
        # report claiming a new enrichment file while listing the same
        # item under Needs writing up, with nothing on disk to write up.
        if count:
            outcome = self.outcomes.setdefault(owner, _ItemOutcome())
            if existed:
                outcome.changed += 1
            else:
                outcome.new += 1
        return str(out.relative_to(self.ctx.instance.root))

    # -- extraction assets ----------------------------------------

    def _write_assets(self, entry: LedgerEntry, assets: list) -> None:
        """Write embedded assets under the media caps, ledgered ``job: asset``.

        Deterministic names (``<hash6>-asset-<n>.<ext>``) make reruns
        overwrite, never duplicate; the caps (4 files per item, 10MB per
        file) are shared with the media stage's downloads.

        ``entry`` is the ``done`` line :meth:`_apply_done` just recorded,
        so its ``item`` is already the owning one — as it is for every
        other write this success fans out to.
        """
        owner = entry.item
        for index, asset in enumerate(assets):
            name = f"{entry.hash[:6]}-asset-{index}.{asset.suggested_ext}"
            out = self.ctx.instance.enrichment_dir / owner / name
            rel = f"enrichment/{owner}/{name}"
            if len(asset.data) > MEDIA_MAX_BYTES:
                self._asset_outcome(
                    entry, rel, status=Status.SKIPPED, reason="asset exceeds 10MB ceiling"
                )
                continue
            if not out.exists() and self._media_file_count(owner) >= MEDIA_MAX_FILES:
                self._asset_outcome(
                    entry,
                    rel,
                    status=Status.SKIPPED,
                    reason=f"media cap ({MEDIA_MAX_FILES} files) reached",
                )
                continue
            changed = not out.exists() or out.read_bytes() != asset.data
            out.parent.mkdir(parents=True, exist_ok=True)
            atomic.write_bytes(out, asset.data)
            if changed:
                self.outcomes.setdefault(owner, _ItemOutcome()).media += 1
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
                job=Job.ASSET,
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
                job=Job.MEDIA,
                parent=parent.hash,
                depth=(parent.depth or 0) + 1,
            )
            # The birth line lands BEFORE the download (entries exist from
            # birth) — a crash mid-download leaves a queued media entry
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
                job=Job.MEDIA,
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
        outcome = fetch_classified(self.ctx.transport, entry.url, limit=MEDIA_MAX_BYTES)
        if isinstance(outcome, FetchFailure):
            failure = outcome.classification
            self._media_failure(entry, failure.status, failure.reason)
            return
        response = outcome
        if len(response.body) > MEDIA_MAX_BYTES:
            # Backstop for servers that lie about (or omit) Content-Length.
            # The ceiling rides the fetch, so the transport stopped reading
            # one byte past it — the over-ceiling body was never held whole.
            self.record_outcome(entry, status=Status.SKIPPED, reason="media exceeds 10MB ceiling")
            return
        if not response.body:
            # No image or video is zero bytes, and a 200 carrying nothing
            # says nothing about the MEDIA — the Instagram embed proxies
            # answer an out-of-range index with exactly this shape — so it
            # earns the retries a flaky server gets, never a done ledger
            # line over an empty file.
            self._media_failure(entry, Status.BLOCKED, "media URL returned an empty response body")
            return
        owner = self.owner_of(entry)
        out = (
            self.ctx.instance.enrichment_dir
            / owner
            / f"media-{slot}.{ext_of(entry.url, default='jpg')}"
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        atomic.write_bytes(out, response.body)
        self.outcomes.setdefault(owner, _ItemOutcome()).media += 1
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
        owner = self.owner_of(entry)
        slot = self._media_position(entry)
        item_dir = self.ctx.instance.enrichment_dir / owner
        if any(_is_media_file(path) for path in item_dir.glob(f"media-{slot}.*")):
            return slot
        if self._media_file_count(owner) >= MEDIA_MAX_FILES:
            return None
        return slot

    def _media_position(self, entry: LedgerEntry) -> int:
        """How many of the item's media units were ledgered before this one.

        Counted regardless of status: the position is an identity, so a
        parked or skipped sibling still holds its place — a position that
        moved as statuses changed would rename files under the item.

        The item is the owning one on both sides of the comparison
        (:meth:`owner_of`) — the slot names a file in ``enrichment/<owner>/``,
        so counting the stored string's siblings would let a renamed item's
        media and its own collide on one index.
        """
        owner = self.owner_of(entry)
        seen = 0
        for unit_hash, other in self.entries.items():
            if unit_hash == entry.hash:
                break
            if self.owner_of(other) == owner and other.job is Job.MEDIA:
                seen += 1
        return seen

    def fetched_count(self, item_id: str) -> int:
        """Fetched-page entries the item owns — what the 12-URL cap bounds.

        Media downloads, extraction-asset byte-writes, and cap-skip marker
        lines don't count: none of them is a fetched page, and a document
        rich in embedded images must not spend the item's URL budget.

        Which entries are the item's is the corpus's answer
        (:meth:`owner_of`, off the owner map the drain already holds) —
        counting the stored string finds nothing under a renamed item's
        live id, ever, so its budget restarted from zero at every rename
        and the cap never fired, which also silences the drift reading a
        cap fire feeds the health check.

        The answer is a maintained count, not a recount: one pass over the
        entries builds the per-item table, `record` — the one door entries
        change through — keeps it current per write, and re-resolving
        ownership drops it (the attribution the counts key on has a new
        answer). At every read it equals the full recount by the rule
        above.
        """
        if self._fetched_counts is None:
            counts: dict[str, int] = {}
            for entry in self.entries.values():
                if _spends_url_budget(entry):
                    owner = self.owner_of(entry)
                    counts[owner] = counts.get(owner, 0) + 1
            self._fetched_counts = counts
        return self._fetched_counts.get(item_id, 0)

    # -- recording -------------------------------------------------------

    def record(self, entry: LedgerEntry, *, count: bool = False) -> LedgerEntry:
        stamped = ledger.stamp(
            entry,
            today=self.ctx.today,
            now=self.ctx.now,
            engine_version=self.ctx.engine_version,
        )
        self._appender.append(stamped)
        self._count_write(stamped)
        self.entries[stamped.hash] = stamped
        self.written[stamped.hash] = stamped
        if count:
            self.counts[stamped.status] = self.counts.get(stamped.status, 0) + 1
            self.touched.add(stamped.item)
        if count and stamped.status in _PARKED:
            self.parked.append(
                _parked_row(stamped, stamped.item, media_fetch=self.ctx.config.media_fetch)
            )
        return stamped

    def _count_write(self, stamped: LedgerEntry) -> None:
        """Keep the fetched counts equal to a recount across one write.

        Called before ``self.entries`` takes the new line, while the line it
        supersedes is still readable: the superseded line leaves the count
        it was in, the new one enters the count its owner and status say —
        which is how a skip landing on a queued unit gives the budget back,
        exactly as a recount would.
        """
        if self._fetched_counts is None:
            return
        previous = self.entries.get(stamped.hash)
        if previous is not None and _spends_url_budget(previous):
            self._fetched_counts[self.owner_of(previous)] -= 1
        if _spends_url_budget(stamped):
            owner = self.owner_of(stamped)
            self._fetched_counts[owner] = self._fetched_counts.get(owner, 0) + 1

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
        """Write the outcome for a drained entry, provenance carried forward.

        Everything but the attribution is carried from the drained line;
        the attribution is asked of the corpus (:meth:`owner_of`), so a
        renamed item's work is recorded against the item that owes it
        rather than against the id the line was written under.
        """
        self.record(
            LedgerEntry(
                hash=entry.hash,
                url=entry.url,
                item=self.owner_of(entry),
                kind=entry.kind,
                format=entry.format,
                status=status,
                needs=needs,
                attempts=attempts,
                http_shared=entry.http_shared,
                engine="seed",  # stamped in _record
                date=datetime.date.min,
                job=entry.job,
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
        """List the items the unit-driven report cannot see, or misstates.

        Two families, one owed job. A no-source capture seeded nothing, so
        it never appears among the drained units; an item whose every unit
        closed without content has units that all appear, but only as a
        count that says nothing about what the item still owes. Both owe
        description + digest from the owner's note, and both are named on
        the report until the digest lands.

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
        # Digest coverage is filename-resolved (:func:`digested_items`): a
        # renamed item's digest sits under the old id, and asking for the
        # live id's file re-listed the item as owing work it recorded.
        digested = digested_items(self.ctx.instance, set(self.item_status))
        self.no_source_items = [
            item_id
            for item_id, status in sorted(self.item_status.items())
            if status == "raw" and item_id not in sourced and item_id not in digested
        ]
        # The closed-without-content sibling: every unit landed `dead` or
        # was deliberately ruled out, so the engine owes nothing further
        # and no enrichment exists — the owed work is the same description
        # + digest, from the owner's note. Same shared reading as the
        # digest backstop (:func:`_terminal_no_content_items`), and it
        # drops off the moment the item's digest is recorded.
        self.closed_unenriched_items = [
            item_id
            for item_id in _terminal_no_content_items(
                self.entries, self.owners, set(self.item_status)
            )
            if item_id not in digested
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
        items += [
            {
                "id": item_id,
                "reason": (
                    "every source dead or ruled out — awaiting description + digest from the note"
                ),
            }
            for item_id in self.closed_unenriched_items
        ]
        payload: dict[str, object] = {
            "counts": {status.value: n for status, n in self.counts.items()},
            "items": items,
            "parked": self.parked,
        }
        unchanged = [
            {"id": item_id, "count": outcome.unchanged}
            for item_id, outcome in sorted(self.outcomes.items())
            if outcome.unchanged
        ]
        if unchanged:
            payload["unchanged"] = unchanged
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

        Cap-fire markers are not units: a fetch batch that overran a bound
        recorded refused work, and refused work never landed. Counted as
        landed they inflate both halves of the shape — "15 of 16 units
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

        The item named is the corpus's owner (:meth:`owner_of`), like every
        other surface: the row is the manual-work pointer, and a renamed
        item's stored string sends the session into ``enrichment/<dead-id>/``
        while ``enrich status`` names the live one.
        """
        capabilities = self.ctx.capabilities
        if capabilities is None:
            return []
        return [
            {"item": self.owner_of(entry), "url": entry.url, "need": entry.needs.value}
            for entry in self.entries.values()
            if entry.status is Status.WAITING
            and entry.needs is not None
            and capabilities.is_cognitive(entry.needs, entry.format)
        ]


def _parked_row(entry: LedgerEntry, item_id: str, *, media_fetch: MediaFetch) -> dict[str, object]:
    """One parked entry as both report surfaces read it.

    The run report says what THIS run parked and the standing view says
    what is parked now; they are the same shape read at two moments, so
    they are built here once.

    Built here, with the config in hand, is also where a resting media
    unit is told apart: under ``media_fetch: none`` the drain defers every
    media job, so a parked one waits on the owner flipping the config, not
    on any retry the engine will make. The renderer cannot ask the config
    (payloads are self-contained), so the row itself carries the
    classification — marked ``resting``, reason naming what unblocks it,
    and no attempt count, because "attempt n of m" is retry framing for
    retries that are switched off. ``manual`` media rows keep their own
    stated reason: they wait on the owner either way, and turning media
    back on resumes nothing for them.
    """
    if (
        entry.job is Job.MEDIA
        and media_fetch is MediaFetch.NONE
        and entry.status is not Status.MANUAL
    ):
        return {
            "item": item_id,
            "url": entry.url,
            "status": entry.status.value,
            "reason": "media_fetch is `none` — stays parked until it is turned back on",
            "resting": True,
        }
    row: dict[str, object] = {
        "item": item_id,
        "url": entry.url,
        "status": entry.status.value,
        "reason": entry.reason
        or entry.error
        or (f"waiting on {entry.needs}" if entry.needs else "no reason recorded"),
    }
    if entry.attempts is not None:
        # The report splits parked entries by who owns the next action, and
        # for a blocked one the answer is "the engine, this many more
        # times" — which only the cap makes readable.
        row["attempts"] = entry.attempts
        row["attempt_cap"] = MAX_BLOCKED_ATTEMPTS
    return row


def _drop_superseded_outputs(instance: Instance, entry: LedgerEntry, path: str) -> None:
    """Drop the unit's earlier-kind output — once ``path`` replaces it.

    A redetection relabels a unit, so its output file is renamed by kind.
    The stale file leaves the disk here, on the success that replaces it,
    and never earlier: a corrected fetch that parks must leave the item
    exactly as enriched as it found it. The ledger's audit trail keeps the
    history either way. Every route to a unit's own output comes through
    here — the drain's fetch write, the transcribe drain's landing, and a
    hand-written file closed by ``mark``.

    The directory searched is the replacement's own, not the one the
    entry's ``item`` names: a unit's outputs all live beside each other by
    construction, and the stored string is the attribution as of the day
    the line was written, which a rename since leaves naming nothing.

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
    item_dir = (instance.root / path).parent
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
    drivers: Sequence[SourceDriver] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Which live corpus items each work unit belongs to.

    The one resolution is :func:`dex_engine.pipeline.ownership.unit_owners`
    — frontmatter claims first, the ``parent`` chain for the units no
    frontmatter names, the stored string last — shared with lint so every
    reader and every write answers ownership identically. This wrapper only
    supplies the driver registry for the standing-report paths that hold no
    RunContext.

    Args:
        instance: The instance.
        entries: The ledger, hash -> latest entry.
        drivers: The driver registry that owns canonicalization; built
            fresh when the caller has none in hand.

    Returns:
        Work hash -> the owning item ids, in id order. Every hash in
        ``entries`` has an answer; none is ever empty.
    """
    return unit_owners(
        instance.root, entries, drivers if drivers is not None else default_drivers()
    )


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
    """``enriched`` iff the item owes no further work.

    An item is one unit of knowledge — the post, the thread parents above
    it, the links harvest promoted, the media, the video awaiting its
    transcript — so one outstanding unit keeps the whole item ``raw`` and
    out of digest/wiki. A dead link or a deliberately skipped unit owes
    nothing and holds nothing hostage.

    What it OWES is the whole question, and the ledger answers it through
    the ownership map (``units``) — never the item's own directory
    listing. Seeding dedupes by work hash, so a URL shared into two
    captures is ledgered once and its enrichment lands under the first
    item only; reading the second item's empty directory as "not
    enriched" left it ``raw`` permanently, on no surface, with no verb
    that could move it — ``fetch`` refuses a URL another item enriches,
    ``mark`` heals the unit, which was never the thing that was wrong,
    and a digest pass cannot lift a status it does not derive. The
    listing is where the item's OWN files are, which is a different
    question from who owes what.

    The listing still answers for one item: the capture that seeded
    nothing at all. No units and no files is a text/image-only share,
    which owes its description and its digest — and an ``any()`` over no
    units would call that finished.
    """
    if units is None:
        return item.status
    if not units and not files:
        return "raw"
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
    drain.close_ledger()
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
    targets = {unit_hash for unit_hash, entry in drain.entries.items() if _is_transcribe_job(entry)}
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

    This is the ONE way a link is promoted: which links are primary
    artifacts of the item's subject is judgment, so a session names
    them and the engine bounds what it is handed. ``parent`` defaults to the
    item's primary work unit; depth is the parent's + 1; both re-entry caps
    bound the admission. ``--force`` may exceed the URL cap for an
    owner-requested deepen — the cap fire is still recorded (a superseded
    skipped line in the audit trail) — and reaches no further: the depth
    bound has no override.

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
    """The stated parent, or the item's primary unit — the corpus's answer.

    Which units are the item's is asked of the corpus
    (:meth:`_Drain.owners_of`), never of the line's stored string: a
    renamed item's primary unit sits in the ledger under the dead id, and
    scanning the string told its owner the item had no primary work unit —
    both implied causes false — while demanding a ``--parent`` hash for a
    unit the ledger held all along.

    The question is MEMBERSHIP among the unit's claimants, not equality
    with one name: seeding hands a shared URL's unit to the first claimant
    and the line names only that one, but every item listing the URL owns
    the unit — asked as equality, only the first claimant could ever
    ``enrich fetch``, and the second was told the same two false causes.
    """
    if parent is not None:
        entry = drain.entries.get(parent)
        if entry is None:
            raise ValueError(f"--parent {parent!r} is not a ledger entry")
        return entry
    for entry in drain.entries.values():
        if item_id in drain.owners_of(entry) and (entry.depth or 0) == 0:
            return entry
    raise ValueError(f"item {item_id!r} has no primary work unit — pass --parent <hash> explicitly")


def _is_cap_refusal(entry: LedgerEntry) -> bool:
    """True for a cap-fire marker line — refused work, not an admitted unit."""
    return entry.cap is not None


def _requeue_in_place(drain: _Drain, existing: LedgerEntry) -> str:
    """Re-fetch a unit the item already owns: same depth/parent/via, rerun.

    Never re-parents (re-fetching the item's primary URL must not nest it
    under itself) and never re-spends the URL cap — the unit is already
    counted. The attribution is asked of the corpus
    (:meth:`_Drain.owner_of`), like every write: carried from the held
    line it spells a renamed item's dead id, and a run dying between this
    line and the drain's outcome leaves that spelling standing.
    """
    requeued = dataclasses.replace(
        existing,
        item=drain.owner_of(existing),
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
    """Admit one URL of an ``enrich fetch`` batch, answering the owner who asked.

    :meth:`_Drain.admit` owns the invariants; this owns the batch policy
    for URLs the owner typed — **one bad URL never aborts it**, and every
    refusal comes back as an answer on the report rather than as a raise.
    """
    try:
        admission = drain.admit(item_id, url, via="harvest", parent=parent, force=force)
    except ValueError as e:
        # A URL that cannot be canonicalized — or that the sniff HEAD
        # refuses, the same bad-seed class — parks alone; the URLs already
        # ledgered ahead of it keep their report.
        drain.park_bad_seed(item_id, url, e, what="fetch URL")
        return None
    if admission.held is not None:
        # Whose unit the held line is is the corpus's answer
        # (:meth:`_Drain.owner_of`), never the stored string: a renamed
        # item's own pre-rename unit spells the dead id, and comparing the
        # string refused it as another item's — naming an id no corpus file
        # answers to — leaving the requeue-in-place route unreachable.
        owner = drain.owner_of(admission.held)
        if owner != item_id:
            # One URL enriches under one item — but refusing the BATCH would
            # abort the URLs already ledgered ahead of this one and swallow
            # the report with them. The refusal is reported; the rest fetch.
            drain.notes.append(
                f"{admission.url} already enriches under item {owner} — one URL "
                f"enriches under one item; not fetched into {item_id}"
            )
            return None
        return _requeue_in_place(drain, admission.held)
    if admission.entry is None:
        # An owner-requested refusal IS surfaced: the owner asked, and a
        # bare "skipped 1" leaves the stated route unreachable.
        drain.notes.append(f"not fetched — {admission.url}: {admission.reason}")
        return None
    return admission.hash


def status_report(ctx: RunContext, *, item_id: str | None = None) -> str:
    """Ledger summary, parked standing view, digest backstop, capabilities.

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
    parked = _parked_units(ctx, entries)
    if parked:
        payload["parked"] = parked
    orphans = digest_orphans(ctx.instance)
    if orphans:
        payload["orphans"] = orphans
    report = surfaces.render("status", payload)
    if ctx.capabilities is not None:
        report += "\n" + surfaces.render("capability-report", ctx.capabilities.report_payload())
    return report


def _parked_units(ctx: RunContext, entries: dict[str, LedgerEntry]) -> list[dict[str, object]]:
    """Every unit parked right now — what outlived the run that parked it.

    The session-end invariant is that the only entries surviving a
    session are ``waiting`` / ``blocked`` / ``error`` / ``manual``, each
    parked for a stated reason and each printed. The run report prints what
    ITS run parked; a unit that outlives that run is untouched standing
    state, which is this view's job — and it had no view at all. A
    ``manual`` line three months old rendered as ``**manual** 1`` and
    nowhere else: no id, no URL, no reason, while its item sat permanently
    raw, permanently undigestable, and named on no surface.

    A unit is one row however many items list it: the URL is its identity,
    and the item named is the first that claims it, as seeding's dedupe
    names it.

    Args:
        ctx: The run context.
        entries: The ledger, hash -> latest entry.

    Returns:
        The parked rows, by item then URL.
    """
    owners = _unit_owners(ctx.instance, entries, ctx.drivers)
    rows = [
        _parked_row(
            entry,
            owners.get(entry.hash, (entry.item,))[0],
            media_fetch=ctx.config.media_fetch,
        )
        for entry in entries.values()
        if entry.status in _PARKED
    ]
    rows.sort(key=lambda row: (str(row["item"]), str(row["url"])))
    return rows


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
        if entry.job is not None:
            unit["job"] = entry.job.value
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

    Who is a candidate is asked of the ledger as well as the disk. An
    enrichment directory holding markdown is one answer — it covers files
    no ledger line wrote, like a session's media description. A live item
    the ledger records a landing for is another, and it is the only
    answer a co-claimant has: seeding dedupes by work hash, so a URL
    shared into two captures enriches under the first item, and the
    second — which derives ``enriched`` on the same landing, and is
    therefore digestible — has no directory to be found by. A live item
    whose every unit closed without content — ``dead``, or deliberately
    ruled out ``skipped`` — is the third (:func:`_terminal_no_content_items`):
    nothing landed and nothing owes, so neither of the first two answers
    finds it, yet its description and digest — from the owner's note —
    are exactly the work still owed.

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
    owing = items_owing_work(entries, owners)
    enriched_on = _last_enriched(entries, owners)
    live = {path.stem for path in instance.corpus_dir.glob("*/*.md")}
    recorded = digested_items(instance, live)
    # A digest pass record names the item as of the day it was recorded,
    # like the file it covers: resolved the same way, so a pre-rename pass
    # still dates the live item's digest and the staleness comparison
    # survives the rename.
    digested_on: dict[str, datetime.date] = {}
    for recorded_item, date in _last_digested(instance).items():
        item_id = _dir_owner(recorded_item, live)
        digested_on[item_id] = max(digested_on.get(item_id, date), date)
    # A directory's name is the attribution as of the day it was written,
    # like a ledger line's item: a rename interrupted between the corpus
    # file and the enrichment directory leaves the directory under the
    # dead old id, which the digest verb will refuse. So the candidate a
    # directory names is ownership-resolved (:func:`_dir_owner`), and the
    # mid-rename item is listed once, under the live id.
    candidates = {
        _dir_owner(item_dir.name, live)
        for item_dir in instance.enrichment_dir.iterdir()
        if item_dir.is_dir() and any(item_dir.glob("*.md"))
    }
    # A landing counts for the item the corpus says owns it, whether or not
    # that item has a directory — but only while the item is LIVE: a done
    # line naming an id no corpus file answers to is lint's ghost-item
    # finding, never work this backstop can ask anyone to do.
    candidates |= enriched_on.keys() & live
    # The third answer: an item whose every unit closed without content
    # (dead, or ruled out skipped). It has no enrichment directory and no
    # done line, so neither route above finds it — yet it owes exactly
    # what a no-source capture owes, description and digest from the
    # owner's note, and the no-digest branch below is what lists it. Once
    # its digest pass is recorded it drops off like any other item
    # (nothing ever landed, so nothing is ever stale).
    candidates.update(_terminal_no_content_items(entries, owners, live))
    for item_id in sorted(candidates):
        if item_id in owing:
            continue
        if item_id not in recorded:
            orphans.append(item_id)
            continue
        landed = enriched_on.get(item_id)
        digested = digested_on.get(item_id)
        # No dates on either side is no claim: enrichment written outside
        # the ledger (a media description) and digests older than the pass
        # record are facts about history, not staleness.
        if landed is not None and digested is not None and digested < landed:
            orphans.append(item_id)
    return orphans


def _dir_owner(name: str, live: set[str]) -> str:
    """The live item an id-named artifact belongs to, else the name itself.

    A name matching a live item answers for itself. One matching no live
    item is resolved by its trailing shortid — a rename keeps the shortid
    and rewrites the slug, the same trailing-id match the exclusions and
    the harvest-pass reader use across renames — and attributes to the one
    live item carrying it. No live match, or two (six hex digits can
    collide), resolves nothing: the name stands, as it always did, and
    what it means is a finding, not an attribution to guess at.

    The names resolved this way are the id-keyed leftovers a rename can
    strand: an enrichment directory, a ``state/digests/<id>.md`` file
    (:func:`digested_items`), and a digest pass record's item.
    """
    if name in live:
        return name
    shortid = name.rsplit("-", 1)[-1]
    matches = [item_id for item_id in live if item_id.rsplit("-", 1)[-1] == shortid]
    return matches[0] if len(matches) == 1 else name


def digested_items(instance: Instance, live: set[str]) -> set[str]:
    """The item each recorded digest answers for — filenames ownership-resolved.

    A digest file's name is the attribution as of the day it was written,
    like an enrichment directory's: the rename procedure moves the corpus
    file and the enrichment directory, and ``state/digests/<old-id>.md``
    stays behind. Resolved by the same trailing-shortid rule
    (:func:`_dir_owner`), the renamed item keeps the digest it already
    recorded — asking for the live id's filename instead re-listed the
    item as owing a digest, and the re-digest that answered it left a
    ninth digest beside eight items, named on no surface, forever. A stem
    that resolves to nothing live stands as itself: the digest covers no
    live item, and the item missing one is still surfaced.

    Args:
        instance: The instance.
        live: The live corpus item ids.

    Returns:
        The resolved owner of every ``state/digests/*.md`` file.
    """
    if not instance.digests_dir.is_dir():
        return set()
    return {_dir_owner(path.stem, live) for path in instance.digests_dir.glob("*.md")}


def items_owing_work(
    entries: dict[str, LedgerEntry] | None, owners: Mapping[str, tuple[str, ...]]
) -> set[str]:
    """The items a unit is still outstanding on — the ones deriving ``raw``.

    Read off the same ownership map the frontmatter's ``raw`` is derived
    from (:func:`_unit_owners`): an item held out of digest by an
    outstanding unit and an item listed as owing one have to be the same
    item, or the backstop names work the ingest procedure forbids doing.
    Public because lint's coverage row applies the same exemption: a
    correctly parked item is not drift to place, it is work still owed.

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


# The statuses that close a unit without producing content: confirmed gone,
# or deliberately ruled out (a cap-free skip — the skills' own heal marks a
# dead unit's sibling skipped with the reason stated). `done` landed
# content; everything else is outstanding.
_TERMINAL_NO_CONTENT = frozenset({Status.DEAD, Status.SKIPPED})


def _terminal_no_content_items(
    entries: Mapping[str, LedgerEntry] | None,
    owners: Mapping[str, tuple[str, ...]],
    live: set[str],
) -> list[str]:
    """The live items whose every ledger unit closed without content, sorted.

    One shared reading for the surfaces that owe such an item a listing —
    the run report and the digest backstop: every source confirmed gone or
    deliberately ruled out means the engine owes nothing further and no
    enrichment exists, so the item derives ``enriched`` with nothing to
    digest FROM but the owner's note — which is exactly its owed work,
    description + digest, and neither surface could see it as more than a
    count. Dead-only was the first spelling of this predicate, and it let
    the mixed item vanish: the skills' own heal marks a dead unit's
    sibling ``skipped`` with a reason, and that item owes exactly the same
    note-digest.

    Ownership is the corpus's answer, like every other reading off these
    maps. Cap-fire markers are not units — they record refused work — so
    they neither close an item nor break the reading. An item with no
    units at all is the no-source capture, a different listing. An
    unreadable ledger claims nothing.
    """
    if not entries:
        return []
    statuses: dict[str, set[Status]] = {}
    for entry in entries.values():
        if _is_cap_refusal(entry):
            continue
        for item_id in owners.get(entry.hash, (entry.item,)):
            statuses.setdefault(item_id, set()).add(entry.status)
    return sorted(
        item_id
        for item_id, seen in statuses.items()
        if item_id in live and seen <= _TERMINAL_NO_CONTENT
    )


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


def never_harvested(instance: Instance) -> list[str]:
    """Items whose fetched pages landed with no harvest pass ever recorded.

    A recorded pass is what makes "ran and promoted nothing" distinguishable
    from "never ran", and this is the reader of the distinction: the
    stale-pass check reads only the records that exist, so an item whose
    session skipped harvest outright was digested and cited on the strength
    of the shared link alone, on no surface at all.

    Who owes a pass is bounded the way the ingest procedure bounds the
    step. An item still owing a unit derives ``raw`` and has not reached
    harvest, so it is never listed, however long it stays parked — the same
    rule :func:`digest_orphans` applies. An item with no fetched page at
    all — a no-source capture, or one whose every unit closed without
    content (dead, or ruled out skipped) — has no pages to read the
    subject rule over; its work is description and digest, and it owes no
    pass. A fetched page is a ``done`` unit that is
    not a media download or an extracted asset, the same reading the URL
    cap counts (:meth:`_Drain.fetched_count`).

    A recorded pass covers an item by the id's trailing shortid, the
    trailing-id match exclusions already use across renames: a rename keeps
    the shortid and rewrites the slug, so a full-id match would report
    every renamed item's harvest as never run while the record stands in
    the file under the old name.

    An item on the digest backstop is listed here too when both hold: the
    two findings are different facts with different repairs — the
    backstop's repair is digest → place → wiki, which never runs harvest —
    so each check answers for itself, as the referential-integrity rows do.

    Args:
        instance: The instance.

    Returns:
        Item ids owing a harvest pass that no record covers, sorted.
    """
    entries = _ledger_or_none(instance)
    if not entries:
        return []
    owners = _unit_owners(instance, entries)
    owing = items_owing_work(entries, owners)
    live = {path.stem for path in instance.corpus_dir.glob("*/*.md")}
    fetched = {
        item_id
        for entry in entries.values()
        if entry.status is Status.DONE and entry.job is None
        for item_id in owners.get(entry.hash, (entry.item,))
    }
    recorded = _harvested_shortids(instance)
    return sorted(
        item_id
        for item_id in fetched & live
        if item_id not in owing and item_id.rsplit("-", 1)[-1] not in recorded
    )


def _harvested_shortids(instance: Instance) -> set[str]:
    """The trailing shortid of every item a harvest pass was recorded for.

    Any rules version counts: a pass under old rules RAN, which is the
    stale-pass finding, never this one's.
    """
    path = instance.passes_path
    if not path.exists():
        return set()
    recorded: set[str] = set()
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # lint parses this file loudly; this reader skips the line
        if not isinstance(record, dict) or record.get("stage") != "harvest":
            continue
        item = record.get("item")
        if isinstance(item, str):
            recorded.add(item.rsplit("-", 1)[-1])
    return recorded


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
    The attribution is the corpus's answer (:meth:`_Drain.owner_of`), like
    every other write's — a renamed item's heal lands under the live id,
    never the dead string the prior line spells. The owning item's derived
    frontmatter is refreshed in the same call, so a hand-written enrichment
    file is listed the moment its heal lands.

    A unit is found by its canonical identity, or — failing that — by the
    exact key it was stored under: bad seeds and every media line
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
    # The attribution is asked of the corpus before the write, like every
    # other write path: a heal carried on the stored string re-records a
    # renamed item's unit under the dead id, and the superseded-output drop
    # below then searches a directory the rename moved away.
    drain.resolve_owners()
    effective_path = path if path is not None else (prior.path if status is Status.DONE else None)
    healed = LedgerEntry(
        hash=prior.hash,
        url=prior.url,
        item=drain.owner_of(prior),
        kind=prior.kind,
        format=prior.format,
        status=status,
        # A stray --needs on a status that cannot carry it passes through
        # and fails schema validation loudly rather than being silently
        # dropped.
        needs=(needs or prior.needs) if status in (Status.WAITING, Status.BLOCKED) else needs,
        attempts=max(prior.attempts or 0, 1) if status is Status.BLOCKED else None,
        http_shared=prior.http_shared,
        engine="seed",  # stamped in record
        date=datetime.date.min,
        job=prior.job,
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
    drain.close_ledger()
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
