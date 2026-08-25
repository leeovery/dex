"""The driver registry (§2): an explicit ordered list — ordering is semantics.

No auto-discovery. Specialized drivers first, ``web`` last as the
catch-all; :func:`~dex_engine.pipeline.detect.detect` treats a match on the
final entry as inconclusive and may sniff. The typed literal below is the
Protocol-conformance point: the type checker verifies every driver against
:class:`~dex_engine.pipeline.types.SourceDriver` at this one assignment.

Two construction paths: :data:`DRIVERS` is the default list — pattern
matching and ``normalize`` need nothing more — and :func:`build_drivers`
rebuilds the same order wired to a real instance (config-ordered
capabilities, the instance root for local-file work). The order is defined
once, in :func:`build_drivers`; the literal is that function applied to
defaults.
"""

from collections.abc import Sequence
from pathlib import Path

from dex_engine.capabilities import Capabilities
from dex_engine.drivers.file import FileDriver
from dex_engine.drivers.github import GitHubDriver
from dex_engine.drivers.paper import PaperDriver
from dex_engine.drivers.podcast import PodcastDriver
from dex_engine.drivers.web import WebDriver
from dex_engine.drivers.x import XDriver
from dex_engine.drivers.youtube import YouTubeDriver

from .types import Config, Kind, SourceDriver

__all__ = ["DRIVERS", "build_drivers", "driver_for"]


def build_drivers(*, capabilities: Capabilities, root: Path | None = None) -> list[SourceDriver]:
    """The ordered registry, wired to an instance (§2).

    Args:
        capabilities: The resolved capability registries — the file driver
            routes formats through them (§6).
        root: The instance root, for ``file:<repo-path>`` work.

    Returns:
        The drivers, specialized first, ``web`` last as the catch-all.
    """
    web = WebDriver()
    return [
        YouTubeDriver(),
        XDriver(),
        GitHubDriver(),
        PaperDriver(web=web),
        PodcastDriver(),
        FileDriver(capabilities=capabilities, root=root),
        web,  # the catch-all — ALWAYS last (§2)
    ]


# The default registry: default provider order, no instance root. The typed
# literal is the Protocol-conformance point (§2). Constructing providers
# reads no ambient state — credentials resolve lazily at availability time.
DRIVERS: list[SourceDriver] = build_drivers(capabilities=Capabilities.build(Config()))


def driver_for(kind: Kind, drivers: Sequence[SourceDriver]) -> SourceDriver | None:
    """The driver owning ``kind``, or ``None`` when the registry lacks one.

    Args:
        kind: The detected kind.
        drivers: The ordered registry in use.

    Returns:
        The driver, or ``None`` — the run layer parks such work honestly
        rather than crashing (partial registries exist in tests; every
        work-unit kind has a driver in the shipped registry).
    """
    for driver in drivers:
        if driver.kind is kind:
            return driver
    return None
