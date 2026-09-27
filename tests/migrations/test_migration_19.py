"""Tests for migration 19: cancel the heal reruns still pending over a stored copy that stands."""

import dataclasses
import datetime
import json

import pytest

from dex_engine.migrations import run_pending
from dex_engine.migrations.migration_19 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.types import Format, Instance, Job, Kind, LedgerEntry, Need, Status
from dex_engine.pipeline.urls import work_hash

TODAY = datetime.date(2026, 9, 26)
NOW = datetime.datetime(2026, 9, 26, 9, 0, 0, 500000, tzinfo=datetime.UTC)
LANDED = datetime.date(2026, 8, 14)
LANDED_AT = datetime.datetime(2026, 8, 14, 10, tzinfo=datetime.UTC)
SEEDED_AT = datetime.datetime(2026, 9, 25, 10, tzinfo=datetime.UTC)
OLD_ENGINE = "0.1.9"

ITEM = "2026-08-14-docs-page-abc123"
RENAMED = "2026-08-14-tables-guide-abc123"
PAGE_URL = "https://docs.example.test/guide/tables"
POST_URL = "https://x.com/i/status/2059522098754629738"
ATTACHMENT_URL = "https://github.com/user-attachments/files/31802150/Architecture.pdf"
REPO_URL = "https://github.com/example/tool/tree/main/docs"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.3")


def write_ledger(root, *entries):
    path = root / "state" / "enrichment-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(ledger.to_line(e) + "\n" for e in entries))
    return path


def write_corpus_item(root, item_id=ITEM, urls=(PAGE_URL,)):
    path = root / "corpus" / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    listing = "".join(f"  - {url}\n" for url in urls)
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: manual\nchannel: inbox\nshared_by: alex\ndate: 2026-08-14\n"
        f"urls:\n{listing}"
        "kinds: [web]\nstatus: raw\nenrichment: []\n---\n**alex**: note\n",
        encoding="utf-8",
    )
    return path


def landing(url=PAGE_URL, *, kind=Kind.WEB, item=ITEM, **fields):
    unit_hash = work_hash(url)
    landed = LedgerEntry(
        hash=unit_hash,
        url=url,
        item=item,
        kind=kind,
        status=Status.DONE,
        engine=OLD_ENGINE,
        date=LANDED,
        at=LANDED_AT,
        path=f"enrichment/{item}/{kind.value}-{unit_hash[:6]}.md",
        title="Tables in the guide",
    )
    return dataclasses.replace(landed, **fields)


def seed(of: LedgerEntry, *, via="migration-18", status=Status.QUEUED, **fields):
    """The heal rerun ``via`` queued over ``of``, as migrations 15 to 18 wrote them."""
    written = {
        "status": status,
        "engine": "0.2.2",
        "date": datetime.date(2026, 9, 25),
        "at": SEEDED_AT,
        "via": via,
        "rerun": True,
        "path": None,
        "title": None,
        "attempts": 1 if status is Status.BLOCKED else None,
        "error": "gh api returned HTTP 502" if status is Status.ERROR else None,
        "reason": "HTTP 406" if status is Status.BLOCKED else None,
    }
    return dataclasses.replace(of, **(written | fields))


def stand(root, entry, body="# Tables\n\n| a | b |\n"):
    """The landing's file, where it recorded it."""
    file = root / entry.path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(body, encoding="utf-8")
    return file


def live_lines(root):
    # Read as of a month on, so every line a test writes is in the past.
    path = root / "state" / "enrichment-ledger.jsonl"
    return ledger.latest_readable(path, now=lambda: NOW + datetime.timedelta(days=30))


def line_count(root):
    return len((root / "state" / "enrichment-ledger.jsonl").read_text().splitlines())


def landed_with_rerun(root, url=PAGE_URL, *, kind=Kind.WEB, via="migration-18", **seeded):
    write_corpus_item(root, urls=(url,))
    first = landing(url, kind=kind)
    stand(root, first)
    write_ledger(root, first, seed(first, via=via, **seeded))
    return first


class TestMembers:
    def test_a_queued_rerun_is_recorded_done_at_its_landing_as_it_landed(self, tmp_path, migration):
        first = landed_with_rerun(tmp_path)
        migration.apply(tmp_path)
        cancel = live_lines(tmp_path)[first.hash]
        assert cancel == dataclasses.replace(first, at=NOW, via="migration-19", rerun=False)
        # The day and the engine of the content, not of this apply: the
        # item's enrichment did not change, so no digest is owed.
        assert (cancel.date, cancel.engine) == (LANDED, OLD_ENGINE)

    @pytest.mark.parametrize(
        "via", ["migration-15", "migration-16", "migration-17", "migration-18"]
    )
    def test_every_heal_migrations_rerun_is_a_member(self, tmp_path, migration, via):
        first = landed_with_rerun(tmp_path, via=via)
        migration.apply(tmp_path)
        assert live_lines(tmp_path)[first.hash].status is Status.DONE

    @pytest.mark.parametrize("status", [Status.QUEUED, Status.BLOCKED, Status.ERROR])
    def test_a_rerun_queued_blocked_or_in_error_is_a_member(self, tmp_path, migration, status):
        first = landed_with_rerun(tmp_path, status=status)
        migration.apply(tmp_path)
        cancel = live_lines(tmp_path)[first.hash]
        assert (cancel.status, cancel.path) == (Status.DONE, first.path)
        assert (cancel.attempts, cancel.error, cancel.reason) == (None, None, None)

    def test_the_line_carries_the_landings_fields_not_the_seeds(self, tmp_path, migration):
        parent = work_hash("https://x.com/i/status/1")
        first = landing(
            "https://docs.example.test/guide.pdf",
            kind=Kind.FILE,
            format=Format.PDF,
            parent=parent,
            depth=1,
            http_shared=True,
            via="harvest",
            title="The PDF",
        )
        write_corpus_item(tmp_path)
        stand(tmp_path, first)
        write_ledger(tmp_path, first, seed(first, parent=None, depth=None, http_shared=False))
        migration.apply(tmp_path)
        cancel = live_lines(tmp_path)[first.hash]
        assert (cancel.format, cancel.parent, cancel.depth) == (Format.PDF, parent, 1)
        assert cancel.http_shared
        assert cancel.title == "The PDF"
        assert cancel.via == "migration-19"

    def test_a_media_landing_keeps_its_job(self, tmp_path, migration):
        url = "https://pbs.example.test/media/chart.png"
        first = landing(
            url,
            kind=Kind.X,
            job=Job.MEDIA,
            parent=work_hash(POST_URL),
            depth=1,
            path=f"enrichment/{ITEM}/media-0.png",
            title=None,
        )
        write_corpus_item(tmp_path, urls=(POST_URL,))
        stand(tmp_path, first, body="PNG")
        write_ledger(tmp_path, first, seed(first))
        migration.apply(tmp_path)
        assert live_lines(tmp_path)[first.hash].job is Job.MEDIA

    def test_the_latest_landing_answers_not_the_first(self, tmp_path, migration):
        first = landing()
        again = dataclasses.replace(
            first,
            engine="0.2.0",
            date=datetime.date(2026, 9, 1),
            at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
            title="Tables, revised",
        )
        write_corpus_item(tmp_path)
        stand(tmp_path, again)
        write_ledger(tmp_path, first, again, seed(again))
        migration.apply(tmp_path)
        cancel = live_lines(tmp_path)[first.hash]
        assert (cancel.engine, cancel.date, cancel.title) == (
            "0.2.0",
            datetime.date(2026, 9, 1),
            "Tables, revised",
        )

    def test_a_renamed_item_is_recorded_where_its_file_stands_now(self, tmp_path, migration):
        # The rename moved the directory and kept the file's name; the
        # corpus file under the old id is gone.
        first = landing()
        write_corpus_item(tmp_path, item_id=RENAMED)
        moved = dataclasses.replace(first, path=first.path.replace(ITEM, RENAMED))
        stand(tmp_path, moved)
        write_ledger(tmp_path, first, seed(first))
        migration.apply(tmp_path)
        cancel = live_lines(tmp_path)[first.hash]
        assert (cancel.item, cancel.path) == (RENAMED, moved.path)

    def test_the_recorded_path_answers_first(self, tmp_path, migration):
        # A seed re-attributed to another live item that lists the link:
        # the landing's file still stands where it was recorded.
        first = landing()
        write_corpus_item(tmp_path)
        write_corpus_item(tmp_path, item_id=RENAMED)
        stand(tmp_path, first)
        stand(tmp_path, dataclasses.replace(first, path=first.path.replace(ITEM, RENAMED)))
        write_ledger(tmp_path, first, seed(first, item=RENAMED))
        migration.apply(tmp_path)
        cancel = live_lines(tmp_path)[first.hash]
        assert (cancel.item, cancel.path) == (RENAMED, first.path)


class TestNonMembers:
    @pytest.mark.parametrize(
        "fields",
        [
            {"status": Status.WAITING, "needs": Need.TRANSCRIBE},
            # A blocked retry of that park: the re-fetched post is written
            # over the stored one already, and transcription only adds.
            {"status": Status.BLOCKED, "needs": Need.TRANSCRIBE},
            {"status": Status.MANUAL, "reason": "thin-extraction"},
            {"status": Status.DEAD, "reason": None},
            {"status": Status.SKIPPED, "reason": "paywalled"},
            {"via": "migration-13"},
            {"via": "migration-19"},
            {"via": "harvest"},
            {"rerun": False},
        ],
    )
    def test_is_left_as_it_stands(self, tmp_path, migration, fields):
        first = landed_with_rerun(tmp_path, **fields)
        before = live_lines(tmp_path)[first.hash]
        report = migration.apply(tmp_path)
        assert live_lines(tmp_path)[first.hash] == before
        assert report.actions == []
        assert line_count(tmp_path) == 2

    def test_a_rerun_that_never_landed_stays_pending(self, tmp_path, migration):
        write_corpus_item(tmp_path, urls=(ATTACHMENT_URL,))
        dead = landing(ATTACHMENT_URL, kind=Kind.GITHUB, status=Status.DEAD, path=None, title=None)
        write_ledger(tmp_path, dead, seed(dead, via="migration-15"))
        report = migration.apply(tmp_path)
        assert live_lines(tmp_path)[dead.hash].status is Status.QUEUED
        assert report.actions == [
            "left 1 heal rerun(s) pending with no stored copy standing, to drain as before"
        ]

    def test_a_rerun_whose_landing_file_is_gone_stays_pending(self, tmp_path, migration):
        # Migration 15 deleted a misread repo-route output before seeding.
        write_corpus_item(tmp_path, urls=(REPO_URL,))
        first = landing(REPO_URL, kind=Kind.GITHUB)
        write_ledger(tmp_path, first, seed(first, via="migration-15"))
        migration.apply(tmp_path)
        assert live_lines(tmp_path)[first.hash].status is Status.QUEUED

    def test_a_directory_at_the_landings_path_is_no_stored_copy(self, tmp_path, migration):
        first = landing()
        write_corpus_item(tmp_path)
        (tmp_path / first.path).mkdir(parents=True)
        write_ledger(tmp_path, first, seed(first))
        migration.apply(tmp_path)
        assert live_lines(tmp_path)[first.hash].status is Status.QUEUED

    def test_a_recorded_path_outside_the_instance_is_never_trusted(self, tmp_path, migration):
        root = tmp_path / "instance"
        outside = tmp_path / "outside.md"
        outside.write_text("elsewhere\n")
        first = landing(path="../outside.md")
        write_corpus_item(root)
        write_ledger(root, first, seed(first))
        migration.apply(root)
        assert live_lines(root)[first.hash].status is Status.QUEUED

    def test_an_escaping_path_still_finds_its_file_under_the_live_item(self, tmp_path, migration):
        root = tmp_path / "instance"
        (tmp_path / "outside.md").write_text("elsewhere\n")
        first = landing(path="../outside.md")
        write_corpus_item(root)
        (root / "enrichment" / ITEM).mkdir(parents=True)
        (root / "enrichment" / ITEM / "outside.md").write_text("the copy\n")
        write_ledger(root, first, seed(first))
        migration.apply(root)
        assert live_lines(root)[first.hash].path == f"enrichment/{ITEM}/outside.md"


class TestUnclaimed:
    def test_a_unit_no_live_item_claims_is_skipped_with_why(self, tmp_path, migration):
        first = landing()
        stand(tmp_path, first)
        write_ledger(tmp_path, first, seed(first))
        report = migration.apply(tmp_path)
        assert live_lines(tmp_path)[first.hash].status is Status.QUEUED
        (skip,) = report.skipped
        assert skip.what == f"migration-18 rerun for {ITEM}"
        assert skip.why.startswith(f"corpus/2026/{ITEM}.md does not exist")
        assert f"no live corpus item claims {PAGE_URL}" in skip.why
        assert f"`enrich mark {PAGE_URL} done --path <its path>`" in skip.why
        assert report.actions == []

    @pytest.mark.parametrize(
        ("stated", "said"),
        [("spam", "spam"), ("spam\tsaid twice", "spam\tsaid twice"), ("", "no reason recorded")],
    )
    def test_an_excluded_item_names_its_exclusion(self, tmp_path, migration, stated, said):
        first = landing()
        write_ledger(tmp_path, first, seed(first))
        (tmp_path / "state" / "exclusions.tsv").write_text(
            f"2026-01-01-other-000000\tdup\n\n{ITEM}\t{stated}\n"
        )
        report = migration.apply(tmp_path)
        (skip,) = report.skipped
        assert skip.why.startswith(f"corpus/2026/{ITEM}.md is gone and the item is excluded")
        assert f"(state/exclusions.tsv: {said})" in skip.why

    def test_a_live_item_listing_the_link_claims_it(self, tmp_path, migration):
        # The stored item is gone; another live item lists the page and
        # holds the file under the same name.
        first = landing()
        write_corpus_item(tmp_path, item_id=RENAMED)
        stand(tmp_path, dataclasses.replace(first, path=first.path.replace(ITEM, RENAMED)))
        write_ledger(tmp_path, first, seed(first))
        report = migration.apply(tmp_path)
        assert report.skipped == []
        assert live_lines(tmp_path)[first.hash].item == RENAMED


class TestReport:
    def test_counts_per_migration_and_the_hand_heals(self, tmp_path, migration):
        heals = [
            (PAGE_URL, Kind.WEB, "migration-18"),
            ("https://docs.example.test/guide/code", Kind.WEB, "migration-18"),
            (POST_URL, Kind.X, "migration-16"),
            (ATTACHMENT_URL, Kind.GITHUB, "migration-15"),
        ]
        write_corpus_item(tmp_path, urls=[url for url, _, _ in heals] + [REPO_URL])
        lines: list[LedgerEntry] = []
        for url, kind, via in heals:
            first = landing(url, kind=kind)
            stand(tmp_path, first)
            lines += [first, seed(first, via=via)]
        gone = landing(REPO_URL, kind=Kind.GITHUB)
        write_ledger(tmp_path, *lines, gone, seed(gone, via="migration-15"))
        report = migration.apply(tmp_path)
        assert report.actions == [
            (
                "cancelled 4 heal rerun(s) whose stored copy stands (1 from migration 15, 1 from "
                "migration 16, 2 from migration 18): each is recorded done at that copy again, "
                "dated and attributed as it landed, so nothing re-fetches it and no digest is "
                "owed; 1 of migration 15's were attachment links a session had healed by hand; "
                "that reading stands; left 1 heal rerun(s) pending with no stored copy "
                "standing, to drain as before"
            )
        ]

    @pytest.mark.parametrize(
        "url", [REPO_URL, "https://files.example.test/user-attachments/files/1/a.pdf"]
    )
    def test_a_link_that_is_no_github_attachment_is_no_hand_heal(self, tmp_path, migration, url):
        landed_with_rerun(tmp_path, url, kind=Kind.GITHUB, via="migration-15")
        (action,) = migration.apply(tmp_path).actions
        assert "healed by hand" not in action

    def test_every_rerun_is_judged_whatever_came_before_it(self, tmp_path, migration):
        # Reruns that never landed and reruns whose file is gone, taking
        # turns, and one no live item claims, each ahead of one whose
        # stored copy stands.
        never = [f"{ATTACHMENT_URL}?n={n}" for n in range(2)]
        deleted = [f"{REPO_URL}/{n}" for n in range(2)]
        write_corpus_item(tmp_path, urls=(*never, *deleted, PAGE_URL))
        lines: list[LedgerEntry] = []
        for attachment, repo in zip(never, deleted, strict=True):
            dead = landing(attachment, kind=Kind.GITHUB, status=Status.DEAD, path=None, title=None)
            gone = landing(repo, kind=Kind.GITHUB)
            lines += [dead, seed(dead, via="migration-15"), gone, seed(gone, via="migration-15")]
        orphan = landing("https://docs.example.test/orphan", item=RENAMED)
        stand(tmp_path, orphan)
        first = landing()
        stand(tmp_path, first)
        write_ledger(tmp_path, *lines, orphan, seed(orphan), first, seed(first))
        report = migration.apply(tmp_path)
        assert live_lines(tmp_path)[first.hash].status is Status.DONE
        assert len(report.skipped) == 1
        (action,) = report.actions
        assert action.startswith("cancelled 1 heal rerun(s)")
        assert action.endswith(
            "left 4 heal rerun(s) pending with no stored copy standing, to drain as before"
        )

    def test_nothing_pending_reports_nothing(self, tmp_path, migration):
        first = landing()
        write_corpus_item(tmp_path)
        stand(tmp_path, first)
        write_ledger(tmp_path, first)
        report = migration.apply(tmp_path)
        assert (report.actions, report.skipped) == ([], [])
        assert line_count(tmp_path) == 1

    def test_an_instance_with_no_ledger_reports_nothing(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert (report.actions, report.skipped) == ([], [])


class TestTolerantRead:
    def test_an_unreadable_line_is_named_and_the_rest_cancelled(self, tmp_path, migration):
        first = landed_with_rerun(tmp_path)
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.write_text('{"hash": "torn\n\n' + path.read_text())
        report = migration.apply(tmp_path)
        assert live_lines(tmp_path)[first.hash].status is Status.DONE
        (skip,) = report.skipped
        assert skip.what == "ledger line 1"
        assert "migration 19 skipped it" in skip.why

    def test_a_landing_merged_in_after_its_rerun_still_answers(self, tmp_path, migration):
        # A union merge can write a unit's older landing below its newer
        # rerun: the landing loses as the live line and still answers as
        # the landing, and every unit after it is read.
        write_corpus_item(tmp_path, urls=(PAGE_URL, POST_URL))
        first = landing()
        stand(tmp_path, first)
        post = landing(POST_URL, kind=Kind.X)
        stand(tmp_path, post)
        write_ledger(tmp_path, seed(first), first, post, seed(post, via="migration-16"))
        migration.apply(tmp_path)
        lines = live_lines(tmp_path)
        assert lines[first.hash].path == first.path
        assert lines[post.hash].status is Status.DONE

    def test_unstamped_landings_resolve_by_position(self, tmp_path, migration):
        # Lines written before the write instant shipped carry no ``at``.
        first = landing(at=None, title="First reading")
        again = landing(at=None, title="Second reading")
        write_corpus_item(tmp_path)
        stand(tmp_path, again)
        write_ledger(tmp_path, first, again, seed(again))
        migration.apply(tmp_path)
        assert live_lines(tmp_path)[first.hash].title == "Second reading"


class TestIdempotent:
    def test_a_second_apply_writes_nothing(self, tmp_path, migration):
        landed_with_rerun(tmp_path)
        migration.apply(tmp_path)
        written = (tmp_path / "state" / "enrichment-ledger.jsonl").read_text()
        report = migration.apply(tmp_path)
        assert (tmp_path / "state" / "enrichment-ledger.jsonl").read_text() == written
        assert report.actions == []


class TestTheDigestIsNotOwed:
    def test_a_digest_written_after_the_landing_stays_current(self, tmp_path, migration):
        landed_with_rerun(tmp_path)
        instance = Instance(root=tmp_path)
        instance.digests_dir.mkdir(parents=True)
        (instance.digests_dir / f"{ITEM}.md").write_text("digest\n")
        record = {"item": ITEM, "stage": "digest", "date": "2026-08-20"}
        instance.passes_path.write_text(json.dumps(record) + "\n")
        migration.apply(tmp_path)
        assert run_mod.digest_orphans(instance) == []


class TestShipped:
    def test_sync_runs_it_after_eighteen(self, tmp_path):
        first = landed_with_rerun(tmp_path)
        applied = run_pending(
            tmp_path, today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.3"
        )
        numbers = [migration.number for migration in applied]
        assert numbers.index(19) == numbers.index(18) + 1
        assert live_lines(tmp_path)[first.hash].via == "migration-19"
