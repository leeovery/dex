"""Tests for directive.py: list, show and done over an injected directive set."""

import datetime
import json
from pathlib import Path

import pytest

from dex_engine.directive import build_parser, complete, list_pending, main, show
from dex_engine.directives import Directive, append_done, log_path
from dex_engine.pipeline.types import Instance
from dex_engine.version import engine_version
from tests.directives.conftest import make_directive

TODAY = datetime.date(2026, 9, 24)
ENGINE = "0.2.0"


def fixed_today() -> datetime.date:
    return TODAY


def record(instance: Instance, number: int) -> None:
    append_done(log_path(instance.root), number=number, engine="0.1.0", date=TODAY)


def log_text(instance: Instance) -> str | None:
    path = log_path(instance.root)
    return path.read_text() if path.exists() else None


def done(instance: Instance, number: int, shipped: list[Directive]) -> str:
    return complete(instance, number, shipped=shipped, engine=ENGINE, today=fixed_today)


class NoisyCheck:
    """A check that records every root it is asked about."""

    def __init__(self, unmet: list[str]) -> None:
        self.unmet = unmet
        self.roots: list[Path] = []

    def __call__(self, root: Path) -> list[str]:
        self.roots.append(root)
        return self.unmet


def checked(number: int, check: NoisyCheck) -> Directive:
    return Directive(number=number, intent=f"checked {number}", instructions="do it\n", check=check)


class TestList:
    def test_nothing_pending_says_so_plainly(self, instance):
        record(instance, 1)
        assert list_pending(instance, [make_directive(1)]) == "no directives pending"

    def test_an_engine_shipping_nothing_has_nothing_pending(self, instance):
        assert list_pending(instance, []) == "no directives pending"

    def test_pending_directives_are_listed_in_order_with_their_intents(self, instance):
        record(instance, 2)
        shipped = [
            make_directive(3, intent="rewrite the readme"),
            make_directive(1, intent="rehome the scope"),
            make_directive(2, intent="already done here"),
        ]
        assert list_pending(instance, shipped).split("\n") == [
            "2 directives pending, to perform in this order:",
            "directive 1: rehome the scope",
            "directive 3: rewrite the readme",
        ]

    def test_one_pending_directive_is_counted_in_the_singular(self, instance):
        assert list_pending(instance, [make_directive(1)]).startswith("1 directive pending")


class TestShow:
    def test_shows_the_intent_and_the_whole_instructions(self):
        directive = Directive(
            number=2,
            intent="rewrite the readme",
            instructions="# Rewrite\n\nKeep every link.\n\n- one\n- two\n",
            check=lambda _root: [],
        )
        assert show([make_directive(1), directive], 2) == (
            "directive 2: rewrite the readme\n\n# Rewrite\n\nKeep every link.\n\n- one\n- two\n"
        )

    def test_an_unknown_number_names_what_ships(self):
        with pytest.raises(
            ValueError, match=r"no directive 5 ships with this engine \(shipped: 1, 2\)"
        ):
            show([make_directive(1), make_directive(2)], 5)

    def test_an_engine_shipping_nothing_says_none_ship(self):
        with pytest.raises(ValueError, match=r"\(shipped: none\)"):
            show([], 1)


class TestDone:
    def test_a_passing_check_appends_the_record_and_confirms(self, instance):
        output = done(instance, 1, [make_directive(1, intent="rehome the scope")])
        assert json.loads(log_text(instance) or "") == {
            "number": 1,
            "engine": "0.2.0",
            "date": "2026-09-24",
        }
        assert "directive 1 recorded in state/directives.jsonl" in output
        assert output.endswith('together, message "directive 1: rehome the scope"')

    def test_the_check_is_asked_about_this_instance(self, instance):
        check = NoisyCheck([])
        done(instance, 1, [checked(1, check)])
        assert check.roots == [instance.root]

    def test_a_failing_check_names_every_unmet_condition_and_writes_nothing(self, instance):
        unmet = ["lens.md is missing", "config.json does not parse"]
        with pytest.raises(ValueError, match="not done") as err:
            done(instance, 1, [make_directive(1, unmet=unmet)])
        assert str(err.value).split("\n") == [
            "directive 1 is not done: its check found 2 unmet conditions, and nothing was recorded",
            "- lens.md is missing",
            "- config.json does not parse",
        ]
        assert log_text(instance) is None

    def test_one_unmet_condition_is_counted_in_the_singular(self, instance):
        with pytest.raises(ValueError, match="found 1 unmet condition,"):
            done(instance, 1, [make_directive(1, unmet=["lens.md is missing"])])

    def test_an_unknown_number_is_refused_and_writes_nothing(self, instance):
        with pytest.raises(ValueError, match="no directive 7 ships with this engine"):
            done(instance, 7, [make_directive(1)])
        assert log_text(instance) is None

    def test_an_already_recorded_directive_writes_nothing(self, instance):
        record(instance, 1)
        before = log_text(instance)
        check = NoisyCheck(["would fail if asked"])
        output = done(instance, 1, [checked(1, check)])
        assert output == "directive 1 is already recorded as done; nothing written"
        assert log_text(instance) == before
        assert check.roots == []

    def test_a_directive_whose_predecessor_is_pending_is_refused(self, instance):
        record(instance, 1)
        check = NoisyCheck([])
        shipped = [make_directive(1), make_directive(2, intent="rehome the scope")]
        shipped.append(checked(3, check))
        with pytest.raises(ValueError, match="cannot complete") as err:
            done(instance, 3, shipped)
        message = str(err.value)
        assert "directive 3 cannot complete while directive 2 is pending (rehome the scope)" in (
            message
        )
        assert "directive 2 comes first" in message
        assert check.roots == []
        assert (log_text(instance) or "").count("\n") == 1

    def test_the_earliest_pending_directive_is_the_one_named(self, instance):
        shipped = [make_directive(n) for n in (1, 2, 3)]
        with pytest.raises(ValueError, match="while directive 1 is pending"):
            done(instance, 3, shipped)

    def test_completing_in_order_records_each_once(self, instance):
        shipped = [make_directive(1), make_directive(2)]
        done(instance, 1, shipped)
        done(instance, 2, shipped)
        numbers = [json.loads(line)["number"] for line in (log_text(instance) or "").splitlines()]
        assert numbers == [1, 2]
        assert list_pending(instance, shipped) == "no directives pending"


class TestParser:
    @pytest.mark.parametrize("argv", [[], ["show"], ["done"], ["done", "one"], ["perform", "1"]])
    def test_malformed_invocations_are_refused(self, argv):
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)

    def test_numbers_parse_as_integers(self):
        args = build_parser().parse_args(["done", "12"])
        assert (args.command, args.number) == ("done", 12)


class TestMain:
    @pytest.fixture
    def at(self, instance, monkeypatch):
        """Run main from the instance root, over the given shipped set."""

        def use(shipped: list[Directive]) -> Instance:
            monkeypatch.setattr("dex_engine.directive.discover", lambda: shipped)
            monkeypatch.chdir(instance.root)
            return instance

        return use

    def test_list_prints_the_pending_directives(self, at, capsys):
        at([make_directive(1, intent="rehome the scope")])
        main(["list"])
        assert capsys.readouterr().out == (
            "1 directive pending, to perform in this order:\ndirective 1: rehome the scope\n"
        )

    def test_show_prints_the_instructions(self, at, capsys):
        at([make_directive(1)])
        main(["show", "1"])
        assert capsys.readouterr().out.endswith("Do the fixture work for 1.\n")

    def test_done_records_with_the_running_engine_and_exits_zero(self, at, capsys):
        instance = at([make_directive(1)])
        main(["done", "1"])
        assert "directive 1 recorded" in capsys.readouterr().out
        written = json.loads(log_text(instance) or "")
        assert (written["number"], written["engine"]) == (1, engine_version())

    def test_a_refusal_exits_non_zero_with_the_reason(self, at):
        instance = at([make_directive(1, unmet=["lens.md is missing"])])
        with pytest.raises(SystemExit) as exit_info:
            main(["done", "1"])
        assert isinstance(exit_info.value.code, str)
        assert exit_info.value.code.startswith("dex-directive: directive 1 is not done")
        assert exit_info.value.code.endswith("\n- lens.md is missing")
        assert log_text(instance) is None

    def test_an_unknown_number_exits_non_zero(self, at):
        at([])
        with pytest.raises(SystemExit, match="dex-directive: no directive 1 ships"):
            main(["show", "1"])

    def test_a_corrupt_log_exits_non_zero_naming_it(self, at):
        instance = at([make_directive(1)])
        log_path(instance.root).write_text("{torn\n")
        with pytest.raises(SystemExit, match=r"dex-directive: .*directives\.jsonl:1"):
            main(["list"])
