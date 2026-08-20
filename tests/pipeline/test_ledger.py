"""Tests for pipeline/ledger.py: the single serialization boundary."""

import dataclasses
import datetime
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dex_engine.pipeline import ledger
from dex_engine.pipeline.ledger import LedgerSchemaError
from dex_engine.pipeline.types import Instance, Kind, LedgerEntry, Need, Status

TODAY = datetime.date(2026, 8, 20)


BASE_ENTRY = LedgerEntry(
    hash="73bd784849",
    url="https://example.test/post",
    item="2026-08-19-example-55ad7b",
    kind=Kind.WEB,
    status=Status.QUEUED,
    engine="0.2.1",
    date=TODAY,
)


def entry(**overrides: object) -> LedgerEntry:
    return dataclasses.replace(BASE_ENTRY, **overrides)


FULL_ENTRY = entry(
    kind=Kind.X,
    status=Status.DONE,
    via="migration-2",
    parent="a1b2c3d4e5",
    depth=2,
    rerun=True,
    path="enrichment/2026-08-19-example-55ad7b/x-73bd78.md",
    title="A thread",
)


class TestToLine:
    def test_none_fields_are_dropped(self):
        record = json.loads(ledger.to_line(entry()))
        assert set(record) == {"hash", "url", "item", "kind", "status", "engine", "date"}

    def test_rerun_false_is_dropped_true_is_kept(self):
        assert "rerun" not in json.loads(ledger.to_line(entry()))
        assert json.loads(ledger.to_line(FULL_ENTRY))["rerun"] is True

    def test_enums_and_dates_serialize_as_plain_strings(self):
        record = json.loads(ledger.to_line(FULL_ENTRY))
        assert record["kind"] == "x"
        assert record["status"] == "done"
        assert record["date"] == "2026-08-20"

    def test_lines_emit_in_schema_order(self):
        record = json.loads(ledger.to_line(FULL_ENTRY))
        assert list(record) == [
            "hash",
            "url",
            "item",
            "kind",
            "status",
            "engine",
            "date",
            "via",
            "parent",
            "depth",
            "rerun",
            "path",
            "title",
        ]


class TestFromLine:
    def test_round_trip(self):
        assert ledger.from_line(ledger.to_line(FULL_ENTRY)) == FULL_ENTRY

    def test_waiting_with_needs_round_trips(self):
        waiting = entry(status=Status.WAITING, needs=Need.TRANSCRIBE)
        assert ledger.from_line(ledger.to_line(waiting)) == waiting

    def test_not_json_is_a_schema_error(self):
        with pytest.raises(LedgerSchemaError, match="unparseable"):
            ledger.from_line("{oops")

    def test_non_object_is_a_schema_error(self):
        with pytest.raises(LedgerSchemaError, match="not a JSON object"):
            ledger.from_line('["a", "b"]')

    def test_pre_rename_kind_names_migration_1(self):
        line = ledger.to_line(entry()).replace('"web"', '"tweet"')
        with pytest.raises(LedgerSchemaError, match="migration 1") as exc:
            ledger.from_line(line)
        assert "'x'" in str(exc.value)

    def test_retired_status_names_migration_1(self):
        line = ledger.to_line(entry()).replace('"queued"', '"nocaptions"')
        with pytest.raises(LedgerSchemaError, match="migration 1") as exc:
            ledger.from_line(line)
        assert "waiting" in str(exc.value)

    def test_missing_required_field_names_the_likely_migration(self):
        record = json.loads(ledger.to_line(entry()))
        del record["engine"]
        with pytest.raises(LedgerSchemaError, match="migration 1") as exc:
            ledger.from_line(json.dumps(record))
        assert "engine" in str(exc.value)

    def test_unknown_field_is_a_schema_error(self):
        record = json.loads(ledger.to_line(entry()))
        record["surprise"] = 1
        with pytest.raises(LedgerSchemaError, match="surprise"):
            ledger.from_line(json.dumps(record))

    def test_unknown_enum_value_is_a_schema_error(self):
        line = ledger.to_line(entry()).replace('"web"', '"gopher"')
        with pytest.raises(LedgerSchemaError, match="gopher"):
            ledger.from_line(line)

    def test_invariant_violation_is_a_schema_error_not_a_valueerror_later(self):
        record = json.loads(ledger.to_line(entry()))
        record["status"] = "waiting"  # waiting without needs
        with pytest.raises(LedgerSchemaError, match="needs"):
            ledger.from_line(json.dumps(record))

    def test_wrong_field_type_is_a_schema_error(self):
        record = json.loads(ledger.to_line(entry()))
        record["attempts"] = "three"
        with pytest.raises(LedgerSchemaError, match="attempts"):
            ledger.from_line(json.dumps(record))


class TestLoadAppendCompact:
    def test_missing_file_is_an_empty_ledger(self, instance: Instance):
        assert ledger.load(instance.ledger_path) == {}

    def test_append_then_load(self, instance: Instance):
        ledger.append(instance.ledger_path, FULL_ENTRY)
        assert ledger.load(instance.ledger_path) == {FULL_ENTRY.hash: FULL_ENTRY}

    def test_last_per_hash_wins(self, instance: Instance):
        ledger.append(instance.ledger_path, entry())
        done = entry(status=Status.DONE, path="enrichment/i/web-73bd78.md")
        ledger.append(instance.ledger_path, done)
        loaded = ledger.load(instance.ledger_path)
        assert loaded == {done.hash: done}

    def test_blank_lines_are_skipped(self, instance: Instance):
        instance.ledger_path.parent.mkdir(exist_ok=True)
        instance.ledger_path.write_text(ledger.to_line(entry()) + "\n\n\n")
        assert len(ledger.load(instance.ledger_path)) == 1

    def test_load_errors_name_file_and_line(self, instance: Instance):
        instance.ledger_path.write_text(ledger.to_line(entry()) + "\n{broken\n")
        with pytest.raises(LedgerSchemaError, match=r"enrichment-ledger\.jsonl:2"):
            ledger.load(instance.ledger_path)

    def test_compact_keeps_only_the_latest_line_per_hash(self, instance: Instance):
        superseded = entry(status=Status.BLOCKED, attempts=1)
        final = entry(status=Status.DONE, path="enrichment/i/web-73bd78.md")
        other = entry(hash="ffff000000", url="https://other.test")
        for e in (superseded, other, final):
            ledger.append(instance.ledger_path, e)
        removed = ledger.compact(instance.ledger_path)
        assert removed == 1
        lines = instance.ledger_path.read_text().splitlines()
        assert len(lines) == 2
        assert ledger.load(instance.ledger_path) == {final.hash: final, other.hash: other}

    def test_compact_is_idempotent(self, instance: Instance):
        ledger.append(instance.ledger_path, entry())
        ledger.append(instance.ledger_path, entry(status=Status.DEAD))
        assert ledger.compact(instance.ledger_path) == 1
        before = instance.ledger_path.read_text()
        assert ledger.compact(instance.ledger_path) == 0
        assert instance.ledger_path.read_text() == before

    def test_compact_missing_file_is_a_noop(self, instance: Instance):
        assert ledger.compact(instance.ledger_path) == 0
        assert not instance.ledger_path.exists()


class TestStamp:
    def test_stamp_uses_the_injected_clock_and_engine(self):
        stamped = ledger.stamp(
            entry(),
            today=lambda: datetime.date(2027, 1, 2),
            engine_version="0.3.0",
        )
        assert stamped.date == datetime.date(2027, 1, 2)
        assert stamped.engine == "0.3.0"

    def test_layer_never_calls_the_ambient_clock(self):
        source = Path(ledger.__file__).read_text(encoding="utf-8")
        assert "date.today()" not in source


# ---------------------------------------------------------------------------
# Hypothesis properties (§15): random entry sequences ⇒ compact preserves
# last-per-hash and round-trips.
# ---------------------------------------------------------------------------

_HASHES = ("aaaa000000", "bbbb111111", "cccc222222", "dddd333333")
_WORK_KINDS = [k for k in Kind if k not in (Kind.IMAGE, Kind.TEXT)]


@st.composite
def entries(draw: st.DrawFn) -> LedgerEntry:
    status = draw(st.sampled_from(list(Status)))
    is_done = status is Status.DONE
    return LedgerEntry(
        hash=draw(st.sampled_from(_HASHES)),
        url=draw(st.text(min_size=1)),
        item=draw(st.text(min_size=1)),
        kind=draw(st.sampled_from(_WORK_KINDS)),
        status=status,
        needs=draw(st.sampled_from(list(Need))) if status is Status.WAITING else None,
        attempts=draw(st.integers(min_value=1, max_value=5)) if status is Status.BLOCKED else None,
        engine=draw(st.sampled_from(["0.1.0", "0.2.1", "1.0.0"])),
        date=draw(st.dates()),
        via=draw(st.sampled_from([None, "harvest", "thread", "media", "sniff", "migration-1"])),
        parent=draw(st.none() | st.sampled_from(_HASHES)),
        depth=draw(st.none() | st.integers(min_value=0, max_value=4)),
        rerun=draw(st.booleans()),
        path=draw(st.none() | st.text(min_size=1)) if is_done else None,
        title=draw(st.none() | st.text(min_size=1)) if is_done else None,
        error=draw(st.text(min_size=1)) if status is Status.ERROR else None,
    )


@given(sequence=st.lists(entries(), max_size=20))
def test_entries_round_trip_through_the_serialization_boundary(sequence: list[LedgerEntry]):
    for e in sequence:
        assert ledger.from_line(ledger.to_line(e)) == e


@given(sequence=st.lists(entries(), max_size=20))
def test_compact_preserves_last_per_hash_and_round_trips(
    sequence: list[LedgerEntry], tmp_path_factory: pytest.TempPathFactory
):
    path = tmp_path_factory.mktemp("ledger") / "enrichment-ledger.jsonl"
    expected: dict[str, LedgerEntry] = {}
    for e in sequence:
        ledger.append(path, e)
        expected[e.hash] = e
    assert ledger.load(path) == expected
    removed = ledger.compact(path)
    assert removed == len(sequence) - len(expected)
    assert ledger.load(path) == expected
    if sequence:
        # Count records by "\n" alone — a JSON string field may legally hold
        # unicode line separators, which str.splitlines() would miscount.
        assert path.read_text(encoding="utf-8").count("\n") == len(expected)
