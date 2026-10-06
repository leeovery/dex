"""Tests for migration 21: give the units an earlier attempt cap gave up on a fresh start."""

import dataclasses
import datetime

import pytest

from dex_engine.drivers.ytdlp import ProbeError
from dex_engine.migrations import run_pending
from dex_engine.migrations.migration_21 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.run import BLOCKED_BACKOFF, MAX_BLOCKED_ATTEMPTS
from dex_engine.pipeline.types import (
    Format,
    Instance,
    Job,
    Kind,
    LedgerEntry,
    MigrationReport,
    Need,
    Refused,
    Status,
)
from dex_engine.pipeline.urls import work_hash
from tests.conftest import FakeDriver
from tests.pipeline.test_run import (
    ITEM,
    NOW,
    TODAY,
    URL,
    entry_for,
    make_ctx,
    retry_clock,
    write_item,
)
from tests.pipeline.test_transcribe import VIDEO_URL, FakeDownload, transcribe_ctx

# The unit's life under the old cap, then this migration's apply, then the
# runs (at NOW) that drain what it re-queued.
BORN_AT = datetime.datetime(2026, 8, 18, 6, tzinfo=datetime.UTC)
GIVEN_UP_AT = datetime.datetime(2026, 8, 18, 11, tzinfo=datetime.UTC)
APPLIED = datetime.datetime(2026, 8, 20, 8, 0, 0, 500000, tzinfo=datetime.UTC)

OLD_CAP = 5
OLD_ENGINE = "0.2.10"
ENGINE = "0.2.11"
REFUSAL = "HTTP 429"
AUDIO_REFUSAL = "audio acquisition failed: HTTP Error 429: Too Many Requests"

RENAMED = "2026-08-19-renamed-55ad7b"
POST_URL = "https://x.com/i/status/2059522098754629738"
PICTURE_URL = "https://pbs.example.test/media/chart.png"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: APPLIED, engine_version=ENGINE)


def unit(url=URL, *, kind=Kind.WEB, item=ITEM, **fields) -> LedgerEntry:
    """The unit's birth line, as its capture seeded it."""
    born = LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=item,
        kind=kind,
        status=Status.QUEUED,
        engine=OLD_ENGINE,
        date=datetime.date(2026, 8, 18),
        at=BORN_AT,
    )
    return dataclasses.replace(born, **fields)


def blocked(of: LedgerEntry, attempts: int, *, refusal=REFUSAL, needs=None) -> LedgerEntry:
    """A failed try as the old engine recorded it, an hour after the one before."""
    return dataclasses.replace(
        of,
        status=Status.BLOCKED,
        attempts=attempts,
        needs=needs,
        reason=refusal,
        at=BORN_AT + datetime.timedelta(hours=attempts),
    )


def given_up(of: LedgerEntry, *, attempts=OLD_CAP, refusal=REFUSAL, **fields) -> LedgerEntry:
    """The line the old cap gave the unit up with, its last try spent."""
    written = {
        "status": Status.MANUAL,
        "needs": None,
        "attempts": None,
        "reason": f"still blocked after {attempts} attempts — {refusal}",
        "at": GIVEN_UP_AT,
    }
    return dataclasses.replace(of, **(written | fields))


def history(of: LedgerEntry, *, refusal=REFUSAL, needs=None) -> list[LedgerEntry]:
    """The unit's whole life under the old cap: born, refused, and given up at the last."""
    tries = [blocked(of, n, refusal=refusal, needs=needs) for n in range(1, OLD_CAP)]
    return [of, *tries, given_up(of, refusal=refusal)]


def write_ledger(instance: Instance, *entries: LedgerEntry) -> None:
    instance.ledger_path.write_text("".join(ledger.to_line(e) + "\n" for e in entries))


def live(instance: Instance) -> dict[str, LedgerEntry]:
    # Read as of a month on, so every line a test writes is in the past.
    later = APPLIED + datetime.timedelta(days=30)
    return ledger.latest_readable(instance.ledger_path, now=lambda: later)


def line_count(instance: Instance) -> int:
    return len(instance.ledger_path.read_text().splitlines())


def given_up_page(instance: Instance, **fields) -> LedgerEntry:
    """A page whose every try was refused, given up under its live item."""
    write_item(instance)
    born = unit()
    write_ledger(instance, *history(born)[:-1], given_up(born, **fields))
    return born


class TestMembers:
    def test_a_unit_the_old_cap_gave_up_on_is_queued_afresh(self, instance, migration):
        born = given_up_page(instance)
        migration.apply(instance.root)
        assert live(instance)[born.hash] == LedgerEntry(
            hash=born.hash,
            url=URL,
            item=ITEM,
            kind=Kind.WEB,
            status=Status.QUEUED,
            engine=ENGINE,
            date=TODAY,
            at=APPLIED,
            via="migration-21",
        )

    @pytest.mark.parametrize("attempts", [1, OLD_CAP, MAX_BLOCKED_ATTEMPTS - 1])
    def test_every_escalation_short_of_the_cap_is_a_member(self, instance, migration, attempts):
        born = given_up_page(instance, attempts=attempts)
        migration.apply(instance.root)
        assert live(instance)[born.hash].status is Status.QUEUED

    def test_the_line_keeps_the_units_identity_and_lineage(self, instance, migration):
        parent = work_hash(POST_URL)
        write_item(instance, urls=[POST_URL])
        born = unit(
            "https://docs.example.test/guide.pdf",
            kind=Kind.FILE,
            format=Format.PDF,
            parent=parent,
            depth=1,
            http_shared=True,
            via="harvest",
        )
        write_ledger(instance, *history(born))
        migration.apply(instance.root)
        fresh = live(instance)[born.hash]
        assert (fresh.kind, fresh.format) == (Kind.FILE, Format.PDF)
        assert (fresh.parent, fresh.depth, fresh.http_shared) == (parent, 1, True)
        assert (fresh.via, fresh.rerun) == ("migration-21", False)

    def test_a_media_download_keeps_its_job(self, instance, migration):
        write_item(instance, urls=[POST_URL])
        born = unit(PICTURE_URL, kind=Kind.X, job=Job.MEDIA, parent=work_hash(POST_URL), depth=1)
        write_ledger(instance, *history(born, refusal="media URL returned an empty response body"))
        migration.apply(instance.root)
        fresh = live(instance)[born.hash]
        assert (fresh.status, fresh.job, fresh.needs) == (Status.QUEUED, Job.MEDIA, None)

    def test_a_unit_the_transcribe_drain_gave_up_on_waits_for_it_again(self, instance, migration):
        write_item(instance, urls=[VIDEO_URL])
        born = unit(VIDEO_URL, kind=Kind.YOUTUBE)
        parked = dataclasses.replace(
            born, status=Status.WAITING, needs=Need.TRANSCRIBE, reason="no captions available"
        )
        write_ledger(instance, *history(parked, refusal=AUDIO_REFUSAL, needs=Need.TRANSCRIBE))
        migration.apply(instance.root)
        fresh = live(instance)[born.hash]
        assert (fresh.status, fresh.needs) == (Status.WAITING, Need.TRANSCRIBE)
        assert (fresh.attempts, fresh.reason, fresh.via) == (None, None, "migration-21")

    def test_a_compacted_ledger_still_tells_a_transcribe_job_by_its_reason(
        self, instance, migration
    ):
        # A compact keeps the live line alone: every blocked line that
        # carried `needs` is gone.
        write_item(instance, urls=[VIDEO_URL])
        born = unit(VIDEO_URL, kind=Kind.YOUTUBE)
        write_ledger(instance, given_up(born, refusal=AUDIO_REFUSAL))
        migration.apply(instance.root)
        assert live(instance)[born.hash].needs is Need.TRANSCRIBE

    def test_a_refusal_naming_the_audio_further_in_is_a_fetch(self, instance, migration):
        born = given_up_page(instance, refusal=f"HTTP 403 ({AUDIO_REFUSAL})")
        migration.apply(instance.root)
        fresh = live(instance)[born.hash]
        assert (fresh.status, fresh.needs) == (Status.QUEUED, None)


class TestNonMembers:
    @pytest.mark.parametrize(
        "fields",
        [
            # Given up under the cap as it stands: every attempt was spent.
            {"attempts": MAX_BLOCKED_ATTEMPTS},
            {"attempts": MAX_BLOCKED_ATTEMPTS + 1},
            {"reason": "thin-extraction"},
            {"reason": "HTTP 403"},
            {"reason": f"requeue by hand: still blocked after {OLD_CAP} attempts — {REFUSAL}"},
            {"status": Status.SKIPPED},
            {"status": Status.DEAD},
            {"rerun": True},
        ],
    )
    def test_is_left_as_it_stands(self, instance, migration, fields):
        born = given_up_page(instance, **fields)
        before = live(instance)[born.hash]
        report = migration.apply(instance.root)
        assert live(instance)[born.hash] == before
        assert line_count(instance) == OLD_CAP + 1
        assert report == MigrationReport()

    def test_a_unit_that_landed_before_it_was_given_up_is_left_alone(self, instance, migration):
        # Its stored copy stands, and nothing re-fetches it.
        write_item(instance)
        born = unit()
        landing = dataclasses.replace(
            born,
            status=Status.DONE,
            path=f"enrichment/{ITEM}/web-{born.hash[:6]}.md",
            at=BORN_AT + datetime.timedelta(minutes=30),
        )
        write_ledger(instance, born, landing, *history(born)[1:])
        report = migration.apply(instance.root)
        assert live(instance)[born.hash].status is Status.MANUAL
        assert report == MigrationReport()

    def test_a_landing_a_union_merge_wrote_below_still_counts(self, instance, migration):
        write_item(instance)
        born = unit()
        landing = dataclasses.replace(
            born, status=Status.DONE, path=f"enrichment/{ITEM}/web-{born.hash[:6]}.md"
        )
        write_ledger(instance, *history(born), landing)
        migration.apply(instance.root)
        assert live(instance)[born.hash].status is Status.MANUAL


class TestOwner:
    def test_a_renamed_item_takes_the_line(self, instance, migration):
        write_item(instance, item_id=RENAMED)
        born = unit()
        write_ledger(instance, *history(born))
        report = migration.apply(instance.root)
        assert live(instance)[born.hash].item == RENAMED
        assert report.skipped == []

    def test_the_stored_item_answers_while_its_file_exists(self, instance, migration):
        write_item(instance)
        write_item(instance, item_id=RENAMED)
        born = unit()
        write_ledger(instance, *history(born))
        migration.apply(instance.root)
        assert live(instance)[born.hash].item == ITEM

    def test_a_download_follows_its_post_to_the_renamed_item(self, instance, migration):
        # Never listed in frontmatter, it is claimed through its parent.
        write_item(instance, item_id=RENAMED, urls=[POST_URL])
        post = unit(POST_URL, kind=Kind.X, status=Status.DONE, path=f"enrichment/{ITEM}/x.md")
        born = unit(PICTURE_URL, kind=Kind.X, job=Job.MEDIA, parent=post.hash, depth=1)
        write_ledger(instance, post, *history(born))
        migration.apply(instance.root)
        assert live(instance)[born.hash].item == RENAMED


class TestUnclaimed:
    def test_a_unit_no_live_item_claims_is_skipped_with_why(self, instance, migration):
        born = unit()
        write_ledger(instance, *history(born))
        report = migration.apply(instance.root)
        assert live(instance)[born.hash].status is Status.MANUAL
        (skip,) = report.skipped
        assert skip.what == f"given-up unit {URL} of {ITEM}"
        assert skip.why == (
            f"corpus/2026/{ITEM}.md does not exist, no state/exclusions.tsv record names the "
            f"item, and no live corpus item claims {URL} — nothing owns the unit, so it stays "
            "manual as it stands"
        )
        assert report.actions == []

    @pytest.mark.parametrize(
        ("stated", "said"),
        [("spam", "spam"), ("spam\tsaid twice", "spam\tsaid twice"), ("", "no reason recorded")],
    )
    def test_an_excluded_item_names_its_exclusion(self, instance, migration, stated, said):
        born = unit()
        write_ledger(instance, *history(born))
        (instance.state_dir / "exclusions.tsv").write_text(
            f"2026-01-01-other-000000\tdup\n\n {ITEM} \t{stated} \n"
        )
        report = migration.apply(instance.root)
        (skip,) = report.skipped
        assert skip.why == (
            f"corpus/2026/{ITEM}.md is gone and the item is excluded on the record "
            f"(state/exclusions.tsv: {said}) — the unit stays manual as it stands"
        )

    def test_a_claimed_unit_is_requeued_beside_an_unclaimed_one(self, instance, migration):
        write_item(instance)
        orphan = unit("https://example.test/orphan", item=RENAMED)
        born = unit()
        write_ledger(instance, *history(orphan), *history(born))
        report = migration.apply(instance.root)
        assert live(instance)[born.hash].status is Status.QUEUED
        assert [skip.what for skip in report.skipped] == [
            f"given-up unit https://example.test/orphan of {RENAMED}"
        ]
        assert len(report.actions) == 1


class TestTolerantRead:
    def test_an_unreadable_line_is_named_and_the_rest_requeued(self, instance, migration):
        born = given_up_page(instance)
        instance.ledger_path.write_text('{"hash": "torn\n\n' + instance.ledger_path.read_text())
        report = migration.apply(instance.root)
        assert live(instance)[born.hash].status is Status.QUEUED
        (skip,) = report.skipped
        assert skip.what == "ledger line 1"
        assert skip.why == "does not parse — left untouched; migration 21 skipped it"

    def test_an_unreadable_line_is_named_when_nothing_is_requeued(self, instance, migration):
        instance.ledger_path.write_text("\nnot json\n")
        report = migration.apply(instance.root)
        assert [skip.what for skip in report.skipped] == ["ledger line 2"]
        assert report.actions == []

    def test_a_try_a_union_merge_wrote_below_the_given_up_line_is_not_live(
        self, instance, migration
    ):
        # And every unit after the ones it lost to is still read.
        after = "https://example.test/after"
        write_item(instance, urls=[URL, after])
        born = unit()
        *tries, last = history(born)
        write_ledger(instance, last, *tries, *history(unit(after)))
        migration.apply(instance.root)
        lines = live(instance)
        assert lines[born.hash].status is Status.QUEUED
        assert lines[work_hash(after)].status is Status.QUEUED

    def test_a_newer_line_written_above_the_given_up_line_is_live(self, instance, migration):
        write_item(instance)
        born = unit()
        healed = dataclasses.replace(born, at=GIVEN_UP_AT + datetime.timedelta(hours=1))
        write_ledger(instance, healed, *history(born))
        report = migration.apply(instance.root)
        assert report == MigrationReport()

    def test_unstamped_lines_resolve_by_position(self, instance, migration):
        write_item(instance)
        born = unit(at=None)
        tries = [dataclasses.replace(line, at=None) for line in history(born)]
        write_ledger(instance, *tries)
        migration.apply(instance.root)
        assert live(instance)[born.hash].status is Status.QUEUED


class TestReport:
    def test_counts_the_units_requeued_by_what_they_are(self, instance, migration):
        pages = ["https://example.test/a", "https://example.test/b"]
        write_item(instance, urls=[*pages, VIDEO_URL, POST_URL])
        lines: list[LedgerEntry] = []
        for page in pages:
            lines += history(unit(page))
        lines += history(unit(VIDEO_URL, kind=Kind.YOUTUBE), refusal=AUDIO_REFUSAL)
        lines += history(
            unit(PICTURE_URL, kind=Kind.X, job=Job.MEDIA, parent=work_hash(POST_URL), depth=1)
        )
        write_ledger(instance, *lines)
        report = migration.apply(instance.root)
        assert report == MigrationReport(
            actions=[
                (
                    "re-queued 4 unit(s) an earlier engine gave up on as still blocked, none of "
                    "which ever landed (1 media, 2 web, 1 youtube): the next run that can take "
                    "each tries it again, and a refusal now waits out a backoff before the next "
                    f"of its {MAX_BLOCKED_ATTEMPTS} attempts"
                )
            ]
        )

    def test_nothing_given_up_reports_nothing(self, instance, migration):
        write_item(instance)
        write_ledger(instance, unit())
        assert migration.apply(instance.root) == MigrationReport()
        assert line_count(instance) == 1

    def test_an_instance_with_no_ledger_reports_nothing(self, tmp_path, migration):
        assert migration.apply(tmp_path) == MigrationReport()


class TestIdempotent:
    def test_a_second_apply_writes_nothing(self, instance, migration):
        given_up_page(instance)
        migration.apply(instance.root)
        written = instance.ledger_path.read_text()
        assert migration.apply(instance.root) == MigrationReport()
        assert instance.ledger_path.read_text() == written


class TestShipped:
    def test_sync_runs_it_after_twenty(self, instance):
        born = given_up_page(instance)
        applied = run_pending(
            instance.root, today=lambda: TODAY, now=lambda: APPLIED, engine_version=ENGINE
        )
        numbers = [migration.number for migration in applied]
        assert numbers.index(21) == numbers.index(20) + 1
        assert live(instance)[born.hash].via == "migration-21"


class TestTheNextRun:
    """A re-queued unit is ordinary work again, and a refusal takes the lifecycle as it stands."""

    def test_fetches_the_unit_and_lands_it(self, instance, migration):
        given_up_page(instance)
        migration.apply(instance.root)
        driver = FakeDriver()
        ctx = make_ctx(instance, driver)
        run_mod.run(ctx)
        assert [fetched.url for fetched in driver.fetched] == [URL]
        assert entry_for(ctx).status is Status.DONE

    def test_a_refusal_starts_the_attempts_and_the_backoff_afresh(self, instance, migration):
        given_up_page(instance)
        migration.apply(instance.root)
        driver = FakeDriver(fetch_fn=lambda _unit: Refused(evidence=REFUSAL))
        report = run_mod.run(make_ctx(instance, driver))
        assert (
            f"- **{ITEM}** · `blocked` · attempt 1 of {MAX_BLOCKED_ATTEMPTS} · "
            "next try after 2026-08-20 10:30 UTC"
        ) in report
        early = NOW + BLOCKED_BACKOFF[0] - datetime.timedelta(seconds=1)
        run_mod.run(make_ctx(instance, driver, now=lambda: early))
        assert len(driver.fetched) == 1

    def test_a_unit_given_up_under_the_cap_is_never_requeued_again(self, instance, migration):
        given_up_page(instance)
        migration.apply(instance.root)
        driver = FakeDriver(fetch_fn=lambda _unit: Refused(evidence=REFUSAL))
        for failed in range(MAX_BLOCKED_ATTEMPTS):
            run_mod.run(make_ctx(instance, driver, now=retry_clock(failed)))
        assert entry_for(make_ctx(instance, driver)).status is Status.MANUAL
        last = retry_clock(MAX_BLOCKED_ATTEMPTS - 1)
        again = build(today=lambda: TODAY, now=last, engine_version=ENGINE)
        assert again.apply(instance.root) == MigrationReport()

    def test_a_transcribe_job_goes_back_through_the_transcribe_drain(self, instance, migration):
        write_item(instance, urls=[VIDEO_URL])
        born = unit(VIDEO_URL, kind=Kind.YOUTUBE)
        write_ledger(instance, given_up(born, refusal=AUDIO_REFUSAL))
        migration.apply(instance.root)
        download = FakeDownload()
        ctx = transcribe_ctx(instance, download=download)
        run_mod.run(ctx)
        assert download.calls == [(VIDEO_URL, born.hash)]
        assert entry_for(ctx, VIDEO_URL).status is Status.DONE

    def test_a_transcribe_job_refused_again_backs_off_in_that_drain(self, instance, migration):
        write_item(instance, urls=[VIDEO_URL])
        born = unit(VIDEO_URL, kind=Kind.YOUTUBE)
        write_ledger(instance, given_up(born, refusal=AUDIO_REFUSAL))
        migration.apply(instance.root)
        failing = FakeDownload(raise_=ProbeError("HTTP Error 429: Too Many Requests"))
        ctx = transcribe_ctx(instance, download=failing)
        run_mod.run(ctx)
        entry = entry_for(ctx, VIDEO_URL)
        assert (entry.status, entry.attempts, entry.needs) == (Status.BLOCKED, 1, Need.TRANSCRIBE)
