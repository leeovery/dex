"""Tests for migration 7: restore article bodies a wayback rescue overwrote."""

import datetime
import subprocess

import pytest

from dex_engine.migrations.migration_7 import build

TODAY = datetime.date(2026, 8, 28)
NOW = datetime.datetime(2026, 8, 28, 9, 0, 0, 500000, tzinfo=datetime.UTC)

ITEM = "2026-08-27-example-55ad7b"
URL = "https://example.test/post"

DIRECT_BODY = "the complete article body, all of it, " * 40
PREVIEW_BODY = "a member-only preview of the article"

DIRECT = f'---\nurl: "{URL}"\nfetched: 2026-08-20\ntitle: "The Article"\n---\n\n{DIRECT_BODY}\n'
CLOBBERED = (
    f'---\nurl: "{URL}"\nfetched: 2026-08-27\nvia: wayback\n'
    f'snapshot: "https://web.archive.org/web/1/x"\n---\n\n{PREVIEW_BODY}\n'
)


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.0")


def git(root, *args):
    subprocess.run(  # noqa: S603 — test-built args, no shell
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t.test", *args],  # noqa: S607 — PATH resolution is the dependency contract
        check=True,
        capture_output=True,
    )


def repo(root):
    git(root, "init", "-q")


def enrichment_file(root, text, name="web-55ad7b.md"):
    path = root / "enrichment" / ITEM / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def commit(root, message):
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


class TestApply:
    def test_a_committed_clobber_restores_the_direct_fetch(self, tmp_path, migration):
        # The field defect: a refused live fetch fell back to a snapshot
        # holding only the preview, which replaced the complete stored
        # body — committed by the run that reported it as new material.
        repo(tmp_path)
        file = enrichment_file(tmp_path, DIRECT)
        commit(tmp_path, "direct fetch")
        file.write_text(CLOBBERED)
        commit(tmp_path, "the clobbering rerun")
        report = migration.apply(tmp_path)
        assert file.read_text() == DIRECT
        assert len(report.actions) == 1
        assert "restored" in report.actions[0]
        assert str(len(PREVIEW_BODY)) in report.actions[0]

    def test_an_uncommitted_clobber_restores_too(self, tmp_path, migration):
        repo(tmp_path)
        file = enrichment_file(tmp_path, DIRECT)
        commit(tmp_path, "direct fetch")
        file.write_text(CLOBBERED)  # the run wrote but nothing committed yet
        migration.apply(tmp_path)
        assert file.read_text() == DIRECT

    def test_a_wayback_born_file_is_left_alone_without_comment(self, tmp_path, migration):
        # The ordinary rescue: wayback saved a page nothing else could
        # reach, and there is no larger ancestor to prefer.
        repo(tmp_path)
        file = enrichment_file(tmp_path, CLOBBERED)
        commit(tmp_path, "born of its rescue")
        report = migration.apply(tmp_path)
        assert file.read_text() == CLOBBERED
        assert report.actions == []
        assert report.skipped == []

    def test_a_legitimate_shrink_is_not_a_clobber(self, tmp_path, migration):
        # The nearest direct fetch does not dwarf the rescue — the page
        # really was that size — so the rescue stands even when an older,
        # larger revision exists further back.
        repo(tmp_path)
        file = enrichment_file(tmp_path, DIRECT)
        commit(tmp_path, "the long original")
        modest = f'---\nurl: "{URL}"\n---\n\n{"trimmed prose " * 4}\n'
        file.write_text(modest)
        commit(tmp_path, "the page got shorter for real")
        rescued = f'---\nurl: "{URL}"\nvia: wayback\n---\n\n{"trimmed prose " * 3}\n'
        file.write_text(rescued)
        commit(tmp_path, "rescued at its real size")
        report = migration.apply(tmp_path)
        assert file.read_text() == rescued
        assert report.actions == []

    def test_a_direct_fetch_file_is_never_a_candidate(self, tmp_path, migration):
        repo(tmp_path)
        file = enrichment_file(tmp_path, DIRECT)
        commit(tmp_path, "direct fetch")
        report = migration.apply(tmp_path)
        assert file.read_text() == DIRECT
        assert report.actions == []

    def test_no_git_answers_with_one_skip_and_touches_nothing(self, tmp_path, migration):
        file = enrichment_file(tmp_path, CLOBBERED)  # no repo around it
        report = migration.apply(tmp_path)
        assert file.read_text() == CLOBBERED
        assert report.actions == []
        assert len(report.skipped) == 1
        assert "history is the only place" in report.skipped[0].why

    def test_second_apply_is_a_clean_noop(self, tmp_path, migration):
        # The restore removed the wayback stamp, so the file is no longer
        # a candidate — the idempotency the framework requires.
        repo(tmp_path)
        file = enrichment_file(tmp_path, DIRECT)
        commit(tmp_path, "direct fetch")
        file.write_text(CLOBBERED)
        commit(tmp_path, "the clobbering rerun")
        migration.apply(tmp_path)
        report = migration.apply(tmp_path)
        assert file.read_text() == DIRECT
        assert report.actions == []

    def test_an_undecodable_revision_does_not_stop_the_walk(self, tmp_path, migration):
        # A revision git cannot hand back as text is stepped past — the
        # direct fetch behind it still answers.
        repo(tmp_path)
        file = enrichment_file(tmp_path, DIRECT)
        commit(tmp_path, "direct fetch")
        file.write_bytes(b"\xff\xfe not text at all")
        commit(tmp_path, "a torn write")
        file.write_text(CLOBBERED)
        commit(tmp_path, "the clobbering rerun")
        migration.apply(tmp_path)
        assert file.read_text() == DIRECT

    def test_exactly_twice_the_rescue_is_already_a_clobber(self, tmp_path, migration):
        # The guard's own boundary: "at least twice" includes twice.
        repo(tmp_path)
        stored = f'---\nurl: "{URL}"\n---\n\n{"y" * 100}\n'
        file = enrichment_file(tmp_path, stored)
        commit(tmp_path, "direct fetch")
        file.write_text(f'---\nurl: "{URL}"\nvia: wayback\n---\n\n{"x" * 50}\n')
        commit(tmp_path, "the clobbering rerun")
        migration.apply(tmp_path)
        assert file.read_text() == stored

    def test_a_rule_inside_the_stored_body_is_not_a_second_fence(self, tmp_path, migration):
        # Markdown bodies carry --- horizontal rules; the body is measured
        # from the FIRST fence, exactly as read_enrichment reads it, or a
        # long article ending in a short section reads as short.
        repo(tmp_path)
        ruled = f'---\nurl: "{URL}"\n---\n\n{"long prose " * 60}\n\n---\n\ntail\n'
        file = enrichment_file(tmp_path, ruled)
        commit(tmp_path, "direct fetch with a rule in it")
        file.write_text(CLOBBERED)
        commit(tmp_path, "the clobbering rerun")
        migration.apply(tmp_path)
        assert file.read_text() == ruled

    def test_an_unreadable_candidate_is_named_not_fatal(self, tmp_path, migration):
        repo(tmp_path)
        bad = tmp_path / "enrichment" / ITEM / "garbled.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"\xff\xfe broken bytes")
        good = enrichment_file(tmp_path, DIRECT)
        commit(tmp_path, "state")
        good.write_text(CLOBBERED)
        commit(tmp_path, "clobber")
        report = migration.apply(tmp_path)
        assert good.read_text() == DIRECT  # the readable file still repaired
        assert any("garbled.md" in s.what for s in report.skipped)

    def test_no_enrichment_directory_is_an_empty_report(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []
