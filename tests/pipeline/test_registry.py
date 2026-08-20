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

    def test_the_registry_ships_seven_drivers_in_design_order(self):
        # Podcast before web (Apple/Spotify/RSS-ish would otherwise fall to
        # the catch-all); file before web (file: keys likewise).
        assert [driver.kind for driver in DRIVERS] == [
            Kind.YOUTUBE,
            Kind.X,
            Kind.GITHUB,
            Kind.PAPER,
            Kind.PODCAST,
            Kind.FILE,
            Kind.WEB,
        ]


class TestDriverFor:
    def test_resolves_registered_kinds(self):
        for driver in DRIVERS:
            assert driver_for(driver.kind, DRIVERS) is driver

    def test_every_work_unit_kind_has_a_driver(self):
        # IMAGE and TEXT are corpus vocabulary only and never become work
        # units; everything else must resolve.
        for kind in Kind:
            if kind in (Kind.IMAGE, Kind.TEXT):
                assert driver_for(kind, DRIVERS) is None
            else:
                assert driver_for(kind, DRIVERS) is not None

    def test_a_partial_registry_resolves_to_none_never_crashes(self):
        assert driver_for(Kind.PODCAST, DRIVERS[:2]) is None
