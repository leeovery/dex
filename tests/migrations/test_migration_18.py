"""Tests for migration 18: re-extract every page the unfixed article seam stored or parked thin."""

import dataclasses
import datetime

import pytest

from dex_engine.migrations import run_pending
from dex_engine.migrations.migration_18 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.enrichment import render_enrichment
from dex_engine.pipeline.types import Job, Kind, LedgerEntry, Need, Status, Unusable
from dex_engine.pipeline.urls import work_hash
from tests.conftest import FakeDriver
from tests.pipeline.test_run import make_ctx

TODAY = datetime.date(2026, 9, 22)
NOW = datetime.datetime(2026, 9, 22, 9, 0, 0, 500000, tzinfo=datetime.UTC)
ENGINE = "0.2.2"
UNFIXED = "0.2.1"
LANDED = datetime.date(2026, 9, 10)

ITEM = "2026-09-20-docs-page-abc123"
PAGE_URL = "https://docs.example.test/guide/tables"
PAPER_URL = "https://arxiv.org/abs/2601.00001"
REVIEW_URL = "https://openreview.net/forum?id=abc123"
THIN = "thin-extraction"
PAYWALL = "payment/login required (HTTP 402)"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version=ENGINE)


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
        "source: manual\nchannel: inbox\nshared_by: alex\ndate: 2026-09-20\n"
        f"urls:\n{listing}"
        "kinds: [web]\nstatus: raw\nenrichment: []\n---\n**alex**: note\n",
        encoding="utf-8",
    )
    return path


def unit(  # noqa: PLR0913 — a fixture builder mirrors the entry's own fields
    url=PAGE_URL,
    *,
    kind=Kind.WEB,
    status=Status.DONE,
    engine=UNFIXED,
    item=ITEM,
    at=NOW - datetime.timedelta(days=12),
    job=None,
    parent=None,
    depth=None,
    http_shared=False,
    rerun=False,
    reason=None,
):
    unit_hash = work_hash(url)
    return LedgerEntry(
        hash=unit_hash,
        url=url,
        item=item,
        kind=kind,
        status=status,
        needs=Need.TRANSCRIBE if status is Status.WAITING else None,
        attempts=1 if status is Status.BLOCKED else None,
        engine=engine,
        date=LANDED,
        at=at,
        job=job,
        parent=parent,
        depth=depth,
        http_shared=http_shared,
        rerun=rerun,
        via="migration-18" if rerun else ("harvest" if parent else None),
        reason=reason or (PAYWALL if status in (Status.MANUAL, Status.SKIPPED) else None),
        path=f"enrichment/{item}/{kind.value}-{unit_hash[:6]}.md"
        if status is Status.DONE
        else None,
    )


def write_paper(root, url=PAPER_URL, *, meta, item=ITEM):
    """A paper landing's file, written by the real renderer."""
    path = root / "enrichment" / item / f"paper-{work_hash(url)[:6]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_enrichment(url, LANDED, meta, "## Abstract\n\nWhat the paper says."),
        encoding="utf-8",
    )
    return path


FULL_TEXT: dict[str, str | int | None] = {
    "title": "A Paper",
    "published": "2026-01-02",
    "arxiv_id": "2601.00001",
}
ABSTRACT_ONLY: dict[str, str | int | None] = {**FULL_TEXT, "note": "abstract only"}


def live_lines(root):
    # Read as of a month on, so every line a test writes is in the past.
    path = root / "state" / "enrichment-ledger.jsonl"
    return ledger.latest_readable(path, now=lambda: NOW + datetime.timedelta(days=30))


def seeds(root):
    return [entry for entry in live_lines(root).values() if entry.status is Status.QUEUED]


class TestMembers:
    def test_a_web_landing_by_the_unfixed_engine_requeues_as_a_rerun(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, unit())
        report = migration.apply(tmp_path)
        (seed,) = seeds(tmp_path)
        assert seed.hash == work_hash(PAGE_URL)
        assert seed.url == PAGE_URL
        assert seed.item == ITEM
        assert seed.kind is Kind.WEB
        assert seed.rerun
        assert seed.via == "migration-18"
        assert (seed.engine, seed.date, seed.at) == (ENGINE, TODAY, NOW)
        assert seed.path is None
        assert report.actions[0].startswith("seeded 1 rerun(s) — 1 web page(s), 0 paper(s)")
        assert "Already stored" in report.actions[0]
        assert "Needs writing up" in report.actions[0]

    @pytest.mark.parametrize("engine", ["0.0.1", "0.1.13", "0.2.0", "0.2.1"])
    def test_every_unfixed_vintage_is_a_member(self, tmp_path, migration, engine):
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, unit(engine=engine))
        migration.apply(tmp_path)
        assert len(seeds(tmp_path)) == 1

    def test_an_arxiv_full_text_landing_requeues(self, tmp_path, migration):
        write_corpus_item(tmp_path, urls=(PAPER_URL,))
        write_ledger(tmp_path, unit(PAPER_URL, kind=Kind.PAPER))
        write_paper(tmp_path, meta=FULL_TEXT)
        report = migration.apply(tmp_path)
        (seed,) = seeds(tmp_path)
        assert seed.kind is Kind.PAPER
        assert report.actions[0].startswith("seeded 1 rerun(s) — 0 web page(s), 1 paper(s)")

    def test_a_paper_read_as_an_article_requeues(self, tmp_path, migration):
        # The non-arXiv paper hosts go through the same seam as a web page,
        # and their landing records no arxiv_id.
        write_corpus_item(tmp_path, urls=(REVIEW_URL,))
        write_ledger(tmp_path, unit(REVIEW_URL, kind=Kind.PAPER))
        write_paper(tmp_path, REVIEW_URL, meta={"title": "A Review"})
        migration.apply(tmp_path)
        assert [seed.url for seed in seeds(tmp_path)] == [REVIEW_URL]

    def test_a_harvested_pages_seed_keeps_its_lineage_and_license(self, tmp_path, migration):
        parent = unit()
        child_url = "http://blog.example.test/linked-post"
        child = unit(child_url, parent=parent.hash, depth=1, http_shared=True)
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, parent, child)
        migration.apply(tmp_path)
        seed = live_lines(tmp_path)[child.hash]
        assert seed.status is Status.QUEUED
        assert (seed.parent, seed.depth, seed.http_shared) == (parent.hash, 1, True)


class TestNonMembers:
    def test_a_media_download_never_requeues(self, tmp_path, migration):
        media = unit(
            "https://cdn.example.test/hero.png", job=Job.MEDIA, parent="0123456789", depth=1
        )
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, media)
        assert migration.apply(tmp_path).actions == []
        assert seeds(tmp_path) == []

    @pytest.mark.parametrize(
        "status",
        [Status.QUEUED, Status.WAITING, Status.BLOCKED, Status.MANUAL, Status.SKIPPED],
    )
    def test_a_unit_that_has_not_landed_never_requeues(self, tmp_path, migration, status):
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, unit(status=status))
        before = path.read_text()
        assert migration.apply(tmp_path).actions == []
        assert path.read_text() == before

    def test_an_abstract_only_paper_never_requeues(self, tmp_path, migration):
        write_corpus_item(tmp_path, urls=(PAPER_URL,))
        write_ledger(tmp_path, unit(PAPER_URL, kind=Kind.PAPER))
        write_paper(tmp_path, meta=ABSTRACT_ONLY)
        assert migration.apply(tmp_path).actions == []
        assert seeds(tmp_path) == []

    def test_a_paper_whose_stored_file_cannot_be_found_is_reported(self, tmp_path, migration):
        write_corpus_item(tmp_path, urls=(PAPER_URL,))
        write_ledger(tmp_path, unit(PAPER_URL, kind=Kind.PAPER))
        report = migration.apply(tmp_path)
        assert (report.actions, report.anomalies) == ([], [])
        assert seeds(tmp_path) == []
        (skip,) = report.skipped
        assert skip.what == f"paper {PAPER_URL}"
        assert skip.why.startswith(
            f"no stored file at enrichment/{ITEM}/paper-{work_hash(PAPER_URL)[:6]}.md "
            f"or under enrichment/{ITEM}/"
        )

    def test_a_renamed_items_paper_is_read_where_the_rename_moved_it(self, tmp_path, migration):
        # The rename moved the enrichment directory and kept the file's
        # name; the landing still records the path under the old id.
        renamed = "2026-09-20-renamed-abc123"
        write_corpus_item(tmp_path, item_id=renamed, urls=(PAPER_URL,))
        write_ledger(tmp_path, unit(PAPER_URL, kind=Kind.PAPER))
        write_paper(tmp_path, meta=FULL_TEXT, item=renamed)
        report = migration.apply(tmp_path)
        (seed,) = seeds(tmp_path)
        assert (seed.url, seed.item) == (PAPER_URL, renamed)
        assert report.skipped == []

    def test_a_renamed_items_abstract_only_paper_stays_out(self, tmp_path, migration):
        renamed = "2026-09-20-renamed-abc123"
        write_corpus_item(tmp_path, item_id=renamed, urls=(PAPER_URL,))
        write_ledger(tmp_path, unit(PAPER_URL, kind=Kind.PAPER))
        write_paper(tmp_path, meta=ABSTRACT_ONLY, item=renamed)
        report = migration.apply(tmp_path)
        assert (report.actions, report.skipped) == ([], [])

    def test_a_landing_that_records_no_path_is_read_at_the_drains_name(self, tmp_path, migration):
        write_corpus_item(tmp_path, urls=(PAPER_URL,))
        write_ledger(tmp_path, dataclasses.replace(unit(PAPER_URL, kind=Kind.PAPER), path=None))
        write_paper(tmp_path, meta=FULL_TEXT)
        migration.apply(tmp_path)
        assert [seed.url for seed in seeds(tmp_path)] == [PAPER_URL]

    def test_a_paper_left_out_never_stops_the_rest(self, tmp_path, migration):
        write_corpus_item(tmp_path, urls=(PAPER_URL, PAGE_URL))
        write_ledger(tmp_path, unit(PAPER_URL, kind=Kind.PAPER), unit())
        write_paper(tmp_path, meta=ABSTRACT_ONLY)
        migration.apply(tmp_path)
        assert [seed.url for seed in seeds(tmp_path)] == [PAGE_URL]

    def test_a_recorded_path_outside_the_instance_is_never_read(self, tmp_path, migration):
        root = tmp_path / "instance"
        outside = tmp_path / "elsewhere" / "paper.md"
        outside.parent.mkdir()
        outside.write_text(render_enrichment(PAPER_URL, LANDED, FULL_TEXT, "Not this."))
        write_corpus_item(root, urls=(PAPER_URL,))
        landing = dataclasses.replace(
            unit(PAPER_URL, kind=Kind.PAPER), path="../elsewhere/paper.md"
        )
        write_ledger(root, landing)
        report = migration.apply(root)
        assert seeds(root) == []
        (skip,) = report.skipped
        assert skip.what == f"paper {PAPER_URL}"

    @pytest.mark.parametrize("engine", ["0.2.2", "0.3.0", "1.0.0"])
    def test_a_landing_by_the_fixed_engine_never_requeues(self, tmp_path, migration, engine):
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, unit(engine=engine))
        assert migration.apply(tmp_path).actions == []
        assert seeds(tmp_path) == []

    @pytest.mark.parametrize(
        ("kind", "url"),
        [
            (Kind.X, "https://x.com/i/status/1234567890"),
            (Kind.YOUTUBE, "https://www.youtube.com/watch?v=abcdefghijk"),
            (Kind.GITHUB, "https://github.com/example/project"),
        ],
    )
    def test_a_kind_that_never_reads_through_the_seam_never_requeues(
        self, tmp_path, migration, kind, url
    ):
        # Its stored file is there to be read: the kind alone refuses it.
        landing = unit(url, kind=kind)
        stored = tmp_path / str(landing.path)
        stored.parent.mkdir(parents=True)
        stored.write_text(render_enrichment(url, LANDED, {"title": "A post"}, "What it says."))
        write_corpus_item(tmp_path, urls=(url,))
        write_ledger(tmp_path, landing)
        assert migration.apply(tmp_path).actions == []


class TestThinParks:
    """A page the unfixed seam parked thin, which a fresh share today might land."""

    @pytest.mark.parametrize("engine", ["0.1.0", "0.1.13", "0.2.0", "0.2.1"])
    def test_a_thin_park_by_every_unfixed_rewrite_engine_requeues(
        self, tmp_path, migration, engine
    ):
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, unit(status=Status.MANUAL, reason=THIN, engine=engine))
        report = migration.apply(tmp_path)
        (seed,) = seeds(tmp_path)
        assert (seed.hash, seed.rerun, seed.via) == (work_hash(PAGE_URL), True, "migration-18")
        assert (seed.reason, seed.engine) == (None, ENGINE)
        assert report.actions[0].startswith(
            "seeded 1 rerun(s) — 1 web page(s), 0 paper(s), 1 of them parked thin"
        )

    def test_a_paper_read_as_an_article_that_parked_thin_requeues(self, tmp_path, migration):
        # No stored file to read: only the article route ever parks a paper thin.
        write_corpus_item(tmp_path, urls=(REVIEW_URL,))
        write_ledger(tmp_path, unit(REVIEW_URL, kind=Kind.PAPER, status=Status.MANUAL, reason=THIN))
        report = migration.apply(tmp_path)
        assert [seed.url for seed in seeds(tmp_path)] == [REVIEW_URL]
        assert "0 web page(s), 1 paper(s), 1 of them parked thin" in report.actions[0]

    def test_a_landing_is_not_counted_as_parked_thin(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, unit())
        (action,) = migration.apply(tmp_path).actions
        assert "0 of them parked thin" in action

    @pytest.mark.parametrize(
        "reason",
        [
            PAYWALL,
            f"{PAYWALL}; wayback snapshot extraction was thin",
            "still blocked after 5 attempts — HTTP 429",
            "unstated (pre-migration)",
        ],
        ids=["paywall", "paywall-thin-snapshot", "escalated-blocked", "pre-rewrite"],
    )
    def test_a_manual_park_for_any_other_reason_stays_parked(self, tmp_path, migration, reason):
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, unit(status=Status.MANUAL, reason=reason))
        before = path.read_text()
        assert migration.apply(tmp_path).actions == []
        assert path.read_text() == before

    def test_a_thin_verdict_closed_skipped_stays_alone(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, unit(status=Status.SKIPPED, reason=THIN))
        assert migration.apply(tmp_path).actions == []

    def test_the_pre_rewrite_engines_dead_line_stays_alone(self, tmp_path, migration):
        # It wrote a thin page and a gone one as the same reasonless dead line.
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, unit(status=Status.DEAD, engine="0.0.1"))
        assert migration.apply(tmp_path).actions == []

    def test_a_park_by_the_fixed_engine_never_requeues(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, unit(status=Status.MANUAL, reason=THIN, engine=ENGINE))
        assert migration.apply(tmp_path).actions == []

    def test_a_page_the_drain_parks_thin_again_stays_out_of_the_cohort(self, instance, migration):
        # Through the real drain: the rerun re-parks, and that park line
        # carries the engine that wrote it, so a re-application finds nothing.
        root = instance.root
        write_corpus_item(root)
        path = write_ledger(root, unit(status=Status.MANUAL, reason=THIN))
        migration.apply(root)
        later = NOW + datetime.timedelta(hours=1)
        driver = FakeDriver(fetch_fn=lambda _unit: Unusable(evidence=THIN))
        run_mod.run(
            make_ctx(
                instance, driver, today=lambda: TODAY, now=lambda: later, engine_version=ENGINE
            )
        )
        repark = live_lines(root)[work_hash(PAGE_URL)]
        assert (repark.status, repark.reason, repark.engine) == (Status.MANUAL, THIN, ENGINE)
        after_run = path.read_text()
        again = build(
            today=lambda: TODAY,
            now=lambda: later + datetime.timedelta(days=1),
            engine_version=ENGINE,
        )
        assert again.apply(root).actions == []
        assert path.read_text() == after_run


class TestIdempotency:
    def test_a_second_apply_finds_the_seed_queued_and_adds_nothing(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, unit())
        migration.apply(tmp_path)
        after_first = path.read_text()
        assert migration.apply(tmp_path).actions == []
        assert path.read_text() == after_first

    def test_the_reruns_landing_by_the_fixed_engine_is_outside_the_cohort(
        self, tmp_path, migration
    ):
        # A log race re-applies the migration after the rerun landed: the
        # landing carries the fixed engine's version, so nothing reseeds.
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, unit())
        migration.apply(tmp_path)
        landed = unit(engine=ENGINE, at=NOW + datetime.timedelta(hours=1), rerun=True)
        ledger.append(path, landed)
        after_landing = path.read_text()
        later = build(
            today=lambda: TODAY,
            now=lambda: NOW + datetime.timedelta(days=1),
            engine_version=ENGINE,
        )
        assert later.apply(tmp_path).actions == []
        assert path.read_text() == after_landing

    def test_the_runner_logs_it_once(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, unit())
        first = run_pending(
            tmp_path,
            today=lambda: TODAY,
            now=lambda: NOW,
            engine_version=ENGINE,
            migrations=[migration],
        )
        again = run_pending(
            tmp_path,
            today=lambda: TODAY,
            now=lambda: NOW,
            engine_version=ENGINE,
            migrations=[migration],
        )
        assert [applied.number for applied in first] == [18]
        assert again == []

    def test_the_live_line_decides_whatever_the_file_order(self, tmp_path, migration):
        # Union merges interleave machines' lines: the later write instant
        # is the live line even where it sits earlier in the file, and the
        # lines after the stale one are still read.
        other = "https://docs.example.test/guide/code"
        write_corpus_item(tmp_path, urls=(PAGE_URL, other))
        requeued = unit(status=Status.QUEUED, engine=ENGINE, at=NOW, rerun=True)
        stale = unit(at=NOW - datetime.timedelta(days=3))
        path = write_ledger(tmp_path, requeued, stale, unit(other))
        before = path.read_text()
        migration.apply(tmp_path)
        (appended,) = path.read_text().removeprefix(before).splitlines()
        assert ledger.from_line(appended).url == other

    def test_between_lines_of_one_instant_the_later_in_the_file_decides(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        landed = unit(at=NOW - datetime.timedelta(days=3))
        requeued = unit(status=Status.QUEUED, engine=ENGINE, at=landed.at, rerun=True)
        path = write_ledger(tmp_path, landed, requeued)
        before = path.read_text()
        assert migration.apply(tmp_path).actions == []
        assert path.read_text() == before


class TestTolerantRead:
    def test_a_missing_ledger_is_a_noop(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert (report.actions, report.skipped, report.anomalies) == ([], [], [])
        assert not (tmp_path / "state").exists()

    def test_an_unparseable_ledger_line_never_stops_the_healing(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, unit())
        path.write_text('{"hash": "not a line"\n' + path.read_text())
        report = migration.apply(tmp_path)
        assert [skip.what for skip in report.skipped] == ["ledger line 1"]
        assert "article-seam rerun skipped it" in report.skipped[0].why
        assert len(seeds(tmp_path)) == 1

    def test_a_blank_line_never_ends_the_read(self, tmp_path, migration):
        other = "https://docs.example.test/guide/code"
        write_corpus_item(tmp_path, urls=(PAGE_URL, other))
        path = write_ledger(tmp_path, unit())
        path.write_text(path.read_text() + "\n" + ledger.to_line(unit(other)) + "\n")
        migration.apply(tmp_path)
        assert [seed.url for seed in seeds(tmp_path)] == [PAGE_URL, other]

    def test_a_parse_skip_is_reported_even_with_no_members(self, tmp_path, migration):
        path = write_ledger(tmp_path, unit(engine=ENGINE))
        path.write_text(path.read_text() + "not json\n")
        report = migration.apply(tmp_path)
        assert [skip.what for skip in report.skipped] == ["ledger line 2"]

    def test_an_undatable_landing_is_skipped_with_why(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_ledger(tmp_path, unit(engine="nightly"))
        report = migration.apply(tmp_path)
        assert report.actions == []
        (skip,) = report.skipped
        assert skip.what == f"ledger entry {work_hash(PAGE_URL)}"
        assert "unparseable engine 'nightly'" in skip.why

    def test_an_unparseable_paper_file_is_an_anomaly(self, tmp_path, migration):
        write_corpus_item(tmp_path, urls=(PAPER_URL,))
        write_ledger(tmp_path, unit(PAPER_URL, kind=Kind.PAPER))
        stored = write_paper(tmp_path, meta=FULL_TEXT)
        stored.write_text("---\nurl: https://arxiv.org/abs/2601.00001\nno closing fence\n")
        report = migration.apply(tmp_path)
        assert report.actions == []
        (anomaly,) = report.anomalies
        assert anomaly.startswith(f"enrichment/{ITEM}/paper-{work_hash(PAPER_URL)[:6]}.md")
        assert PAPER_URL in anomaly

    def test_an_anomaly_never_stops_the_rest(self, tmp_path, migration):
        write_corpus_item(tmp_path, urls=(PAGE_URL, PAPER_URL))
        write_ledger(tmp_path, unit(), unit(PAPER_URL, kind=Kind.PAPER))
        write_paper(tmp_path, meta=FULL_TEXT).write_text("---\nunterminated\n")
        report = migration.apply(tmp_path)
        assert len(report.anomalies) == 1
        assert [seed.url for seed in seeds(tmp_path)] == [PAGE_URL]

    def test_an_unpulled_corpus_seeds_nothing_and_says_why(self, tmp_path, migration):
        write_ledger(tmp_path, unit())
        report = migration.apply(tmp_path)
        assert report.actions == []
        (skip,) = report.skipped
        assert skip.what == f"article-seam rerun for {ITEM}"
        assert skip.why.startswith(f"corpus/2026/{ITEM}.md does not exist")
        assert f"no live corpus item claims {PAGE_URL}" in skip.why

    def test_an_anomaly_survives_a_run_that_seeds_nothing(self, tmp_path, migration):
        gone = "2026-09-09-gone-def456"
        write_corpus_item(tmp_path, urls=(PAPER_URL,))
        write_ledger(tmp_path, unit(item=gone), unit(PAPER_URL, kind=Kind.PAPER))
        write_paper(tmp_path, meta=FULL_TEXT).write_text("---\nunterminated\n")
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert len(report.anomalies) == 1
        assert [skip.what for skip in report.skipped] == [f"article-seam rerun for {gone}"]


class TestOwnership:
    def test_a_renamed_item_reattributes_through_its_url(self, tmp_path, migration):
        renamed = "2026-09-20-renamed-abc123"
        write_corpus_item(tmp_path, item_id=renamed)
        write_ledger(tmp_path, unit())
        report = migration.apply(tmp_path)
        (seed,) = seeds(tmp_path)
        assert seed.item == renamed
        (action,) = report.actions
        assert action.startswith("seeded 1 rerun(s)")
        assert action.endswith("; 1 re-attributed to the live item that claims the unit")

    def test_a_harvested_page_follows_its_renamed_item_through_the_parent(
        self, tmp_path, migration
    ):
        # No frontmatter lists a harvested page: its parent's claim is the
        # only corpus answer it has.
        renamed = "2026-09-20-renamed-abc123"
        write_corpus_item(tmp_path, item_id=renamed)
        parent = unit(engine=ENGINE)
        child = unit("https://blog.example.test/linked-post", parent=parent.hash, depth=1)
        write_ledger(tmp_path, parent, child)
        migration.apply(tmp_path)
        (seed,) = seeds(tmp_path)
        assert (seed.hash, seed.item) == (child.hash, renamed)

    def test_an_excluded_items_paper_gets_the_exclusion_and_no_requeue_advice(
        self, tmp_path, migration
    ):
        # `dex exclude` deletes the item's enrichment with it, so the paper's
        # file is gone too: the report must name the exclusion, not ask the
        # session to requeue a unit the owner removed.
        write_ledger(tmp_path, unit(PAPER_URL, kind=Kind.PAPER))
        (tmp_path / "state" / "exclusions.tsv").write_text(f"{ITEM}\toff-topic for this dex\n")
        report = migration.apply(tmp_path)
        assert seeds(tmp_path) == []
        (skip,) = report.skipped
        assert skip.what == f"article-seam rerun for {ITEM}"
        assert "excluded on the record (state/exclusions.tsv: off-topic for this dex)" in skip.why
        assert "enrich mark" not in skip.why

    def test_a_purged_item_is_never_requeued(self, tmp_path, migration):
        write_ledger(tmp_path, unit())
        (tmp_path / "state" / "exclusions.tsv").write_text(f"{ITEM}\toff-topic for this dex\n")
        report = migration.apply(tmp_path)
        assert seeds(tmp_path) == []
        (skip,) = report.skipped
        assert "excluded on the record (state/exclusions.tsv: off-topic for this dex)" in skip.why

    def test_a_purge_with_no_stated_reason_still_names_the_record(self, tmp_path, migration):
        write_ledger(tmp_path, unit())
        (tmp_path / "state" / "exclusions.tsv").write_text(
            f"2026-09-01-earlier-fed321\tduplicate of another item\n\n{ITEM}\n"
        )
        (skip,) = migration.apply(tmp_path).skipped
        assert "(state/exclusions.tsv: no reason recorded)" in skip.why

    def test_an_unclaimed_member_never_stops_the_rest(self, tmp_path, migration):
        other = "https://docs.example.test/guide/code"
        write_corpus_item(tmp_path, urls=(other,))
        write_ledger(tmp_path, unit(item="2026-09-19-gone-def456"), unit(other))
        report = migration.apply(tmp_path)
        assert [seed.url for seed in seeds(tmp_path)] == [other]
        assert [skip.what for skip in report.skipped] == [
            "article-seam rerun for 2026-09-19-gone-def456"
        ]
