"""Migration 2: the rerun seed for pre-rewrite web/x enrichments."""

import datetime
import json

import pytest

from dex_engine.migrations import MigrationError
from dex_engine.migrations.migration_2 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline.types import Kind, LedgerEntry, Status

ENGINE = "0.5.0"
TODAY = datetime.date(2026, 8, 20)


def fixed_today() -> datetime.date:
    return TODAY


@pytest.fixture
def migration():
    return build(today=fixed_today, engine_version=ENGINE)


class TestBuildGuard:
    def test_refuses_the_pre_rewrite_marker_version(self):
        # Seeds stamped 0.0.1 would satisfy this migration's own membership
        # test — a mis-built engine must be refused loudly, never seeded.
        with pytest.raises(MigrationError, match="pre-rewrite marker"):
            build(today=fixed_today, engine_version="0.0.1")

    def test_refuses_the_v_prefixed_marker_too(self):
        with pytest.raises(MigrationError, match="pre-rewrite marker"):
            build(today=fixed_today, engine_version="v0.0.1")

    def test_accepts_rewrite_versions(self):
        assert build(today=fixed_today, engine_version="0.1.0").number == 2


def entry(  # noqa: PLR0913 — one keyword per exercised schema slot
    unit_hash, *, kind, status=Status.DONE, engine="0.0.1", reason=None, error=None
):
    path = (
        f"enrichment/2026-05-01-item-a1b2c3/{kind.value}-{unit_hash[:6]}.md"
        if status is Status.DONE
        else None
    )
    return LedgerEntry(
        hash=unit_hash,
        url=f"https://a.test/{unit_hash}",
        item="2026-05-01-item-a1b2c3",
        kind=kind,
        status=status,
        engine=engine,
        date=datetime.date(2026, 5, 1),
        path=path,
        reason=reason,
        error=error,
    )


def write_entries(root, entries):
    path = root / "state" / "enrichment-ledger.jsonl"
    for one in entries:
        ledger.append(path, one)
    return path


class TestSeeding:
    def test_pre_rewrite_done_web_and_x_are_requeued(self, tmp_path, migration):
        path = write_entries(
            tmp_path,
            [
                entry("aaaaaaaaaa", kind=Kind.X),
                entry("bbbbbbbbbb", kind=Kind.WEB),
            ],
        )
        report = migration.apply(tmp_path)
        entries = ledger.load(path)
        for unit_hash in ("aaaaaaaaaa", "bbbbbbbbbb"):
            seed = entries[unit_hash]
            assert seed.status is Status.QUEUED
            assert seed.rerun is True
            assert seed.via == "migration-2"
            assert seed.engine == ENGINE
            assert seed.date == TODAY
            assert seed.path is None  # queued entries carry no outputs
        assert any(
            "1 x (thread walk-up), 1 web (link keeping)" in action for action in report.actions
        )

    def test_seed_reuses_the_url_keyed_work_unit(self, tmp_path, migration):
        original = entry("aaaaaaaaaa", kind=Kind.X)
        path = write_entries(tmp_path, [original])
        migration.apply(tmp_path)
        seed = ledger.load(path)["aaaaaaaaaa"]
        assert (seed.url, seed.item, seed.kind) == (original.url, original.item, original.kind)

    def test_other_kinds_and_statuses_stay_parked(self, tmp_path, migration):
        path = write_entries(
            tmp_path,
            [
                entry("cccccccccc", kind=Kind.YOUTUBE),  # neither fix applies
                entry("dddddddddd", kind=Kind.X, status=Status.DEAD),
                entry("eeeeeeeeee", kind=Kind.WEB, status=Status.MANUAL, reason="paywall"),
                # old error entries already retry under the new-engine rule
                entry("f0f0f0f0f0", kind=Kind.X, status=Status.ERROR, error="boom"),
            ],
        )
        report = migration.apply(tmp_path)
        entries = ledger.load(path)
        assert entries["cccccccccc"].status is Status.DONE
        assert entries["dddddddddd"].status is Status.DEAD
        assert entries["eeeeeeeeee"].status is Status.MANUAL
        assert entries["f0f0f0f0f0"].status is Status.ERROR
        assert report.actions == []

    def test_post_rewrite_done_entries_are_not_seeded(self, tmp_path, migration):
        # Entries written by the rewritten engine already have both fixes.
        path = write_entries(tmp_path, [entry("aaaaaaaaaa", kind=Kind.WEB, engine="0.5.0")])
        migration.apply(tmp_path)
        assert ledger.load(path)["aaaaaaaaaa"].status is Status.DONE

    def test_membership_reads_the_latest_line_per_hash(self, tmp_path, migration):
        # A hash whose done line was superseded is not done any more.
        path = write_entries(
            tmp_path,
            [
                entry("aaaaaaaaaa", kind=Kind.WEB),
                entry("aaaaaaaaaa", kind=Kind.WEB, status=Status.DEAD, engine="0.5.0"),
            ],
        )
        migration.apply(tmp_path)
        assert ledger.load(path)["aaaaaaaaaa"].status is Status.DEAD

    def test_unparseable_engine_skipped_with_why(self, tmp_path, migration):
        record = {
            "hash": "abcdefabcd",
            "url": "https://a.test/weird",
            "item": "2026-05-01-item-a1b2c3",
            "kind": "web",
            "status": "done",
            "engine": "hand-healed",
            "date": "2026-05-01",
        }
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record) + "\n")
        report = migration.apply(tmp_path)
        assert ledger.load(path)["abcdefabcd"].status is Status.DONE
        assert any("unparseable engine" in s.why for s in report.skipped)

    def test_line_migration_1_left_untranslated_does_not_block_seeding(self, tmp_path, migration):
        good = entry("aaaaaaaaaa", kind=Kind.X)
        path = write_entries(tmp_path, [good])
        with path.open("a", encoding="utf-8") as f:
            f.write('{"hash": "bad", "status": "nocaptions"}\n')
        report = migration.apply(tmp_path)
        assert any("unreadable under the current schema" in s.why for s in report.skipped)
        lines = [line for line in path.read_text().split("\n") if line.strip()]
        seeds = [line for line in lines if '"migration-2"' in line]
        assert len(seeds) == 1

    def test_missing_ledger_is_tolerated(self, tmp_path, migration):
        # Un-pulled repos are a supported input.
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []


class TestIdempotency:
    def test_second_apply_seeds_nothing(self, tmp_path, migration):
        path = write_entries(tmp_path, [entry("aaaaaaaaaa", kind=Kind.X)])
        migration.apply(tmp_path)
        first = path.read_text()
        second_report = migration.apply(tmp_path)
        assert path.read_text() == first
        assert second_report.actions == []
