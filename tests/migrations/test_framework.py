"""Tests for the migrations framework: discovery, log, runner."""

import datetime
import json
from pathlib import Path

import pytest

from dex_engine.migrations import (
    AppliedMigration,
    MigrationError,
    append_applied,
    discover,
    log_path,
    read_applied,
    run_pending,
)
from dex_engine.pipeline.types import MigrationReport

TODAY = datetime.date(2026, 8, 20)
NOW = datetime.datetime(2026, 8, 20, 8, 0, 0, 500000, tzinfo=datetime.UTC)
ENGINE = "0.5.0"


def fixed_today() -> datetime.date:
    return TODAY


def fixed_now() -> datetime.datetime:
    return NOW


class FakeMigration:
    def __init__(self, number, apply_fn=None):
        self.number = number
        self.intent = f"fake migration {number}"
        self.applied = 0
        self._apply_fn = apply_fn

    def apply(self, root: Path) -> MigrationReport:
        self.applied += 1
        if self._apply_fn is not None:
            return self._apply_fn(root)
        return MigrationReport(actions=[f"did {self.number}"])


class TestDiscover:
    def test_shipped_migrations_in_numeric_order(self):
        migrations = discover(today=fixed_today, now=fixed_now, engine_version=ENGINE)
        assert [m.number for m in migrations] == [1, 2, 3, 4, 5, 6, 7]

    def test_intents_are_single_line_and_stated(self):
        # The sync-report surface requires single-line intents.
        for migration in discover(today=fixed_today, now=fixed_now, engine_version=ENGINE):
            assert migration.intent
            assert "\n" not in migration.intent

    def test_near_miss_migration_filenames_are_loud(self, tmp_path, monkeypatch):
        # migration_01 / migration_x would silently never run — a latent
        # packaging bug the discovery must refuse.
        (tmp_path / "migration_01.py").write_text("")
        monkeypatch.setattr("dex_engine.migrations.__path__", [str(tmp_path)])
        with pytest.raises(MigrationError, match="plain integer"):
            discover(today=fixed_today, now=fixed_now, engine_version=ENGINE)


class TestAppliedLog:
    def test_missing_file_is_empty_log(self, tmp_path):
        assert read_applied(tmp_path / "state" / "migrations.jsonl") == set()

    def test_append_then_read_round_trips(self, tmp_path):
        path = log_path(tmp_path)
        append_applied(path, number=1, engine=ENGINE, date=TODAY)
        append_applied(path, number=2, engine=ENGINE, date=TODAY)
        assert read_applied(path) == {1, 2}

    def test_record_shape_is_number_engine_date(self, tmp_path):
        path = log_path(tmp_path)
        append_applied(path, number=1, engine=ENGINE, date=TODAY)
        record = json.loads(path.read_text().split("\n")[0])
        assert record == {"number": 1, "engine": "0.5.0", "date": "2026-08-20"}

    def test_union_merged_duplicates_collapse(self, tmp_path):
        # git merge=union can duplicate whole lines and interleave orders.
        path = log_path(tmp_path)
        line1 = json.dumps({"number": 1, "engine": "0.4.0", "date": "2026-08-01"})
        line2 = json.dumps({"number": 2, "engine": "0.5.0", "date": "2026-08-20"})
        path.parent.mkdir(parents=True)
        path.write_text(f"{line2}\n{line1}\n{line1}\n")
        assert read_applied(path) == {1, 2}

    def test_malformed_json_is_loud(self, tmp_path):
        path = log_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("not json\n")
        with pytest.raises(MigrationError, match="unparseable"):
            read_applied(path)

    def test_a_torn_record_names_the_file_line_and_sanctioned_repair(self, tmp_path):
        # The state a crash mid-append leaves: a whole record, then a torn
        # trailing line. Sync has no report to render, so the error itself
        # carries the repair — a half-written line is not a record.
        path = log_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"number": 1, "engine": "0.5.0", "date": "2026-08-20"}\n{"number": 2,\n')
        with pytest.raises(MigrationError) as err:
            read_applied(path)
        message = str(err.value)
        assert "migrations.jsonl:2" in message  # the file AND the torn line
        assert "delete the torn line" in message
        assert "re-run sync" in message
        assert "idempotent migrations make a lost record harmless" in message

    def test_record_without_number_is_loud(self, tmp_path):
        path = log_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"engine": "0.5.0", "date": "2026-08-20"}\n')
        with pytest.raises(MigrationError, match="number"):
            read_applied(path)

    def test_non_object_record_is_loud(self, tmp_path):
        path = log_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("[1]\n")
        with pytest.raises(MigrationError, match="object"):
            read_applied(path)


class TestRunPending:
    def test_runs_in_order_and_logs_each(self, tmp_path):
        first, second = FakeMigration(1), FakeMigration(2)
        applied = run_pending(
            tmp_path,
            today=fixed_today,
            now=fixed_now,
            engine_version=ENGINE,
            migrations=[first, second],
        )
        assert [a.number for a in applied] == [1, 2]
        assert isinstance(applied[0], AppliedMigration)
        assert applied[0].report.actions == ["did 1"]
        assert (first.applied, second.applied) == (1, 1)
        assert read_applied(log_path(tmp_path)) == {1, 2}

    def test_applied_migrations_are_skipped(self, tmp_path):
        first, second = FakeMigration(1), FakeMigration(2)
        append_applied(log_path(tmp_path), number=1, engine="0.4.0", date=TODAY)
        applied = run_pending(
            tmp_path,
            today=fixed_today,
            now=fixed_now,
            engine_version=ENGINE,
            migrations=[first, second],
        )
        assert [a.number for a in applied] == [2]
        assert (first.applied, second.applied) == (0, 1)

    def test_rerun_is_a_noop_via_the_log(self, tmp_path):
        migration = FakeMigration(1)
        run_pending(
            tmp_path,
            today=fixed_today,
            now=fixed_now,
            engine_version=ENGINE,
            migrations=[migration],
        )
        applied = run_pending(
            tmp_path,
            today=fixed_today,
            now=fixed_now,
            engine_version=ENGINE,
            migrations=[migration],
        )
        assert applied == []
        assert migration.applied == 1

    def test_interrupted_run_resumes_where_it_failed(self, tmp_path):
        # The log record lands only after apply returns: an interruption
        # mid-migration re-runs that migration, never the finished ones.
        def boom(_root):
            raise RuntimeError("interrupted")

        first, second = FakeMigration(1), FakeMigration(2, apply_fn=boom)
        with pytest.raises(RuntimeError, match="interrupted"):
            run_pending(
                tmp_path,
                today=fixed_today,
                now=fixed_now,
                engine_version=ENGINE,
                migrations=[first, second],
            )
        assert read_applied(log_path(tmp_path)) == {1}

        recovered = FakeMigration(2)
        applied = run_pending(
            tmp_path,
            today=fixed_today,
            now=fixed_now,
            engine_version=ENGINE,
            migrations=[first, recovered],
        )
        assert [a.number for a in applied] == [2]
        assert first.applied == 1  # not re-run
        assert recovered.applied == 1

    def test_default_discovery_runs_shipped_migrations_on_a_fresh_instance(self, tmp_path):
        applied = run_pending(tmp_path, today=fixed_today, now=fixed_now, engine_version=ENGINE)
        assert [a.number for a in applied] == [1, 2, 3, 4, 5, 6, 7]
        assert read_applied(log_path(tmp_path)) == {1, 2, 3, 4, 5, 6, 7}
