"""The driver registry (§2): an explicit ordered list — ordering is semantics.

No auto-discovery. Specialized drivers first, ``web`` last as the
catch-all; :func:`~dex_engine.pipeline.detect.detect` treats a match on the
final entry as inconclusive and may sniff. The typed literal below is the
Protocol-conformance point: the type checker verifies every driver against
:class:`~dex_engine.pipeline.types.SourceDriver` at this one assignment.

``file`` and ``podcast`` drivers arrive in phase 3 — absent here, so
:func:`driver_for` returns ``None`` for their kinds and the run layer parks
that work honestly.
"""

from collections.abc import Sequence

from dex_engine.drivers.github import GitHubDriver
from dex_engine.drivers.paper import PaperDriver
from dex_engine.drivers.web import WebDriver
from dex_engine.drivers.x import XDriver
from dex_engine.drivers.youtube import YouTubeDriver

from .types import Kind, SourceDriver

__all__ = ["DRIVERS", "driver_for"]

_web = WebDriver()

DRIVERS: list[SourceDriver] = [
    YouTubeDriver(),
    XDriver(),
    GitHubDriver(),
    PaperDriver(web=_web),
    _web,  # the catch-all — ALWAYS last (§2)
]


def driver_for(kind: Kind, drivers: Sequence[SourceDriver]) -> SourceDriver | None:
    """The driver owning ``kind``, or ``None`` when no driver ships yet.

    Args:
        kind: The detected kind.
        drivers: The ordered registry in use.

    Returns:
        The driver, or ``None`` (``file``/``podcast`` until phase 3) — the
        run layer parks such work rather than crashing.
    """
    for driver in drivers:
        if driver.kind is kind:
            return driver
    return None
