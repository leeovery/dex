"""Tests for pipeline/registry.py: ordering semantics and kind resolution."""

from dex_engine.capabilities import Capabilities
from dex_engine.drivers.instagram import DEFAULT_BASE_URL, InstagramDriver
from dex_engine.pipeline import registry
from dex_engine.pipeline.registry import build_drivers, default_drivers, driver_for
from dex_engine.pipeline.types import Config, Kind

DRIVERS = default_drivers()


class TestNoImportTimeState:
    def test_importing_the_module_holds_no_built_registry(self):
        # The registry is a function the entry points call. A module-level
        # driver list would be state built by import order, shared by every
        # caller in the process — the revert this pins against.
        assert not hasattr(registry, "DRIVERS")
        built = {
            name: value
            for name, value in vars(registry).items()
            if not name.startswith("__") and isinstance(value, (list, tuple))
        }
        assert built == {}

    def test_each_call_constructs_a_fresh_registry(self):
        first, second = default_drivers(), default_drivers()
        assert first is not second
        assert all(a is not b for a, b in zip(first, second, strict=True))


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

    def test_the_registry_ships_eight_drivers_in_design_order(self):
        # Podcast before web (Apple/Spotify/RSS-ish would otherwise fall to
        # the catch-all); file before web (file: keys likewise); instagram
        # before web too, and it claims its whole host.
        assert [driver.kind for driver in DRIVERS] == [
            Kind.YOUTUBE,
            Kind.X,
            Kind.INSTAGRAM,
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


class TestConfiguredEndpoints:
    def test_the_instagram_proxy_comes_from_config_when_set(self):
        drivers = build_drivers(
            capabilities=Capabilities.build(Config()),
            config=Config(instagram_base_url="https://mirror.test"),
        )
        instagram = driver_for(Kind.INSTAGRAM, drivers)
        assert isinstance(instagram, InstagramDriver)
        assert instagram.base_url == "https://mirror.test"

    def test_omitting_config_leaves_every_driver_on_its_own_default(self):
        # default_drivers() stays config-free — normalize stamps kinds
        # offline and must not need an instance to do it.
        instagram = driver_for(Kind.INSTAGRAM, default_drivers())
        assert isinstance(instagram, InstagramDriver)
        assert instagram.base_url == DEFAULT_BASE_URL
