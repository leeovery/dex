"""Tests for directive.py: list, show and done over an injected directive set."""

import datetime
import json
from pathlib import Path

import pytest

from dex_engine.directive import (
    LIST_NEXT,
    MATERIALS_BEGIN,
    MATERIALS_END,
    REFUSED_NEXT,
    build_parser,
    complete,
    list_pending,
    main,
    show,
    show_materials,
)
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
            "",
            LIST_NEXT,
        ]

    def test_the_next_step_performs_each_through_show(self):
        assert "`bin/dex directive show <n>`" in LIST_NEXT

    def test_one_pending_directive_is_counted_in_the_singular(self, instance):
        assert list_pending(instance, [make_directive(1)]).startswith("1 directive pending")


def with_materials(materials: str) -> Directive:
    """Directive 2, whose materials are ``materials`` followed by the root they were made for."""
    return Directive(
        number=2,
        intent="rewrite the readme",
        instructions="# Rewrite\n\nWrite the materials below.\n",
        check=lambda _root: [],
        materials=lambda root: f"{materials}{root.name}\n",
    )


class TestShow:
    def test_shows_the_intent_and_the_whole_instructions(self, instance):
        directive = Directive(
            number=2,
            intent="rewrite the readme",
            instructions="# Rewrite\n\nKeep every link.\n\n- one\n- two\n",
            check=lambda _root: [],
        )
        assert show(instance, [make_directive(1), directive], 2) == (
            "directive 2: rewrite the readme\n\n# Rewrite\n\nKeep every link.\n\n- one\n- two\n"
        )

    def test_materials_follow_the_instructions_between_their_markers(self, instance):
        shown = show(instance, [with_materials("# Title\n\n## Section\n")], 2)
        assert shown == (
            "directive 2: rewrite the readme\n\n# Rewrite\n\nWrite the materials below.\n\n"
            f"{MATERIALS_BEGIN}\n# Title\n\n## Section\n{instance.root.name}\n{MATERIALS_END}\n"
        )

    def test_the_markers_stand_on_lines_of_their_own(self, instance):
        lines = show(instance, [with_materials("")], 2).split("\n")
        assert lines[-4:] == [MATERIALS_BEGIN, instance.root.name, MATERIALS_END, ""]

    def test_materials_without_a_final_newline_still_end_before_the_marker(self, instance):
        directive = Directive(
            number=1,
            intent="one",
            instructions="Do it.",
            check=lambda _root: [],
            materials=lambda _root: "last line",
        )
        assert show(instance, [directive], 1) == (
            f"directive 1: one\n\nDo it.\n\n{MATERIALS_BEGIN}\nlast line\n{MATERIALS_END}\n"
        )

    def test_trailing_blank_lines_in_the_materials_are_kept(self, instance):
        shown = show(instance, [with_materials("body\n\n")], 2)
        assert shown.endswith(f"{MATERIALS_BEGIN}\nbody\n\n{instance.root.name}\n{MATERIALS_END}\n")

    def test_an_unknown_number_names_what_ships(self, instance):
        with pytest.raises(
            ValueError, match=r"no directive 5 ships with this engine \(shipped: 1, 2\)"
        ):
            show(instance, [make_directive(1), make_directive(2)], 5)

    def test_an_engine_shipping_nothing_says_none_ship(self, instance):
        with pytest.raises(ValueError, match=r"\(shipped: none\)"):
            show(instance, [], 1)


class TestShowMaterials:
    def test_the_materials_alone_exactly_as_rendered_for_this_instance(self, instance):
        materials = show_materials(instance, [make_directive(1), with_materials("# T\n\n")], 2)
        assert materials == f"# T\n\n{instance.root.name}\n"

    def test_a_directive_without_materials_is_refused(self, instance):
        with pytest.raises(ValueError, match="directive 1 carries no materials"):
            show_materials(instance, [make_directive(1)], 1)

    def test_an_unknown_number_names_what_ships(self, instance):
        with pytest.raises(ValueError, match=r"no directive 3 ships with this engine"):
            show_materials(instance, [make_directive(1)], 3)


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
            "",
            *REFUSED_NEXT.format(number=1).split("\n"),
        ]
        assert log_text(instance) is None

    def test_the_next_steps_name_the_refused_directive(self, instance):
        record(instance, 1)
        shipped = [make_directive(1), make_directive(2, unmet=["README.md differs"])]
        with pytest.raises(ValueError, match="not done") as err:
            done(instance, 2, shipped)
        assert str(err.value).endswith(f"\n\n{REFUSED_NEXT.format(number=2)}")

    def test_the_issue_wording_is_fixed_so_every_run_dedups_to_one_issue(self):
        steps = REFUSED_NEXT.format(number=4)
        assert '"verb": "directive"' in steps
        assert '"expected": "directive 4 completes and passes its own check"' in steps
        assert '"observed": "directive 4 did not complete on this instance"' in steps

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
        assert REFUSED_NEXT.format(number=3) not in message
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

    def test_show_takes_materials_as_a_flag(self):
        assert build_parser().parse_args(["show", "2", "--materials"]).materials is True
        assert build_parser().parse_args(["show", "2"]).materials is False


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
            f"\n{LIST_NEXT}\n"
        )

    def test_list_with_nothing_pending_prints_no_next_step(self, at, capsys):
        instance = at([make_directive(1)])
        record(instance, 1)
        main(["list"])
        assert capsys.readouterr().out == "no directives pending\n"

    def test_show_prints_the_instructions(self, at, capsys):
        at([make_directive(1)])
        main(["show", "1"])
        assert capsys.readouterr().out.endswith("Do the fixture work for 1.\n")

    def test_show_prints_the_materials_for_the_instance_at_cwd(self, at, capsys):
        instance = at([with_materials("# T\n")])
        main(["show", "2"])
        assert capsys.readouterr().out.endswith(
            f"{MATERIALS_BEGIN}\n# T\n{instance.root.name}\n{MATERIALS_END}\n"
        )

    def test_show_materials_prints_them_alone_byte_for_byte(self, at, capsys):
        instance = at([with_materials("# T\n\nbody\n")])
        main(["show", "2", "--materials"])
        assert capsys.readouterr().out == f"# T\n\nbody\n{instance.root.name}\n"

    def test_show_materials_of_a_directive_without_any_exits_non_zero(self, at):
        at([make_directive(1)])
        with pytest.raises(SystemExit, match="dex-directive: directive 1 carries no materials"):
            main(["show", "1", "--materials"])

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
        assert exit_info.value.code.endswith(
            f"\n- lens.md is missing\n\n{REFUSED_NEXT.format(number=1)}"
        )
        assert log_text(instance) is None

    def test_an_out_of_order_refusal_names_only_the_directive_that_comes_first(self, at):
        instance = at([make_directive(1), make_directive(2)])
        with pytest.raises(SystemExit) as exit_info:
            main(["done", "2"])
        code = exit_info.value.code
        assert isinstance(code, str)
        assert code.startswith("dex-directive: directive 2 cannot complete")
        assert code.endswith("so directive 1 comes first")
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
