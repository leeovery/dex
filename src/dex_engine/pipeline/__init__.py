"""The ingestion pipeline's public surface: types and enums.

Dependency direction, structurally enforced: ``types`` imports nothing from
this package; ``ledger``/``classify``/``urls`` import only ``types``;
``detect`` takes the driver list as an argument; only ``registry``/``run``/
``transcribe`` import drivers (transcribe reaches the transport seam and
the shared yt-dlp classifier, both leaves) — which prevents the
normalize/detect/drivers circular-import trap.

Ledger persistence and the other submodules are used as modules:
``from dex_engine.pipeline import ledger``.
"""

from .ledger import LedgerSchemaError
from .types import (
    Asset,
    Availability,
    Config,
    Content,
    Extraction,
    Extractor,
    Format,
    Instance,
    Kind,
    LedgerEntry,
    MediaFetch,
    MigrationReport,
    Missing,
    Need,
    NeedsCapability,
    Outcome,
    Redetected,
    Refused,
    Skipped,
    SourceDriver,
    Status,
    Transcriber,
    Unusable,
    WorkUnit,
    parse_version,
    version_newer,
)

__all__ = [
    "Asset",
    "Availability",
    "Config",
    "Content",
    "Extraction",
    "Extractor",
    "Format",
    "Instance",
    "Kind",
    "LedgerEntry",
    "LedgerSchemaError",
    "MediaFetch",
    "MigrationReport",
    "Missing",
    "Need",
    "NeedsCapability",
    "Outcome",
    "Redetected",
    "Refused",
    "Skipped",
    "SourceDriver",
    "Status",
    "Transcriber",
    "Unusable",
    "WorkUnit",
    "parse_version",
    "version_newer",
]
