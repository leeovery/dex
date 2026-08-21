"""Pipeline vocabulary: enums, work-unit dataclasses, instance paths, config.

This module is the bottom of the dependency graph: it imports nothing from
the rest of the package.
"""

import datetime
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

__all__ = [
    "DRIVER_STATUSES",
    "Asset",
    "Availability",
    "Child",
    "Config",
    "Extraction",
    "Extractor",
    "Format",
    "Instance",
    "Kind",
    "LedgerEntry",
    "MediaFetch",
    "MigrationReport",
    "Need",
    "Redetection",
    "Result",
    "Skipped",
    "SourceDriver",
    "Status",
    "Transcriber",
    "WorkUnit",
    "parse_version",
    "version_newer",
]


# ---------------------------------------------------------------------------
# Enums. StrEnum so ledgers and frontmatter serialize as plain strings.
# No aliases: renames are a clean break executed by migration 1.
# ---------------------------------------------------------------------------


class Kind(StrEnum):
    """Source shape — one driver each.

    ``IMAGE`` and ``TEXT`` are corpus-frontmatter vocabulary only and never
    become work units.
    """

    YOUTUBE = "youtube"
    X = "x"
    GITHUB = "github"
    PAPER = "paper"
    PODCAST = "podcast"
    WEB = "web"
    FILE = "file"
    IMAGE = "image"
    TEXT = "text"


class Format(StrEnum):
    """File format — extractor routing only (not images, not audio)."""

    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    ODT = "odt"
    ODS = "ods"
    ODP = "odp"
    RTF = "rtf"
    EPUB = "epub"
    CSV = "csv"


class Status(StrEnum):
    """Ledger status lifecycle."""

    QUEUED = "queued"
    DONE = "done"
    DEAD = "dead"
    SKIPPED = "skipped"
    MANUAL = "manual"
    WAITING = "waiting"
    BLOCKED = "blocked"
    ERROR = "error"


class Need(StrEnum):
    """Capability a parked work unit needs — mechanical and resource-keyed only."""

    TRANSCRIBE = "transcribe"
    EXTRACT = "extract"
    OCR = "ocr"


class MediaFetch(StrEnum):
    """Instance policy for the media stage's URL downloads."""

    NONE = "none"
    LEAD = "lead"


# `queued` is a birth state, not a driver outcome, and `error` is RAISED,
# never returned — a Result has no error channel, so a returned `error`
# could only carry a fabricated message; the run loop's single broad except
# is the one place errors are made. ONE exception: a Result carrying a
# `redetect` returns `queued`, because a mid-fetch kind correction IS a
# re-birth — the unit re-enters the queue under its corrected identity.
DRIVER_STATUSES: frozenset[Status] = frozenset(Status) - {Status.QUEUED, Status.ERROR}


# ---------------------------------------------------------------------------
# Provenance vocabulary. `via` stays a documented string, not an enum:
# `migration-<n>` is parameterized and provenance is descriptive, never
# dispatched on. It is still validated against the known shapes.
# ---------------------------------------------------------------------------

_VIA_EXACT = frozenset({"harvest", "thread", "media", "sniff", "extract-asset"})
_VIA_MIGRATION = re.compile(r"^migration-[1-9][0-9]*$")


def _validate_via(via: str) -> None:
    if via in _VIA_EXACT or _VIA_MIGRATION.match(via):
        return
    known = ", ".join(sorted(_VIA_EXACT))
    raise ValueError(f"unknown via {via!r}: expected one of {known}, or migration-<n>")


# ---------------------------------------------------------------------------
# Shared work-unit identity invariants, enforced on WorkUnit and
# LedgerEntry alike.
# ---------------------------------------------------------------------------

# The stated-reason contract, shared by Result and LedgerEntry:
# manual and skipped exist only by deliberate decision, so the decision must
# be recorded; waiting/blocked/dead may carry one; done/queued need none, and
# error carries the scrubbed `error` field instead (ledger) or no reason at
# all (results) — reason and error never coexist.
_REASON_REQUIRED = frozenset({Status.MANUAL, Status.SKIPPED})
_REASON_FORBIDDEN = frozenset({Status.DONE, Status.QUEUED, Status.ERROR})

# Where `needs` may ride (LedgerEntry only — a driver Result stays
# waiting-only): a waiting park names the missing capability; a blocked
# acquisition retry keeps `needs` so the run loop routes it back through
# the capability drain, never the driver.
_NEEDS_STATUSES = frozenset({Status.WAITING, Status.BLOCKED})

# sha1(work key)[:10] — the ledger key format.
_HASH_RE = re.compile(r"^[0-9a-f]{10}$")
# Corpus-frontmatter vocabulary only; never work units.
_NON_WORK_KINDS = frozenset({Kind.IMAGE, Kind.TEXT})


def _validate_unit_identity(
    *, unit_hash: str, url: str, item: str, kind: Kind, unit_format: Format | None
) -> None:
    if not _HASH_RE.match(unit_hash):
        raise ValueError(
            f"hash must be 10 lowercase hex characters (sha1(work key)[:10]), got {unit_hash!r}"
        )
    if not url:
        raise ValueError("url must not be empty")
    if not item:
        raise ValueError("item must not be empty")
    if kind in _NON_WORK_KINDS:
        raise ValueError(
            f"kind {kind!r} is corpus-frontmatter vocabulary only and never becomes "
            "a work unit"
        )
    if unit_format is not None and kind is not Kind.FILE:
        raise ValueError(f"format is file-work only, got kind {kind!r}")


def _validate_depth(depth: int) -> None:
    if isinstance(depth, bool):
        raise ValueError("depth must be an integer, not a boolean")
    if depth < 0:
        raise ValueError(f"depth must be >= 0, got {depth}")


# ---------------------------------------------------------------------------
# Work-unit dataclasses. All frozen, slots, kw_only.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Availability:
    """Whether a capability provider can run — never ``tuple[bool, str]``."""

    ok: bool
    reason: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkUnit:
    """What the pipeline hands a driver.

    Deliberately not :class:`LedgerEntry`: drivers never see ``attempts``,
    ``engine``, or ``rerun`` bookkeeping.
    """

    hash: str
    url: str
    kind: Kind
    format: Format | None = None
    item: str
    depth: int
    parent: str | None = None

    def __post_init__(self) -> None:
        _validate_unit_identity(
            unit_hash=self.hash,
            url=self.url,
            item=self.item,
            kind=self.kind,
            unit_format=self.format,
        )
        _validate_depth(self.depth)
        if (self.parent is None) != (self.depth == 0):
            raise ValueError(
                "parent and depth travel together: depth 0 is the shared URL (no "
                "parent); spawned units carry both"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class Child:
    """A URL discovered mid-flight that re-enters the queue.

    The pipeline assigns ``depth = parent depth + 1`` and ``parent`` — the
    spawning driver only names the URL and its provenance.
    """

    url: str
    via: str

    def __post_init__(self) -> None:
        _validate_via(self.via)


@dataclass(frozen=True, slots=True, kw_only=True)
class Redetection:
    """A mid-fetch kind correction: the content is not what detection said.

    The canonical case: detection said web (a HEAD lied or was
    inconclusive), the GET returned a PDF. The unit re-enters the queue
    under the corrected identity — same URL, same hash — with
    ``via: "sniff"`` provenance; the run layer enforces once-only.
    """

    kind: Kind
    format: Format | None = None

    def __post_init__(self) -> None:
        if self.kind in _NON_WORK_KINDS:
            raise ValueError(
                f"cannot re-detect to {self.kind!r} — corpus-frontmatter vocabulary "
                "never becomes a work unit"
            )
        if self.format is not None and self.kind is not Kind.FILE:
            raise ValueError(f"format is file-work only, got re-detected kind {self.kind!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Result:
    """What a driver's ``fetch`` returns.

    ``reason`` mirrors the ledger's stated-reason contract: required
    when a driver returns ``manual``/``skipped`` (the driver knows why),
    optional on ``waiting``/``blocked``/``dead``, forbidden otherwise. The
    run layer may append classifier context to it but never invents what the
    driver knew.

    ``redetect`` is the mid-fetch kind-correction signal: the fetched bytes
    are a different kind of content than detection assigned. It travels
    with ``status: queued`` and nothing else — a redetection carries the
    corrected identity only; outputs belong to the driver that owns the
    corrected kind.
    """

    status: Status
    meta: dict[str, str | int | None]
    body: str | None = None
    media: list[str] = field(default_factory=list)
    children: list[Child] = field(default_factory=list)
    # Extraction assets: embedded images have no URL, so bytes are the
    # only possible form — and drivers never touch the disk, so the
    # Result is the one channel through which they can reach the run layer's
    # asset-writing step.
    assets: list["Asset"] = field(default_factory=list)
    needs: Need | None = None
    reason: str | None = None
    redetect: Redetection | None = None

    def __post_init__(self) -> None:
        if self.redetect is not None:
            if self.status is not Status.QUEUED:
                raise ValueError(
                    f"a redetection travels with status 'queued' (a re-birth under the "
                    f"corrected kind), got {self.status!r}"
                )
            if (
                self.meta
                or self.body
                or self.media
                or self.children
                or self.assets
                or self.needs
                or self.reason
            ):
                raise ValueError(
                    "a redetection carries the corrected identity only — outputs belong "
                    "to the driver that owns the corrected kind"
                )
            return
        if self.status not in DRIVER_STATUSES:
            raise ValueError(
                f"drivers may not return status {self.status!r} — 'queued' is a birth "
                "state (returnable only as a redetection) and 'error' is raised, never "
                "returned"
            )
        if self.status is Status.WAITING and self.needs is None:
            raise ValueError("status 'waiting' requires needs")
        if self.needs is not None and self.status is not Status.WAITING:
            raise ValueError(
                f"needs={self.needs!r} only accompanies status 'waiting', got {self.status!r}"
            )
        if self.status in _REASON_REQUIRED and not self.reason:
            raise ValueError(
                f"a driver returning status {self.status!r} must state its reason"
            )
        if self.status in _REASON_FORBIDDEN and self.reason is not None:
            raise ValueError(f"reason is forbidden on a {self.status!r} result")
        if self.assets and self.status is not Status.DONE:
            raise ValueError(f"extraction assets are done-only outputs, got {self.status!r}")


@dataclass(frozen=True, slots=True, kw_only=True)
class Asset:
    """An embedded asset extracted from inside a container document.

    Embedded images have no URL — bytes are the only possible form.
    """

    data: bytes
    suggested_ext: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Extraction:
    """What an extractor returns for a binary document."""

    markdown: str
    assets: list[Asset] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Capability providers: structural, resolved per capability by the
# registries in dex_engine.capabilities. Conformance is verified at the typed
# provider lists there, exactly like the driver registry literal.
# ---------------------------------------------------------------------------


class Transcriber(Protocol):
    """A transcription capability provider.

    ``model`` is the model identifier the provider runs requires raw
    transcripts stamped ``via``/``model`` in enrichment frontmatter, so the
    drain must be able to read it off the provider.
    """

    name: str
    model: str

    def available(self) -> Availability:
        """Honest availability: install intact, credentials present."""
        ...

    def transcribe(self, audio: Path, initial_prompt: str) -> str:
        """Transcribe one audio file, primed with the item's known vocabulary.

        Raises ``ProviderInputError`` for audio it cannot decode (→ manual)
        and ``ProviderUnavailableError`` for capability-level failures
        discovered at call time (→ the job stays waiting).
        """
        ...


class Extractor(Protocol):
    """A document-extraction capability provider.

    The Format is the contract, not the tool: providers register per format
    via ``supports``, so each format falls back (or parks) independently.
    """

    name: str

    def supports(self, fmt: Format) -> bool:
        """True when this provider can extract ``fmt``."""
        ...

    def available(self) -> Availability:
        """Honest availability: install intact."""
        ...

    def extract(self, data: bytes, fmt: Format) -> Extraction:
        """Extract markdown (and embedded assets, as bytes) from a document.

        Raises ``ProviderInputError`` for documents it cannot parse
        (→ manual) and ``ScannedDocumentError`` for image-only documents
        (→ the OCR path: ``waiting`` + ``needs: ocr``).
        """
        ...


# ---------------------------------------------------------------------------
# Driver interface: structural, one per source shape. Conformance is
# verified at the typed registry literal (pipeline/registry.py) — the one
# assignment where the checker matches every driver against this Protocol.
# ---------------------------------------------------------------------------


class SourceDriver(Protocol):
    """One source driver: pattern match, canonicalization, fetch."""

    kind: Kind
    sleep: float  # per-driver politeness delay, seconds

    def matches(self, url: str) -> bool:
        """True when this driver owns ``url`` (checked in registry order)."""
        ...

    def canonical(self, url: str) -> str:
        """The canonical form of ``url`` — it keys the ledger hash."""
        ...

    def fetch(self, unit: WorkUnit) -> Result:
        """Fetch one unit of work; never touches the ledger or the disk."""
        ...


# ---------------------------------------------------------------------------
# Ledger entry: frozen, validated — the schema comments are runtime
# invariants, enforced here so a malformed entry cannot exist in memory.
# Serialization happens in exactly one place: pipeline/ledger.py.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class LedgerEntry:
    """One work unit in the enrichment ledger."""

    hash: str
    url: str
    item: str
    kind: Kind
    format: Format | None = None
    status: Status
    needs: Need | None = None
    attempts: int | None = None
    # a cap-fire marker: this skipped line records refused work, not an
    # admitted unit
    capped: bool = False
    engine: str
    date: datetime.date
    # provenance — children and reruns only
    via: str | None = None
    parent: str | None = None
    depth: int | None = None
    rerun: bool = False
    # outputs — success only
    path: str | None = None
    title: str | None = None
    error: str | None = None
    # the stated parking reason — see _REASON_REQUIRED/_REASON_FORBIDDEN
    reason: str | None = None

    def __post_init__(self) -> None:
        _validate_unit_identity(
            unit_hash=self.hash,
            url=self.url,
            item=self.item,
            kind=self.kind,
            unit_format=self.format,
        )
        if not self.engine:
            raise ValueError("every ledger entry records the engine version that wrote it")
        _validate_entry_queue_fields(self)
        _validate_entry_outcome_fields(self)
        _validate_entry_provenance(self)


def _validate_entry_queue_fields(entry: LedgerEntry) -> None:
    if entry.status is Status.WAITING and entry.needs is None:
        raise ValueError("status 'waiting' requires needs")
    if entry.needs is not None and entry.status not in _NEEDS_STATUSES:
        raise ValueError(
            f"needs={entry.needs!r} rides waiting parks and blocked retries only, "
            f"got status {entry.status!r}"
        )
    if entry.capped and entry.status is not Status.SKIPPED:
        raise ValueError(
            f"capped marks a cap-refused skip — skipped-only, got status {entry.status!r}"
        )
    if isinstance(entry.attempts, bool):
        raise ValueError("attempts must be an integer, not a boolean")
    if entry.status is Status.BLOCKED and (entry.attempts is None or entry.attempts < 1):
        raise ValueError("status 'blocked' requires attempts >= 1")
    if entry.attempts is not None and entry.status is not Status.BLOCKED:
        raise ValueError(f"attempts is blocked-only bookkeeping, got status {entry.status!r}")


def _validate_entry_outcome_fields(entry: LedgerEntry) -> None:
    if entry.status is Status.ERROR and not entry.error:
        raise ValueError("status 'error' requires a scrubbed error message")
    if entry.error is not None and entry.status is not Status.ERROR:
        raise ValueError(f"error message is error-only, got status {entry.status!r}")
    if entry.status in _REASON_REQUIRED and not entry.reason:
        raise ValueError(f"status {entry.status!r} requires a stated reason")
    if entry.reason is not None and "\n" in entry.reason:
        # Reasons render on single-line report surfaces; every writer
        # (scrub, the mark verb) normalizes before construction.
        raise ValueError(f"reason must be a single line: {entry.reason!r}")
    if entry.status in _REASON_FORBIDDEN and entry.reason is not None:
        raise ValueError(
            f"reason is forbidden on status {entry.status!r} — error entries carry "
            "the scrubbed 'error' field instead; done/queued park nothing"
        )
    if (entry.path is not None or entry.title is not None) and entry.status is not Status.DONE:
        raise ValueError(f"outputs (path/title) are success-only, got status {entry.status!r}")
    if entry.title is not None and entry.path is None:
        raise ValueError("title without path: outputs travel together")


def _validate_entry_provenance(entry: LedgerEntry) -> None:
    if entry.via is not None:
        _validate_via(entry.via)
    if entry.depth is not None:
        _validate_depth(entry.depth)
    if (entry.parent is None) != (entry.depth is None):
        raise ValueError("parent and depth are paired provenance — both or neither")


# ---------------------------------------------------------------------------
# Migration report — surfaces render it, so it gets the same
# frozen-dataclass treatment as LedgerEntry.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Skipped:
    """One thing a migration declined to touch, and why."""

    what: str
    why: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationReport:
    """What a migration did — reviewed by Claude in-session."""

    actions: list[str] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Instance and config: no import-time globals — both are built in the
# CLI entry point and constructor-injected everywhere.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Instance:
    """An instance repo root and the paths derived from it."""

    root: Path

    @property
    def corpus_dir(self) -> Path:
        """``corpus/`` — one markdown item per share."""
        return self.root / "corpus"

    @property
    def state_dir(self) -> Path:
        """``state/`` — JSONL/JSON machinery state."""
        return self.root / "state"

    @property
    def enrichment_dir(self) -> Path:
        """``enrichment/`` — fetched content per item."""
        return self.root / "enrichment"

    @property
    def cache_dir(self) -> Path:
        """``cache/`` — gitignored ephemera: render payloads, in-flight audio."""
        return self.root / "cache"

    @property
    def ledger_path(self) -> Path:
        """``state/enrichment-ledger.jsonl`` — the work queue."""
        return self.state_dir / "enrichment-ledger.jsonl"

    @property
    def passes_path(self) -> Path:
        """``state/passes.jsonl`` — per-item stage records."""
        return self.state_dir / "passes.jsonl"

    @property
    def digests_dir(self) -> Path:
        """``state/digests/`` — per-item fact indexes (the digest step's output)."""
        return self.state_dir / "digests"

    @property
    def config_path(self) -> Path:
        """``state/config.json`` — instance config."""
        return self.state_dir / "config.json"


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Instance configuration from ``state/config.json``.

    Parsed loudly: defaults apply only when the file is genuinely missing;
    a present-but-malformed file raises instead of being silently ignored
    (the old config-read-at-import behind a bare ``except`` is the named
    anti-pattern).
    """

    media_fetch: MediaFetch = MediaFetch.LEAD
    transcribe_model: str = "medium"
    # whisper-api credentials: base_url + key from config/env — config
    # wins, the OPENAI_* environment variables are the fallback (read by the
    # provider at availability time, never at import).
    transcribe_base_url: str | None = None
    transcribe_api_key: str | None = None
    # The API-side model name — its own key because local size names
    # (medium, small) never map onto provider model ids (whisper-1,
    # whisper-large-v3). None → the provider's default; per-call --model
    # still overrides.
    transcribe_api_model: str | None = None
    report_issues: bool = True
    providers: dict[str, list[str]] = field(default_factory=dict)
    internal_domains: list[str] = field(default_factory=list)
    noise_prefixes: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Config":
        """Read config from ``path``; defaults only for a missing file.

        Args:
            path: The instance's ``state/config.json``.

        Returns:
            The parsed configuration.

        Raises:
            ValueError: The file exists but is not valid JSON, has unknown
                keys, or holds a value of the wrong shape.
        """
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}: invalid JSON: {e}") from e
        return cls.from_raw(raw, source=str(path))

    @classmethod
    def from_raw(cls, raw: object, *, source: str) -> "Config":
        """Validate already-parsed config content; ``source`` names it in errors.

        The migration path uses this to validate content it is about to
        write, without a file round-trip.

        Args:
            raw: The parsed JSON value (must be an object).
            source: Where the content came from, for error messages.

        Returns:
            The parsed configuration.

        Raises:
            ValueError: Unknown keys, or a value of the wrong shape.
        """
        if not isinstance(raw, dict):
            raise ValueError(f"{source}: expected a JSON object, got {type(raw).__name__}")
        known = {
            "media_fetch",
            "transcribe_model",
            "transcribe_base_url",
            "transcribe_api_key",
            "transcribe_api_model",
            "report_issues",
            "providers",
            "internal_domains",
            "noise_prefixes",
        }
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(
                f"{source}: unknown config keys {unknown} — "
                f"known keys are {sorted(known)} (a typo'd key would otherwise be silently dead)"
            )
        return cls(
            media_fetch=_config_enum(source, raw, "media_fetch", default=MediaFetch.LEAD),
            transcribe_model=_config_str(source, raw, "transcribe_model", default="medium"),
            transcribe_base_url=_config_opt_str(source, raw, "transcribe_base_url"),
            transcribe_api_key=_config_opt_str(source, raw, "transcribe_api_key"),
            transcribe_api_model=_config_opt_str(source, raw, "transcribe_api_model"),
            report_issues=_config_bool(source, raw, "report_issues", default=True),
            providers=_config_providers(source, raw),
            internal_domains=_config_str_list(source, raw, "internal_domains"),
            noise_prefixes=_config_str_list(source, raw, "noise_prefixes"),
        )


def _config_enum(
    source: str, raw: dict[str, object], key: str, *, default: MediaFetch
) -> MediaFetch:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, str):
        raise ValueError(f"{source}: {key} must be a string, got {type(value).__name__}")
    try:
        return MediaFetch(value)
    except ValueError as e:
        options = ", ".join(m.value for m in MediaFetch)
        raise ValueError(f"{source}: {key} must be one of {options}, got {value!r}") from e


def _config_str(source: str, raw: dict[str, object], key: str, *, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{source}: {key} must be a string, got {type(value).__name__}")
    return value


def _config_opt_str(source: str, raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{source}: {key} must be a string, got {type(value).__name__}")
    return value or None  # an empty string is "not configured", not a credential


def _config_bool(source: str, raw: dict[str, object], key: str, *, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{source}: {key} must be a boolean, got {type(value).__name__}")
    return value


def _config_str_list(source: str, raw: dict[str, object], key: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{source}: {key} must be a list of strings")
    return value



def _config_providers(source: str, raw: dict[str, object]) -> dict[str, list[str]]:
    value = raw.get("providers", {})
    if not isinstance(value, dict) or not all(
        isinstance(cap, str) and isinstance(names, list) and all(isinstance(n, str) for n in names)
        for cap, names in value.items()
    ):
        raise ValueError(
            f"{source}: providers must be an object of capability -> list of provider names"
        )
    capabilities = {need.value for need in Need}
    unknown = sorted(set(value) - capabilities)
    if unknown:
        raise ValueError(
            f"{source}: providers key(s) {unknown} are not capabilities — "
            f"known capabilities: {sorted(capabilities)} (a typo'd key would silently "
            "never resolve)"
        )
    return value


# ---------------------------------------------------------------------------
# Engine versions: comparisons parse to tuples, never compare strings —
# "0.10.0" > "0.9.1" is False as strings, and that bug would file issues
# about itself.
# ---------------------------------------------------------------------------


def parse_version(version: str) -> tuple[int, ...]:
    """Parse an engine version or tag (``0.2.1`` / ``v0.2.1``) to a tuple.

    Args:
        version: The version string; a leading ``v`` is accepted.

    Returns:
        The numeric components, comparable with tuple ordering.

    Raises:
        ValueError: The string is not dot-separated integers.
    """
    text = version.strip().removeprefix("v")
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError as e:
        raise ValueError(f"unparseable engine version {version!r}") from e


def version_newer(candidate: str, baseline: str) -> bool:
    """True when ``candidate`` is a strictly newer engine version than ``baseline``."""
    return parse_version(candidate) > parse_version(baseline)
