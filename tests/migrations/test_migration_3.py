"""Tests for migration 3: cache/ gitignored on existing instances."""

import datetime

import pytest

from dex_engine.migrations import discover, run_pending
from dex_engine.migrations.migration_3 import build

TODAY = datetime.date(2026, 8, 20)


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, engine_version="0.1.0")


class TestApply:
    def test_appends_to_an_existing_gitignore(self, tmp_path, migration):
        (tmp_path / ".gitignore").write_text(".DS_Store\n.env\n")
        report = migration.apply(tmp_path)
        assert (tmp_path / ".gitignore").read_text() == ".DS_Store\n.env\ncache/\n"
        assert len(report.actions) == 1
        assert "cache/ appended" in report.actions[0]
        assert "instance-owned" in report.actions[0]  # the owner is told

    def test_creates_a_missing_gitignore(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert (tmp_path / ".gitignore").read_text() == "cache/\n"
        assert report.actions

    def test_missing_trailing_newline_never_glues_lines(self, tmp_path, migration):
        (tmp_path / ".gitignore").write_text(".env")  # no trailing newline
        migration.apply(tmp_path)
        assert (tmp_path / ".gitignore").read_text() == ".env\ncache/\n"

    @pytest.mark.parametrize("spelling", ["cache/", "cache", "/cache/", "/cache", "  cache/  "])
    def test_any_covering_spelling_is_a_no_op(self, tmp_path, migration, spelling):
        before = f".env\n{spelling}\nmedia-tmp/\n"
        (tmp_path / ".gitignore").write_text(before)
        report = migration.apply(tmp_path)
        assert (tmp_path / ".gitignore").read_text() == before
        assert report.actions == []
        assert report.skipped == []

    def test_lookalike_lines_do_not_count_as_coverage(self, tmp_path, migration):
        (tmp_path / ".gitignore").write_text("cache/audio/\n.cache\n")
        migration.apply(tmp_path)
        assert "\ncache/\n" in (tmp_path / ".gitignore").read_text()

    def test_second_apply_is_a_clean_noop(self, tmp_path, migration):
        migration.apply(tmp_path)
        snapshot = (tmp_path / ".gitignore").read_bytes()
        report = migration.apply(tmp_path)
        assert (tmp_path / ".gitignore").read_bytes() == snapshot
        assert report.actions == []

    def test_non_utf8_gitignore_is_skipped_for_the_owner(self, tmp_path, migration):
        (tmp_path / ".gitignore").write_bytes(b"\xff\xfe garbage")
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert any("by hand" in s.why for s in report.skipped)
        assert (tmp_path / ".gitignore").read_bytes() == b"\xff\xfe garbage"


class TestFramework:
    def test_discovered_as_number_three(self):
        migrations = discover(today=lambda: TODAY, engine_version="0.1.0")
        assert [m.number for m in migrations] == [1, 2, 3]

    def test_runs_via_the_pending_path_and_logs(self, tmp_path):
        applied = run_pending(
            tmp_path,
            today=lambda: TODAY,
            engine_version="0.1.0",
            migrations=[build(today=lambda: TODAY, engine_version="0.1.0")],
        )
        assert [one.number for one in applied] == [3]
        assert (tmp_path / ".gitignore").read_text() == "cache/\n"
        # Logged applied: a second pending run skips it.
        again = run_pending(
            tmp_path,
            today=lambda: TODAY,
            engine_version="0.1.0",
            migrations=[build(today=lambda: TODAY, engine_version="0.1.0")],
        )
        assert again == []
