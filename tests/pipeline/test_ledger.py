"""Tests for pipeline/ledger.py: the single serialization boundary."""

import dataclasses
import datetime
import inspect
import json
import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.ledger import LedgerSchemaError
from dex_engine.pipeline.types import Cap, Format, Instance, Kind, LedgerEntry, Need, Status
from dex_engine.pipeline.urls import work_hash
from tests.conftest import FakeDriver
from tests.pipeline.test_run import ITEM, URL, make_ctx, write_item

TODAY = datetime.date(2026, 8, 20)
AT = datetime.datetime(2026, 8, 20, 9, 30, 15, 250000, tzinfo=datetime.UTC)


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
    kind=Kind.FILE,
    format=Format.PDF,
    status=Status.DONE,
    at=AT,
    via="migration-2",
    parent="a1b2c3d4e5",
    depth=2,
    rerun=True,
    path="enrichment/2026-08-19-example-55ad7b/file-73bd78.md",
    title="A whitepaper",
)

PARKED_ENTRY = entry(
    status=Status.MANUAL,
    reason="thin-extraction",
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
        assert record["kind"] == "file"
        assert record["format"] == "pdf"
        assert record["status"] == "done"
        assert record["date"] == "2026-08-20"

    def test_lines_emit_in_schema_order(self):
        record = json.loads(ledger.to_line(FULL_ENTRY))
        assert list(record) == [
            "hash",
            "url",
            "item",
            "kind",
            "format",
            "status",
            "engine",
            "date",
            "at",
            "via",
            "parent",
            "depth",
            "rerun",
            "path",
            "title",
        ]

    def test_the_write_timestamp_is_present_only_when_set(self):
        assert "at" not in json.loads(ledger.to_line(entry()))
        assert json.loads(ledger.to_line(FULL_ENTRY))["at"] == "2026-08-20T09:30:15.250000+00:00"

    def test_reason_serializes_last_after_error_slot(self):
        blocked = entry(status=Status.BLOCKED, attempts=2, reason="403 challenge")
        assert list(json.loads(ledger.to_line(blocked))) == [
            "hash",
            "url",
            "item",
            "kind",
            "status",
            "attempts",
            "engine",
            "date",
            "reason",
        ]


class TestFromLine:
    def test_round_trip(self):
        assert ledger.from_line(ledger.to_line(FULL_ENTRY)) == FULL_ENTRY

    def test_waiting_with_needs_round_trips(self):
        waiting = entry(status=Status.WAITING, needs=Need.TRANSCRIBE)
        assert ledger.from_line(ledger.to_line(waiting)) == waiting

    def test_blocked_acquisition_retry_round_trips_its_needs(self):
        retry = entry(
            status=Status.BLOCKED,
            needs=Need.TRANSCRIBE,
            attempts=2,
            reason="audio acquisition failed: HTTP 429",
        )
        assert ledger.from_line(ledger.to_line(retry)) == retry

    def test_the_typed_cap_round_trips_and_is_absent_without_a_fire(self):
        marker = entry(status=Status.SKIPPED, cap=Cap.URL_REQUESTED, reason="url cap reached")
        assert "cap" not in json.loads(ledger.to_line(entry()))
        assert json.loads(ledger.to_line(marker))["cap"] == "url-requested"
        assert ledger.from_line(ledger.to_line(marker)) == marker

    def test_parked_reason_round_trips(self):
        assert ledger.from_line(ledger.to_line(PARKED_ENTRY)) == PARKED_ENTRY
        assert json.loads(ledger.to_line(PARKED_ENTRY))["reason"] == "thin-extraction"

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

    def test_the_deleted_url_cap_spelling_is_refused_loudly(self):
        # `cap: "url"` was written by no engine, ever — its one writer was
        # the never-taken promotion path, deleted before anything shipped.
        # A hand-written line carrying it must fail loudly, never parse.
        marker = entry(status=Status.SKIPPED, cap=Cap.URL_REQUESTED, reason="url cap reached")
        line = ledger.to_line(marker).replace('"url-requested"', '"url"')
        with pytest.raises(LedgerSchemaError, match="'url'"):
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


class TestWriteTimestampResolution:
    """The union-merge fix: latest-per-hash is by write time, not file order."""

    def scheduled(self) -> LedgerEntry:
        # 09:00 on the scheduled machine: a retry exhausted, the unit blocked.
        return entry(
            status=Status.BLOCKED,
            attempts=3,
            at=datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.UTC),
        )

    def owner(self) -> LedgerEntry:
        # 10:00 on the owner's laptop: the owner parked it by hand.
        return entry(
            status=Status.MANUAL,
            reason="owner ruled: read it myself",
            at=datetime.datetime(2026, 8, 20, 10, 0, tzinfo=datetime.UTC),
        )

    def test_the_later_write_wins_when_it_is_written_first(self, instance: Instance):
        ledger.append(instance.ledger_path, self.owner())
        ledger.append(instance.ledger_path, self.scheduled())
        assert ledger.load(instance.ledger_path)[BASE_ENTRY.hash] == self.owner()

    def test_the_later_write_wins_when_it_is_written_last(self, instance: Instance):
        ledger.append(instance.ledger_path, self.scheduled())
        ledger.append(instance.ledger_path, self.owner())
        assert ledger.load(instance.ledger_path)[BASE_ENTRY.hash] == self.owner()

    def test_sub_second_writes_are_ordered(self, instance: Instance):
        base = datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.UTC)
        later = entry(status=Status.DONE, path="p", at=base + datetime.timedelta(milliseconds=1))
        ledger.append(instance.ledger_path, later)
        ledger.append(instance.ledger_path, entry(status=Status.DEAD, at=base))
        assert ledger.load(instance.ledger_path)[BASE_ENTRY.hash] == later

    def test_a_stamped_line_beats_an_unstamped_one_written_after_it(self, instance: Instance):
        # Every writer stamps, so an unstamped line predates every stamped
        # one however git ordered them.
        stamped = self.owner()
        ledger.append(instance.ledger_path, stamped)
        ledger.append(instance.ledger_path, entry(status=Status.DEAD))
        assert ledger.load(instance.ledger_path)[BASE_ENTRY.hash] == stamped

    def test_two_unstamped_lines_still_resolve_by_file_position(self, instance: Instance):
        ledger.append(instance.ledger_path, entry())
        last = entry(status=Status.DEAD)
        ledger.append(instance.ledger_path, last)
        assert ledger.load(instance.ledger_path)[BASE_ENTRY.hash] == last

    def test_the_loser_does_not_reorder_the_ledger(self, instance: Instance):
        other = entry(hash="ffff000000", url="https://other.test")
        ledger.append(instance.ledger_path, self.owner())
        ledger.append(instance.ledger_path, other)
        ledger.append(instance.ledger_path, self.scheduled())
        assert list(ledger.load(instance.ledger_path)) == [BASE_ENTRY.hash, other.hash]

    def test_compact_keeps_the_line_load_resolves_to(self, instance: Instance):
        ledger.append(instance.ledger_path, self.owner())
        ledger.append(instance.ledger_path, self.scheduled())
        assert ledger.compact(instance.ledger_path) == 1
        assert instance.ledger_path.read_text().count("\n") == 1
        assert ledger.load(instance.ledger_path) == {BASE_ENTRY.hash: self.owner()}

    def test_a_union_merge_of_two_machines_resolves_to_the_later_write(
        self, instance: Instance, tmp_path: Path
    ):
        # The real thing: `merge=union` is what .gitattributes sets on
        # state/*.jsonl, and git's union driver concatenates ours-then-theirs
        # — so the scheduled machine's OLDER line lands last in the file.
        shared = tmp_path / "base.jsonl"
        ours = tmp_path / "ours.jsonl"
        theirs = tmp_path / "theirs.jsonl"
        seed = ledger.to_line(entry(status=Status.QUEUED)) + "\n"
        shared.write_text(seed)
        ours.write_text(seed + ledger.to_line(self.owner()) + "\n")
        theirs.write_text(seed + ledger.to_line(self.scheduled()) + "\n")
        merged = subprocess.run(  # noqa: S603 — test-built args, no shell
            ["git", "merge-file", "-p", "--union", str(ours), str(shared), str(theirs)],  # noqa: S607 — git resolves via PATH like every dev tool
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        instance.ledger_path.write_text(merged)
        assert merged.strip().split("\n")[-1] == ledger.to_line(self.scheduled())
        assert ledger.load(instance.ledger_path)[BASE_ENTRY.hash] == self.owner()

    def test_a_naive_write_timestamp_is_refused(self):
        record = json.loads(ledger.to_line(entry()))
        record["at"] = "2026-08-20T09:00:00"
        with pytest.raises(LedgerSchemaError, match="timezone-aware"):
            ledger.from_line(json.dumps(record))

    def test_an_unparseable_write_timestamp_is_refused(self):
        record = json.loads(ledger.to_line(entry()))
        record["at"] = "yesterday"
        with pytest.raises(LedgerSchemaError, match="ISO 8601"):
            ledger.from_line(json.dumps(record))


class TestFutureDatedWriteTimestamps:
    """A jumped clock costs one line's ordering, never the hash."""

    def clock(self):
        return lambda: AT

    def jumped(self) -> LedgerEntry:
        # The machine whose clock reads 2099: without the ceiling this line
        # wins its hash until the wall clock catches up, and `compact` then
        # deletes every real write behind it.
        return entry(
            status=Status.BLOCKED,
            attempts=3,
            at=datetime.datetime(2099, 1, 1, tzinfo=datetime.UTC),
        )

    def real(self, *, hours: int = 0) -> LedgerEntry:
        return entry(
            status=Status.MANUAL,
            reason="owner ruled: read it myself",
            at=AT + datetime.timedelta(hours=hours),
        )

    def test_a_future_dated_line_loses_to_every_real_write(self, instance: Instance):
        # Three correct writes across three days, all of them behind the
        # jumped line in the file and every one of them lost to it before.
        ledger.append(instance.ledger_path, self.jumped())
        for hours in (-48, -24, 0):
            ledger.append(instance.ledger_path, self.real(hours=hours))
        loaded = ledger.load(instance.ledger_path, now=self.clock())
        assert loaded[BASE_ENTRY.hash] == self.real()

    def test_a_future_dated_line_written_last_still_loses(self, instance: Instance):
        ledger.append(instance.ledger_path, self.real())
        ledger.append(instance.ledger_path, self.jumped())
        loaded = ledger.load(instance.ledger_path, now=self.clock())
        assert loaded[BASE_ENTRY.hash] == self.real()

    def test_compact_keeps_the_real_write_and_drops_the_future_dated_line(self, instance: Instance):
        # The line that goes is the bogus one: compact keeps exactly what
        # `load` resolves to, so it can never delete a real write in favor
        # of a timestamp no clock could have produced.
        ledger.append(instance.ledger_path, self.jumped())
        ledger.append(instance.ledger_path, self.real())
        assert ledger.compact(instance.ledger_path, now=self.clock()) == 1
        assert instance.ledger_path.read_text().strip() == ledger.to_line(self.real())

    def test_a_timestamp_at_the_skew_boundary_is_still_a_write_timestamp(self):
        # The allowance is the ordinary spread between two machines' clocks:
        # a line right at it is a real write and keeps its ordering.
        at_boundary = entry(at=AT + ledger.FUTURE_SKEW_ALLOWANCE)
        past_boundary = entry(at=AT + ledger.FUTURE_SKEW_ALLOWANCE + datetime.timedelta(seconds=1))
        assert ledger.resolution_key(at_boundary, 0, now=AT)[0] == at_boundary.at
        assert ledger.resolution_key(past_boundary, 0, now=AT) == ledger.resolution_key(
            entry(), 0, now=AT
        )

    def test_a_line_with_no_timestamp_resolves_exactly_as_it_always_did(self, instance: Instance):
        # Pre-`at` lines are unstamped, and a future-dated line is READ as
        # unstamped: the two tie, and the tie falls back to file position,
        # as every line resolved before the field existed.
        legacy = entry(status=Status.DEAD)
        ledger.append(instance.ledger_path, self.jumped())
        ledger.append(instance.ledger_path, legacy)
        assert ledger.load(instance.ledger_path, now=self.clock())[BASE_ENTRY.hash] == legacy

    def test_the_mark_verb_heals_a_hash_a_jumped_clock_captured(self, instance: Instance):
        # End to end through the sanctioned repair verb: the reviewer's
        # scenario, where `mark` printed success, the item's frontmatter
        # showed healed, and `load` went on resolving the 2099 line.
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        ledger.append(
            instance.ledger_path,
            entry(
                hash=work_hash(URL),
                url=URL,
                item=ITEM,
                status=Status.BLOCKED,
                attempts=3,
                at=datetime.datetime(2099, 1, 1, tzinfo=datetime.UTC),
            ),
        )
        confirmation = run_mod.mark(ctx, URL, Status.MANUAL, reason="owner ruled")
        assert "manual" in confirmation
        healed = ledger.load(instance.ledger_path)[work_hash(URL)]
        assert (healed.status, healed.reason) == (Status.MANUAL, "owner ruled")
        ledger.compact(instance.ledger_path)
        assert ledger.load(instance.ledger_path)[work_hash(URL)] == healed


class TestABackwardsClock:
    """The other direction: a verb must never report a write that lost.

    The future ceiling bounds a clock that ran ahead; nothing bounds one
    that ran behind, because there is no reference point for "too old". So
    a machine whose clock is set back stamps the owner's correction in the
    past, it loses to the line it was written to supersede, and `compact`
    deletes it as superseded — worse than the forward case, which at least
    kept the correction. What a verb owes is the truth about its own write.
    """

    def behind(self):
        return lambda: AT - datetime.timedelta(days=1)

    def test_mark_refuses_to_report_a_heal_the_ledger_did_not_take(self, instance: Instance):
        # The reproduction: `mark` reported success, `status` showed the
        # unit still done, and `compact` then removed the correction.
        write_item(instance)
        run_mod.run(make_ctx(instance, FakeDriver()))
        stale = make_ctx(instance, FakeDriver(), now=self.behind())
        with pytest.raises(ValueError, match="did not take effect") as caught:
            run_mod.mark(stale, URL, Status.MANUAL, reason="owner ruled: read it myself")
        message = str(caught.value)
        assert URL in message  # the unit it did not heal
        assert "resolves that unit to done" in message  # what it resolves to instead
        assert "clock" in message  # and why
        assert ledger.load(instance.ledger_path)[work_hash(URL)].status is Status.DONE

    def test_a_heal_that_lost_never_refreshes_the_item_as_healed(self, instance: Instance):
        # The frontmatter is derived from the ledger, and the heal is not in
        # it: refreshing on the strength of the append would have shown the
        # owner a correction the next `compact` deletes.
        write_item(instance)
        run_mod.run(make_ctx(instance, FakeDriver()))
        before = (instance.corpus_dir / ITEM[:4] / f"{ITEM}.md").read_text(encoding="utf-8")
        stale = make_ctx(instance, FakeDriver(), now=self.behind())
        with pytest.raises(ValueError, match="did not take effect"):
            run_mod.mark(stale, URL, Status.DEAD, reason="owner ruled: the host is gone")
        assert (instance.corpus_dir / ITEM[:4] / f"{ITEM}.md").read_text(encoding="utf-8") == before

    def test_a_run_names_the_writes_that_lost_instead_of_reporting_them(self, instance: Instance):
        # Same hole one layer up: the enrich-report states outcomes on the
        # strength of having written them, and `enrich fetch` re-fetching a
        # unit on a backwards clock wrote a `done` line that loses.
        write_item(instance)
        run_mod.run(make_ctx(instance, FakeDriver()))
        landed = ledger.load(instance.ledger_path)[work_hash(URL)]
        stale = make_ctx(instance, FakeDriver(), now=self.behind())
        report = " ".join(run_mod.fetch_urls(stale, ITEM, [URL]).split())
        assert "did not take effect" in report
        assert work_hash(URL) in report
        assert ledger.load(instance.ledger_path)[work_hash(URL)] == landed


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

    def test_compact_writes_atomically_and_cleans_its_temp_file(self, instance: Instance):
        ledger.append(instance.ledger_path, entry())
        ledger.append(instance.ledger_path, entry(status=Status.DEAD))
        ledger.compact(instance.ledger_path)
        assert [p.name for p in instance.state_dir.iterdir()] == ["enrichment-ledger.jsonl"]


class TestAppender:
    """The held handle: open-per-line's bytes and durability, without the reopens."""

    def test_one_handle_writes_the_same_bytes_as_per_line_appends(self, instance, tmp_path):
        entries = [
            entry(),
            entry(status=Status.DONE, path="enrichment/i/web-73bd78.md"),
            entry(hash="ffff000000", url="https://other.test"),
        ]
        pooled = tmp_path / "pooled.jsonl"
        appender = ledger.Appender(pooled)
        for record in entries:
            appender.append(record)
        appender.close()
        for record in entries:
            ledger.append(instance.ledger_path, record)
        # The union-merge file format is the contract: byte-identical.
        assert pooled.read_bytes() == instance.ledger_path.read_bytes()

    def test_every_line_is_readable_before_the_handle_closes(self, instance):
        # mark re-reads the ledger right after appending and the run report
        # reads its own writes back; open-per-line's close put each line
        # with the OS, so the held handle must flush per line — a line
        # sitting in a user-space buffer is invisible to every reader and
        # gone if the process dies.
        appender = ledger.Appender(instance.ledger_path)
        written = []
        for n in range(3):
            record = entry(hash=f"{n:010d}", url=f"https://a.test/{n}")
            appender.append(record)
            written.append(record)
            loaded = ledger.load(instance.ledger_path)
            assert [loaded[w.hash] for w in written] == written
        appender.close()

    def test_appends_land_at_the_files_current_end(self, instance):
        # Append mode is load-bearing: a one-shot writer's line arriving
        # between two of the handle's writes (another verb, another
        # process) must never be overwritten.
        first = entry()
        appender = ledger.Appender(instance.ledger_path)
        appender.append(first)
        other = entry(hash="ffff000000", url="https://other.test")
        ledger.append(instance.ledger_path, other)
        last = entry(status=Status.DONE, path="enrichment/i/web-73bd78.md")
        appender.append(last)
        appender.close()
        hashes = [
            json.loads(line)["hash"] for line in instance.ledger_path.read_text().splitlines()
        ]
        assert hashes == [first.hash, other.hash, last.hash]
        loaded = ledger.load(instance.ledger_path)
        assert loaded[other.hash] == other
        assert loaded[first.hash] == last  # same hash: the later line wins


class TestStamp:
    def test_stamp_uses_the_injected_clocks_and_engine(self):
        stamped = ledger.stamp(
            entry(),
            today=lambda: datetime.date(2027, 1, 2),
            now=lambda: datetime.datetime(2027, 1, 2, 14, 30, 5, 125000, tzinfo=datetime.UTC),
            engine_version="0.3.0",
        )
        assert stamped.date == datetime.date(2027, 1, 2)
        assert stamped.at == datetime.datetime(2027, 1, 2, 14, 30, 5, 125000, tzinfo=datetime.UTC)
        assert stamped.engine == "0.3.0"

    def test_a_prior_lines_date_and_engine_are_never_carried(self):
        # There is no carve-out: every write records work, so `date` and
        # `engine` are always this write's. A line written only to correct
        # an earlier one's attribution would need them carried — the drain
        # asks the corpus who owns a unit as it writes instead, so no such
        # line exists.
        prior = entry(date=datetime.date(2026, 1, 5), engine="0.1.0")
        stamped = ledger.stamp(
            prior,
            today=lambda: datetime.date(2027, 1, 2),
            now=lambda: datetime.datetime(2027, 1, 2, 14, 30, 5, 125000, tzinfo=datetime.UTC),
            engine_version="0.3.0",
        )
        assert stamped.date == datetime.date(2027, 1, 2)
        assert stamped.engine == "0.3.0"

    def test_stamping_never_calls_the_ambient_clock(self):
        source = Path(ledger.__file__).read_text(encoding="utf-8")
        assert "date.today()" not in source
        # The layer reads a wall clock in exactly one place: `_utc_now`, the
        # default a reader judges a future-dated `at` against (every reader
        # resolves, and not every reader has a clock to hand). Stamping
        # stays injected — a write instant the writer did not choose orders
        # nothing.
        assert source.count("datetime.now(") == 1
        assert "datetime.now(" not in inspect.getsource(ledger.stamp)


# ---------------------------------------------------------------------------
# Hypothesis properties: random entry sequences ⇒ compact preserves
# last-per-hash and round-trips.
# ---------------------------------------------------------------------------

_HASHES = ("aaaa000000", "bbbb111111", "cccc222222", "dddd333333")
_WORK_KINDS = [k for k in Kind if k not in (Kind.IMAGE, Kind.TEXT)]


_REASON_OPTIONAL = (Status.WAITING, Status.BLOCKED, Status.DEAD)
# A handful of instants, so sequences tie and interleave rather than always
# arriving in a distinct order.
_WRITE_INSTANTS = [
    datetime.datetime(2026, 8, 20, hour, 0, tzinfo=datetime.UTC) for hour in (8, 9, 10)
]


@st.composite
def entries(draw: st.DrawFn) -> LedgerEntry:
    status = draw(st.sampled_from(list(Status)))
    kind = draw(st.sampled_from(_WORK_KINDS))
    provenance = draw(
        st.none() | st.tuples(st.sampled_from(_HASHES), st.integers(min_value=0, max_value=4))
    )
    parent, depth = provenance if provenance is not None else (None, None)
    path = draw(st.none() | st.text(min_size=1)) if status is Status.DONE else None
    # Reasons and work keys are single-line by schema — every writer normalizes.
    single_line = st.text(min_size=1).filter(lambda s: "\n" not in s and "\r" not in s)
    if status in (Status.MANUAL, Status.SKIPPED):
        reason = draw(single_line)
    elif status in _REASON_OPTIONAL:
        reason = draw(st.none() | single_line)
    else:
        reason = None
    if status is Status.WAITING:
        needs = draw(st.sampled_from(list(Need)))
    elif status is Status.BLOCKED:
        needs = draw(st.none() | st.sampled_from(list(Need)))
    else:
        needs = None
    cap = draw(st.none() | st.sampled_from(list(Cap))) if status is Status.SKIPPED else None
    return LedgerEntry(
        hash=draw(st.sampled_from(_HASHES)),
        url=draw(single_line),
        item=draw(st.text(min_size=1)),
        kind=kind,
        format=draw(st.none() | st.sampled_from(list(Format))) if kind is Kind.FILE else None,
        status=status,
        needs=needs,
        attempts=draw(st.integers(min_value=1, max_value=5)) if status is Status.BLOCKED else None,
        cap=cap,
        forced=cap is not None and draw(st.booleans()),
        engine=draw(st.sampled_from(["0.1.0", "0.2.1", "1.0.0"])),
        date=draw(st.dates()),
        at=draw(st.none() | st.sampled_from(_WRITE_INSTANTS)),
        via=draw(st.sampled_from([None, "harvest", "media", "sniff", "migration-1"])),
        parent=parent,
        depth=depth,
        rerun=draw(st.booleans()),
        path=path,
        title=draw(st.none() | st.text(min_size=1)) if path is not None else None,
        error=draw(st.text(min_size=1)) if status is Status.ERROR else None,
        reason=reason,
    )


@given(sequence=st.lists(entries(), max_size=20))
def test_entries_round_trip_through_the_serialization_boundary(sequence: list[LedgerEntry]):
    for e in sequence:
        assert ledger.from_line(ledger.to_line(e)) == e


_OLDEST = datetime.datetime.min.replace(tzinfo=datetime.UTC)


@given(sequence=st.lists(entries(), max_size=20))
def test_compact_preserves_latest_per_hash_and_round_trips(
    sequence: list[LedgerEntry], tmp_path_factory: pytest.TempPathFactory
):
    path = tmp_path_factory.mktemp("ledger") / "enrichment-ledger.jsonl"
    expected: dict[str, LedgerEntry] = {}
    winning: dict[str, tuple[datetime.datetime, int]] = {}
    for position, e in enumerate(sequence):
        ledger.append(path, e)
        key = (e.at or _OLDEST, position)
        if e.hash not in winning or key >= winning[e.hash]:
            expected[e.hash] = e
            winning[e.hash] = key
    assert ledger.load(path) == expected
    removed = ledger.compact(path)
    assert removed == len(sequence) - len(expected)
    assert ledger.load(path) == expected
    if sequence:
        # Count records by "\n" alone — a JSON string field may legally hold
        # unicode line separators, which str.splitlines() would miscount.
        assert path.read_text(encoding="utf-8").count("\n") == len(expected)
