"""The ingestion pipeline's public surface (§14): types and enums.

Dependency direction, structurally enforced: ``types`` imports nothing from
this package; ``ledger``/``classify``/``urls`` import only ``types``;
``detect`` takes the driver list as an argument; only ``registry``/``run``
import drivers — which prevents the normalize/detect/drivers
circular-import trap.

Ledger persistence and the other submodules are used as modules:
``from dex_engine.pipeline import ledger``.
"""

from .ledger import LedgerSchemaError
from .types import (
    DRIVER_STATUSES,
    Asset,
    Availability,
    Child,
    Config,
    Extraction,
    Format,
    Instance,
    Kind,
    LedgerEntry,
    MediaFetch,
    MigrationReport,
    Need,
    Result,
    Skipped,
    SourceDriver,
    Status,
    WorkUnit,
    parse_version,
    version_newer,
)

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
    "LedgerSchemaError",
    "MediaFetch",
    "MigrationReport",
    "Need",
    "Result",
    "Skipped",
    "SourceDriver",
    "Status",
    "WorkUnit",
    "parse_version",
    "version_newer",
]
