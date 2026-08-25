"""Tests for pipeline/registry.py: ordering semantics and kind resolution."""

from dex_engine.pipeline.registry import DRIVERS, driver_for
from dex_engine.pipeline.types import Kind


class TestOrdering:
    def test_web_is_last_the_catch_all(self):
        assert DRIVERS[-1].kind is Kind.WEB

    def test_exactly_one_driver_per_kind(self):
        kinds = [driver.kind for driver in DRIVERS]
        assert len(kinds) == len(set(kinds))

    def test_only_the_catch_all_matches_everything(self):
        probe = "https://no-driver-owns.example.test/x"
        matching = [driver.kind for driver in DRIVERS if driver.matches(probe)]
        assert matching == [Kind.WEB]

    def test_phase_2_registry_ships_five_drivers(self):
        assert [driver.kind for driver in DRIVERS] == [
            Kind.YOUTUBE,
            Kind.X,
            Kind.GITHUB,
            Kind.PAPER,
            Kind.WEB,
        ]


class TestDriverFor:
    def test_resolves_registered_kinds(self):
        for driver in DRIVERS:
            assert driver_for(driver.kind, DRIVERS) is driver

    def test_unshipped_kinds_resolve_to_none(self):
        # file and podcast drivers arrive in phase 3 — the run layer parks
        # their work instead of crashing.
        assert driver_for(Kind.FILE, DRIVERS) is None
        assert driver_for(Kind.PODCAST, DRIVERS) is None
