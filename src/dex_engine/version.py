"""The running engine's version, read from package metadata (§12/§14).

This is the value stamped into ledger lines and migration records, and the
baseline the sync flow compares release tags against. It is injected where
consumed (§14) — call :func:`engine_version` in a CLI entry point and pass
the string down; never read metadata mid-pipeline.

Mint cuts releases, tags, and changelogs — human-invoked, never the engine
(§12). Nothing in this package creates a release; this module only reads
the version the running install was built with.
"""

from importlib import metadata

__all__ = ["engine_version"]


def engine_version() -> str:
    """The installed engine version.

    Returns:
        The version string from package metadata (e.g. ``"0.2.1"``).

    Raises:
        RuntimeError: The package is not installed (not a dex environment).
    """
    try:
        return metadata.version("dex-engine")
    except metadata.PackageNotFoundError as e:
        raise RuntimeError("dex-engine is not installed — run it via the bin/dex shim") from e
