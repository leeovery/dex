"""Tests for serve/roster.py: which instances are served, and the id namespace."""

from pathlib import Path

import pytest

from dex_engine.serve.roster import NotFoundError, build_roster, qualify

from .conftest import BOOKS, COFFEE, V60


class TestBuildRoster:
    def test_names_instances_by_directory_basename_in_order(self, roots):
        assert list(build_roster(roots).instances) == [COFFEE, BOOKS]

    def test_a_trailing_slash_still_names_the_directory(self, roots):
        # What a shell's tab-completion hands the flag.
        assert list(build_roster([Path(f"{roots[0]}/")]).instances) == [COFFEE]

    def test_a_missing_path_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="no such directory"):
            build_roster([tmp_path / "absent"])

    def test_a_directory_that_is_not_an_instance_is_refused(self, tmp_path):
        (tmp_path / "notes").mkdir()
        with pytest.raises(ValueError, match="not a dex instance"):
            build_roster([tmp_path / "notes"])

    def test_a_half_built_instance_names_what_is_missing(self, tmp_path):
        (tmp_path / "half" / "corpus").mkdir(parents=True)
        with pytest.raises(ValueError, match="it has no state/"):
            build_roster([tmp_path / "half"])

    def test_two_roots_with_one_basename_are_refused(self, roots, tmp_path):
        clone = tmp_path / "elsewhere" / COFFEE
        (clone / "corpus").mkdir(parents=True)
        (clone / "state").mkdir()
        with pytest.raises(ValueError, match="two instances would answer to the name"):
            build_roster([*roots, clone])


class TestSelect:
    def test_no_name_fans_out_over_the_whole_roster(self, roster):
        assert [name for name, _ in roster.select(None)] == [COFFEE, BOOKS]

    def test_a_name_restricts_to_one(self, roster):
        assert [name for name, _ in roster.select(BOOKS)] == [BOOKS]

    def test_an_unknown_name_lists_the_served_ones(self, roster):
        with pytest.raises(NotFoundError, match="dex-coffee, dex-books"):
            roster.select("dex-nope")


class TestResolve:
    def test_a_namespaced_id_parses_back_to_its_instance(self, roster):
        name, instance, bare = roster.resolve(qualify(COFFEE, V60))
        assert (name, bare) == (COFFEE, V60)
        assert instance.root.name == COFFEE

    @pytest.mark.parametrize(
        "item_id",
        [V60, "", "/", f"/{V60}", f"{COFFEE}/", f"{COFFEE}/sub/{V60}", f"{COFFEE}/../{V60}"],
    )
    def test_anything_that_is_not_two_parts_is_refused(self, roster, item_id):
        with pytest.raises(NotFoundError, match="not a dex item id"):
            roster.resolve(item_id)

    def test_a_well_formed_id_naming_no_instance_is_refused(self, roster):
        with pytest.raises(NotFoundError, match="no instance named"):
            roster.resolve(f"dex-nope/{V60}")
