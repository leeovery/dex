"""Shared fixtures for the engine test suite.

``instance`` builds the corpus/state/enrichment/cache skeleton in a tmp
dir; ``FakeDriver`` is the scriptable driver the pipeline tests drive;
``FlippableProvider`` is the availability seam the waiting-cohort tests
toggle.
"""

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

if "MUTANT_UNDER_TEST" in os.environ:
    # A mutmut run collects its test-to-function map and then re-runs the
    # covering tests to prove the tree is clean — both inside one process, so
    # every `@given` property is called from two executors and hypothesis fails
    # it as a correctness health check. Here it is an artifact of the harness,
    # not of the test: the same property passes alone. Only mutmut sets this
    # variable, so the ordinary suite keeps the check.
    settings.register_profile("mutmut", suppress_health_check=[HealthCheck.differing_executors])
    settings.load_profile("mutmut")

from dex_engine.pipeline.types import (
    Availability,
    Content,
    Format,
    Instance,
    Kind,
    Need,
    Outcome,
    WorkUnit,
)
from dex_engine.pipeline.urls import base_canonical


@pytest.fixture
def instance(tmp_path: Path) -> Instance:
    """A skeleton instance: the corpus/state/enrichment/cache tree in a tmp dir."""
    inst = Instance(root=tmp_path)
    for directory in (inst.corpus_dir, inst.state_dir, inst.enrichment_dir, inst.cache_dir):
        directory.mkdir()
    return inst


def _default_fetch(_unit: WorkUnit) -> Content:
    return Content(meta={"title": "t"}, body="substantial body " * 30)


class FakeDriver:
    """A scriptable driver: the fetch behavior is injected per test.

    ``fetch_fn`` may return an outcome or raise; every handed unit is
    recorded on ``fetched`` so tests can assert what the run dispatched.
    """

    def __init__(
        self,
        kind: Kind = Kind.WEB,
        *,
        fetch_fn: Callable[[WorkUnit], Outcome] | None = None,
        sleep: float = 0.0,
    ) -> None:
        self.kind = kind
        self.sleep = sleep
        self._fetch_fn = fetch_fn or _default_fetch
        self.fetched: list[WorkUnit] = []

    def matches(self, url: str) -> bool:  # noqa: ARG002 — fakes are catch-alls
        return True

    def canonical(self, url: str) -> str:
        return base_canonical(url)

    def fetch(self, unit: WorkUnit) -> Outcome:
        self.fetched.append(unit)
        return self._fetch_fn(unit)


class FlippableProvider:
    """An availability seam the waiting-cohort tests flip on and off."""

    def __init__(self) -> None:
        self.ok = False
        self.reason = "not installed"

    def __call__(
        self,
        need: Need,  # noqa: ARG002 — one switch for every need
        fmt: Format | None = None,  # noqa: ARG002 — and every format
    ) -> Availability:
        return Availability(ok=self.ok, reason=self.reason)


@pytest.fixture
def fake_driver() -> type[FakeDriver]:
    """The FakeDriver factory."""
    return FakeDriver


@pytest.fixture
def flippable_provider() -> FlippableProvider:
    """A fresh FlippableProvider, starting unavailable."""
    return FlippableProvider()
