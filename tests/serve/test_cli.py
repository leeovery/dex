"""Tests for serve/cli.py: argument shape and the refusals that never start a server."""

from pathlib import Path

import pytest

from dex_engine.serve.cli import build_parser, main

from .conftest import BOOKS, COFFEE


class TestParser:
    def test_at_least_one_instance_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_the_flag_repeats(self, roots):
        args = build_parser().parse_args(["--instance", str(roots[0]), "--instance", str(roots[1])])
        assert args.instances == [Path(roots[0]), Path(roots[1])]

    def test_typoed_flags_are_loud(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--instances", "."])


class TestMain:
    def test_a_missing_root_exits_cleanly(self, tmp_path):
        with pytest.raises(SystemExit) as excinfo:
            main(["--instance", str(tmp_path / "absent")])
        assert "dex-serve" in str(excinfo.value)
        assert "no such directory" in str(excinfo.value)

    def test_a_root_that_is_not_an_instance_exits_cleanly(self, tmp_path):
        with pytest.raises(SystemExit) as excinfo:
            main(["--instance", str(tmp_path)])
        assert "not a dex instance" in str(excinfo.value)

    def test_a_duplicate_name_exits_before_serving(self, roots, tmp_path):
        clone = tmp_path / "elsewhere" / COFFEE
        (clone / "corpus").mkdir(parents=True)
        (clone / "state").mkdir()
        with pytest.raises(SystemExit) as excinfo:
            main(["--instance", str(roots[0]), "--instance", str(clone)])
        assert COFFEE in str(excinfo.value)

    def test_a_valid_roster_reaches_the_transport(self, roots, monkeypatch):
        # The one thing left to check: main hands the built server to stdio.
        served = {}
        monkeypatch.setattr(
            "dex_engine.serve.cli.build_server",
            lambda roster: type(
                "Stub",
                (),
                {"run": lambda _self, transport: served.update(roster=roster, transport=transport)},
            )(),
        )
        main(["--instance", str(roots[0]), "--instance", str(roots[1])])
        assert list(served["roster"].instances) == [COFFEE, BOOKS]
        assert served["transport"] == "stdio"
