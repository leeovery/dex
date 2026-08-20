"""Pipeline vocabulary: enums, work-unit dataclasses, instance paths, config.

Design reference: design/ingestion-pipeline.md — §2 (interfaces and typing
discipline), §3 (enums), §5 (ledger schema and status lifecycle), §14
(implementation standards).

This module is the bottom of the dependency graph: it imports nothing from
the rest of the package.
"""

import datetime
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

__all__ = [
    "DRIVER_STATUSES",
    "Asset",
    "Availability",
    "Child",
    "Config",
    "Extraction",
    "Format",
    "Instance",
    "Kind",
    "LedgerEntry",
    "MediaFetch",
    "MigrationReport",
    "Need",
    "Result",
    "Skipped",
    "Status",
    "WorkUnit",
    "parse_version",
    "version_newer",
]


# ---------------------------------------------------------------------------
# Enums (§3). StrEnum so ledgers and frontmatter serialize as plain strings.
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
    """Ledger status lifecycle (§5)."""

    QUEUED = "queued"
    DONE = "done"
    DEAD = "dead"
    SKIPPED = "skipped"
    MANUAL = "manual"
    WAITING = "waiting"
    BLOCKED = "blocked"
    ERROR = "error"


class Need(StrEnum):
    """Capability a waiting work unit needs — mechanical and resource-keyed only."""

    TRANSCRIBE = "transcribe"
    EXTRACT = "extract"
    OCR = "ocr"


class MediaFetch(StrEnum):
    """Instance policy for the media stage's URL downloads (§7)."""

    NONE = "none"
    LEAD = "lead"


# `queued` is a birth state, not a driver outcome (§2): drivers may return
# every status except it.
DRIVER_STATUSES: frozenset[Status] = frozenset(Status) - {Status.QUEUED}


# ---------------------------------------------------------------------------
# Provenance vocabulary. `via` stays a documented string, not an enum (§3):
# `migration-<n>` is parameterized and provenance is descriptive, never
# dispatched on. It is still validated against the known shapes (§5).
# ---------------------------------------------------------------------------

_VIA_EXACT = frozenset({"harvest", "thread", "media", "sniff", "extract-asset"})
_VIA_MIGRATION = re.compile(r"^migration-[1-9][0-9]*$")


def _validate_via(via: str) -> None:
    if via in _VIA_EXACT or _VIA_MIGRATION.match(via):
        return
    known = ", ".join(sorted(_VIA_EXACT))
    raise ValueError(f"unknown via {via!r}: expected one of {known}, or migration-<n>")


# ---------------------------------------------------------------------------
# Work-unit dataclasses (§2). All frozen, slots, kw_only.
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
    ``engine``, or ``rerun`` bookkeeping (§2).
    """

    hash: str
    url: str
    kind: Kind
    format: Format | None = None
    item: str
    depth: int
    parent: str | None = None


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
class Result:
    """What a driver's ``fetch`` returns."""

    status: Status
    meta: dict[str, str | int | None]
    body: str | None = None
    media: list[str] = field(default_factory=list)
    children: list[Child] = field(default_factory=list)
    needs: Need | None = None

    def __post_init__(self) -> None:
        if self.status not in DRIVER_STATUSES:
            raise ValueError(
                f"drivers may not return status {self.status!r} — "
                "'queued' is a birth state, not a driver outcome (§2)"
            )
        if self.status is Status.WAITING and self.needs is None:
            raise ValueError("status 'waiting' requires needs (§5)")
        if self.needs is not None and self.status is not Status.WAITING:
            raise ValueError(
                f"needs={self.needs!r} only accompanies status 'waiting', got {self.status!r}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class Asset:
    """An embedded asset extracted from inside a container document (§6).

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
# Ledger entry (§5): frozen, validated — the schema comments are runtime
# invariants, enforced here so a malformed entry cannot exist in memory.
# Serialization happens in exactly one place: pipeline/ledger.py.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class LedgerEntry:
    """One work unit in the enrichment ledger (§5)."""

    hash: str
    url: str
    item: str
    kind: Kind
    format: Format | None = None
    status: Status
    needs: Need | None = None
    attempts: int | None = None
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

    def __post_init__(self) -> None:
        if not self.engine:
            raise ValueError("every ledger entry records the engine version that wrote it (§5)")
        if self.status is Status.WAITING and self.needs is None:
            raise ValueError("status 'waiting' requires needs (§5)")
        if self.needs is not None and self.status is not Status.WAITING:
            raise ValueError(
                f"needs={self.needs!r} is waiting-only, got status {self.status!r} (§5)"
            )
        if self.status is Status.BLOCKED and (self.attempts is None or self.attempts < 1):
            raise ValueError("status 'blocked' requires attempts >= 1 (§5)")
        if self.attempts is not None and self.status is not Status.BLOCKED:
            raise ValueError(
                f"attempts is blocked-only bookkeeping, got status {self.status!r} (§5)"
            )
        if self.status is Status.ERROR and not self.error:
            raise ValueError("status 'error' requires a scrubbed error message (§5)")
        if self.error is not None and self.status is not Status.ERROR:
            raise ValueError(f"error message is error-only, got status {self.status!r} (§5)")
        if (self.path is not None or self.title is not None) and self.status is not Status.DONE:
            raise ValueError(
                f"outputs (path/title) are success-only, got status {self.status!r} (§5)"
            )
        if self.via is not None:
            _validate_via(self.via)


# ---------------------------------------------------------------------------
# Migration report (§12) — surfaces render it, so it gets the same
# frozen-dataclass treatment as LedgerEntry.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Skipped:
    """One thing a migration declined to touch, and why."""

    what: str
    why: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MigrationReport:
    """What a migration did — reviewed by Claude in-session (§12)."""

    actions: list[str] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Instance and config (§14): no import-time globals — both are built in the
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
        """``state/`` — JSONL/JSON machinery state (§4)."""
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
        """``state/enrichment-ledger.jsonl`` — the work queue (§5)."""
        return self.state_dir / "enrichment-ledger.jsonl"

    @property
    def config_path(self) -> Path:
        """``state/config.json`` — instance config (§4)."""
        return self.state_dir / "config.json"


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Instance configuration from ``state/config.json`` (§4).

    Parsed loudly: defaults apply only when the file is genuinely missing;
    a present-but-malformed file raises instead of being silently ignored
    (the old config-read-at-import behind a bare ``except`` is the named
    anti-pattern, §14).
    """

    media_fetch: MediaFetch = MediaFetch.LEAD
    transcribe_model: str = "medium"
    report_issues: bool = True
    providers: dict[str, list[str]] = field(default_factory=dict)
    name_map: dict[str, str] = field(default_factory=dict)
    internal_domains: list[str] = field(default_factory=list)

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
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected a JSON object, got {type(raw).__name__}")
        known = {
            "media_fetch",
            "transcribe_model",
            "report_issues",
            "providers",
            "name_map",
            "internal_domains",
        }
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(
                f"{path}: unknown config keys {unknown} — "
                f"known keys are {sorted(known)} (a typo'd key would otherwise be silently dead)"
            )
        return cls(
            media_fetch=_config_enum(path, raw, "media_fetch", default=MediaFetch.LEAD),
            transcribe_model=_config_str(path, raw, "transcribe_model", default="medium"),
            report_issues=_config_bool(path, raw, "report_issues", default=True),
            providers=_config_providers(path, raw),
            name_map=_config_str_map(path, raw, "name_map"),
            internal_domains=_config_str_list(path, raw, "internal_domains"),
        )


def _config_enum(
    path: Path, raw: dict[str, object], key: str, *, default: MediaFetch
) -> MediaFetch:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, str):
        raise ValueError(f"{path}: {key} must be a string, got {type(value).__name__}")
    try:
        return MediaFetch(value)
    except ValueError as e:
        options = ", ".join(m.value for m in MediaFetch)
        raise ValueError(f"{path}: {key} must be one of {options}, got {value!r}") from e


def _config_str(path: Path, raw: dict[str, object], key: str, *, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{path}: {key} must be a string, got {type(value).__name__}")
    return value


def _config_bool(path: Path, raw: dict[str, object], key: str, *, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{path}: {key} must be a boolean, got {type(value).__name__}")
    return value


def _config_str_list(path: Path, raw: dict[str, object], key: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{path}: {key} must be a list of strings")
    return value


def _config_str_map(path: Path, raw: dict[str, object], key: str) -> dict[str, str]:
    value = raw.get(key, {})
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError(f"{path}: {key} must be an object of string -> string")
    return value


def _config_providers(path: Path, raw: dict[str, object]) -> dict[str, list[str]]:
    value = raw.get("providers", {})
    if not isinstance(value, dict) or not all(
        isinstance(cap, str) and isinstance(names, list) and all(isinstance(n, str) for n in names)
        for cap, names in value.items()
    ):
        raise ValueError(
            f"{path}: providers must be an object of capability -> list of provider names"
        )
    return value


# ---------------------------------------------------------------------------
# Engine versions (§5): comparisons parse to tuples, never compare strings —
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
