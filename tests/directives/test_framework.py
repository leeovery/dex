"""Tests for the directives package: discovery, the completed log, the pending set, the gate."""

import datetime
import json

import pytest

from dex_engine.directives import (
    PENDING_REFUSAL,
    DirectiveError,
    DirectivesPendingError,
    append_done,
    discover,
    log_path,
    pending,
    read_done,
    record_shipped,
    refuse_while_pending,
)
from dex_engine.render.kernel import plural
from tests.directives.conftest import directive_module, make_directive

TODAY = datetime.date(2026, 9, 24)
ENGINE = "0.2.0"


def numbers(directives):
    return [directive.number for directive in directives]


class TestDiscover:
    def test_directives_come_back_in_numeric_order_never_name_order(self, fixture_package):
        package = fixture_package(
            {
                f"directive_{n}.{ext}": directive_module(f"fixture {n}") if ext == "py" else "do it"
                for n in (10, 2, 1)
                for ext in ("py", "md")
            }
        )
        assert numbers(discover(package)) == [1, 2, 10]

    def test_a_directive_carries_its_intent_instructions_and_check(self, fixture_package, tmp_path):
        package = fixture_package(
            {
                "directive_1.py": directive_module("rehome the scope", unmet=["no home yet"]),
                "directive_1.md": "# Rehome\n\nMove every section.\n",
            }
        )
        [directive] = discover(package)
        assert directive.number == 1
        assert directive.intent == "rehome the scope"
        assert directive.instructions == "# Rehome\n\nMove every section.\n"
        assert directive.check(tmp_path) == ["no home yet"]

    def test_modules_that_are_not_directives_are_ignored(self, fixture_package):
        package = fixture_package(
            {
                "helpers.py": "SHARED = 1\n",
                "directive_1.py": directive_module("one"),
                "directive_1.md": "do it",
            }
        )
        assert numbers(discover(package)) == [1]

    def test_an_empty_package_ships_nothing(self, fixture_package):
        assert discover(fixture_package({})) == []

    @pytest.mark.parametrize("name", ["directive_01", "directive_x", "directive"])
    def test_a_near_miss_module_name_is_loud(self, fixture_package, name):
        package = fixture_package({f"{name}.py": directive_module("near miss")})
        with pytest.raises(DirectiveError, match="plain integer"):
            discover(package)

    def test_a_directive_without_instructions_is_loud(self, fixture_package):
        package = fixture_package({"directive_1.py": directive_module("one")})
        with pytest.raises(DirectiveError, match=r"directive_1\.md beside it is missing or empty"):
            discover(package)

    def test_blank_instructions_are_loud(self, fixture_package):
        package = fixture_package(
            {"directive_1.py": directive_module("one"), "directive_1.md": " \n\n"}
        )
        with pytest.raises(DirectiveError, match="missing or empty"):
            discover(package)

    @pytest.mark.parametrize(
        "source",
        [
            "def check(root):\n    return []\n",
            "INTENT = ''\n\ndef check(root):\n    return []\n",
            "INTENT = 'two\\nlines'\n\ndef check(root):\n    return []\n",
            "INTENT = 7\n\ndef check(root):\n    return []\n",
        ],
        ids=["absent", "empty", "multi-line", "not-a-string"],
    )
    def test_an_intent_that_is_not_one_line_is_loud(self, fixture_package, source):
        package = fixture_package({"directive_1.py": source, "directive_1.md": "do it"})
        with pytest.raises(DirectiveError, match="INTENT as one non-empty line"):
            discover(package)

    @pytest.mark.parametrize("source", ["INTENT = 'one'\n", "INTENT = 'one'\ncheck = []\n"])
    def test_a_directive_without_a_callable_check_is_loud(self, fixture_package, source):
        package = fixture_package({"directive_1.py": source, "directive_1.md": "do it"})
        with pytest.raises(DirectiveError, match=r"check\(root\) -> list\[str\]"):
            discover(package)

    def test_a_directive_carries_its_materials(self, fixture_package, tmp_path):
        source = directive_module("readme") + (
            "\n\ndef materials(root):\n    return f'# {root.name}\\n'\n"
        )
        package = fixture_package({"directive_1.py": source, "directive_1.md": "do it"})
        [directive] = discover(package)
        assert directive.materials is not None
        assert directive.materials(tmp_path / "dex-cooking") == "# dex-cooking\n"

    def test_materials_are_optional(self, fixture_package):
        package = fixture_package(
            {"directive_1.py": directive_module("one"), "directive_1.md": "do it"}
        )
        [directive] = discover(package)
        assert directive.materials is None

    def test_materials_that_are_not_callable_are_loud(self, fixture_package):
        source = directive_module("one") + "\nmaterials = '# the readme'\n"
        package = fixture_package({"directive_1.py": source, "directive_1.md": "do it"})
        with pytest.raises(DirectiveError, match=r"not as materials\(root\) -> str"):
            discover(package)

    def test_the_engines_own_set_discovers_cleanly(self):
        # Whatever ships: every shipped directive loads its intent, check and
        # instructions, and the numbers run ascending without repeats.
        shipped = numbers(discover())
        assert shipped == sorted(set(shipped))


class TestLog:
    def test_the_log_lives_in_state(self, tmp_path):
        assert log_path(tmp_path) == tmp_path / "state" / "directives.jsonl"

    def test_a_missing_log_is_empty(self, tmp_path):
        assert read_done(log_path(tmp_path)) == set()

    def test_append_then_read_round_trips(self, tmp_path):
        path = log_path(tmp_path)
        append_done(path, number=1, engine=ENGINE, date=TODAY)
        append_done(path, number=3, engine=ENGINE, date=TODAY)
        assert read_done(path) == {1, 3}

    def test_a_record_is_number_engine_date(self, tmp_path):
        path = log_path(tmp_path)
        append_done(path, number=1, engine=ENGINE, date=TODAY)
        assert path.read_text().split("\n") == [
            json.dumps({"number": 1, "engine": "0.2.0", "date": "2026-09-24"}),
            "",
        ]

    def test_appends_never_rewrite_earlier_records(self, tmp_path):
        path = log_path(tmp_path)
        append_done(path, number=1, engine="0.1.0", date=TODAY)
        first = path.read_text()
        append_done(path, number=2, engine=ENGINE, date=TODAY)
        assert path.read_text().startswith(first)

    def test_union_merged_duplicates_read_as_one(self, tmp_path):
        # Two machines both performed directive 1; merge=union keeps both lines.
        path = log_path(tmp_path)
        path.parent.mkdir(parents=True)
        one_here = json.dumps({"number": 1, "engine": "0.2.0", "date": "2026-09-24"})
        one_there = json.dumps({"number": 1, "engine": "0.2.1", "date": "2026-09-25"})
        two = json.dumps({"number": 2, "engine": "0.2.1", "date": "2026-09-25"})
        path.write_text(f"{one_here}\n{two}\n{one_there}\n")
        assert read_done(path) == {1, 2}

    def test_a_blank_line_mid_log_is_skipped_not_read_as_the_end(self, tmp_path):
        path = log_path(tmp_path)
        path.parent.mkdir(parents=True)
        one = json.dumps({"number": 1, "engine": "0.2.0", "date": "2026-09-24"})
        two = json.dumps({"number": 2, "engine": "0.2.0", "date": "2026-09-24"})
        path.write_text(f"{one}\n\n  \n{two}\n")
        assert read_done(path) == {1, 2}

    def test_a_torn_record_names_the_file_line_and_repair(self, tmp_path):
        path = log_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"number": 1, "engine": "0.2.0", "date": "2026-09-24"}\n{"number": 2,\n')
        with pytest.raises(DirectiveError) as err:
            read_done(path)
        message = str(err.value)
        assert "directives.jsonl:2" in message
        assert "unparseable completed-directive record" in message
        assert "delete the torn line and re-run" in message

    @pytest.mark.parametrize(
        ("line", "complaint"),
        [
            ("[1]", "JSON object"),
            ('{"engine": "0.2.0", "date": "2026-09-24"}', "integer 'number'"),
            ('{"number": true, "engine": "0.2.0", "date": "2026-09-24"}', "integer 'number'"),
        ],
    )
    def test_a_line_that_is_not_a_record_is_loud(self, tmp_path, line, complaint):
        path = log_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(line + "\n")
        with pytest.raises(DirectiveError, match=complaint):
            read_done(path)


class TestPending:
    def test_pending_is_the_shipped_numbers_the_log_lacks_ascending(self, tmp_path):
        append_done(log_path(tmp_path), number=2, engine=ENGINE, date=TODAY)
        shipped = [make_directive(3), make_directive(1), make_directive(2)]
        assert numbers(pending(tmp_path, shipped)) == [1, 3]

    def test_nothing_is_pending_once_every_number_is_logged(self, tmp_path):
        for number in (1, 2):
            append_done(log_path(tmp_path), number=number, engine=ENGINE, date=TODAY)
        assert pending(tmp_path, [make_directive(1), make_directive(2)]) == []

    def test_a_record_the_engine_does_not_ship_changes_nothing(self, tmp_path):
        # A newer engine on another machine performed a directive this
        # engine has never heard of; its record merges in with the pull.
        append_done(log_path(tmp_path), number=9, engine="0.3.0", date=TODAY)
        assert numbers(pending(tmp_path, [make_directive(1)])) == [1]

    def test_no_override_reads_the_engines_own_set(self, tmp_path):
        assert numbers(pending(tmp_path)) == numbers(discover())


class TestRecordShipped:
    def test_every_shipped_directive_is_recorded_done(self, tmp_path):
        shipped = [make_directive(1), make_directive(4)]
        record_shipped(tmp_path, engine=ENGINE, date=TODAY, shipped=shipped)
        records = [json.loads(line) for line in log_path(tmp_path).read_text().splitlines()]
        assert records == [
            {"number": 1, "engine": "0.2.0", "date": "2026-09-24"},
            {"number": 4, "engine": "0.2.0", "date": "2026-09-24"},
        ]
        assert pending(tmp_path, shipped) == []

    def test_nothing_shipped_writes_nothing(self, tmp_path):
        record_shipped(tmp_path, engine=ENGINE, date=TODAY, shipped=[])
        assert not log_path(tmp_path).exists()


class TestRefuseWhilePending:
    def test_nothing_pending_lets_content_work_through(self, tmp_path):
        append_done(log_path(tmp_path), number=1, engine=ENGINE, date=TODAY)
        refuse_while_pending(tmp_path, [make_directive(1)])

    def test_an_engine_shipping_nothing_lets_content_work_through(self, tmp_path):
        refuse_while_pending(tmp_path, [])

    def test_pending_directives_refuse_with_the_count_and_the_next_step(self, tmp_path):
        append_done(log_path(tmp_path), number=2, engine=ENGINE, date=TODAY)
        shipped = [make_directive(1), make_directive(2), make_directive(3)]
        with pytest.raises(DirectivesPendingError) as refused:
            refuse_while_pending(tmp_path, shipped)
        assert str(refused.value) == PENDING_REFUSAL.format(count="2 directives")

    def test_one_pending_directive_is_counted_in_the_singular(self, tmp_path):
        with pytest.raises(DirectivesPendingError) as refused:
            refuse_while_pending(tmp_path, [make_directive(1)])
        assert str(refused.value) == PENDING_REFUSAL.format(count="1 directive")

    def test_the_refusal_names_the_command_that_performs_them(self):
        assert "`bin/dex directive list`" in PENDING_REFUSAL

    def test_a_corrupt_log_is_loud_rather_than_read_as_nothing_pending(self, tmp_path):
        log_path(tmp_path).parent.mkdir()
        log_path(tmp_path).write_text("{torn\n")
        with pytest.raises(DirectiveError, match=r"directives\.jsonl:1"):
            refuse_while_pending(tmp_path, [make_directive(1)])

    def test_no_override_reads_the_engines_own_set(self, tmp_path):
        with pytest.raises(DirectivesPendingError) as refused:
            refuse_while_pending(tmp_path)
        count = plural(len(discover()), "directive")
        assert str(refused.value) == PENDING_REFUSAL.format(count=count)
