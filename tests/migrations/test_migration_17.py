"""Tests for migration 17: rerun the x posts whose article landed without its entities."""

import datetime

import pytest

from dex_engine.migrations.migration_17 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline.enrichment import render_enrichment
from dex_engine.pipeline.types import Job, Kind, LedgerEntry, Status
from dex_engine.pipeline.urls import work_hash

TODAY = datetime.date(2026, 9, 26)
NOW = datetime.datetime(2026, 9, 26, 9, 0, 0, 500000, tzinfo=datetime.UTC)
ENGINE = "0.2.2"
OLDER = "0.2.1"

ITEM = "2026-09-20-brain-abc123"
POST_URL = "https://x.com/i/status/4300000000000000001"
POST_HASH = work_hash(POST_URL)
FETCHED = datetime.date(2026, 9, 20)

ATTRIBUTION = "@hana — Fri Sep 18 16:25:10 +0000 2026"
ARTICLE = (
    f"{ATTRIBUTION}\n\n# How to Build a Company Brain\n\n"
    "Right now the smartest AI in your company works for one person.\n\n"
    "## Why the knowledge stays trapped\n\n"
    "macOS / Linux:\n\n"
    "Do that for a quarter.\n\n"
    "In this article, I show you how https://t.co/Zq8kR2mVw1"
)
ORDINARY = f"{ATTRIBUTION}\n\nThe ledger, not the corpus, is the work queue."


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version=ENGINE)


def write_ledger(root, *entries):
    path = root / "state" / "enrichment-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(ledger.to_line(e) + "\n" for e in entries))
    return path


def write_corpus_item(root, item_id=ITEM, urls=(POST_URL,)):
    path = root / "corpus" / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    listing = "".join(f"  - {url}\n" for url in urls)
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: inbox\nchannel: inbox\nshared_by: owner\ndate: 2026-09-20\n"
        f"urls:\n{listing}"
        "kinds: [x]\nstatus: raw\nenrichment: []\n---\nnote\n",
        encoding="utf-8",
    )
    return path


def write_post_file(root, body=ARTICLE, *, item=ITEM, url=POST_URL):
    meta: dict[str, str | int | None] = {
        "author": "Hana Iqbal (@hana)",
        "tweeted": "Fri Sep 18 16:25:10 +0000 2026",
        "via": "fxtwitter",
    }
    path = root / "enrichment" / item / f"x-{work_hash(url)[:6]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_enrichment(url, FETCHED, meta, body), encoding="utf-8")
    return path


def post_entry(  # noqa: PLR0913 — a fixture builder mirrors the entry's own fields
    *,
    url=POST_URL,
    kind=Kind.X,
    status=Status.DONE,
    item=ITEM,
    at=NOW,
    engine=OLDER,
    rerun=False,
    via=None,
    parent=None,
    depth=None,
    job=None,
):
    return LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=item,
        kind=kind,
        status=status,
        engine=engine,
        date=FETCHED,
        at=at,
        rerun=rerun,
        via=via,
        parent=parent,
        depth=depth,
        job=job,
        reason="parked" if status is Status.MANUAL else None,
        path=f"enrichment/{item}/{kind.value}-{work_hash(url)[:6]}.md"
        if status is Status.DONE
        else None,
    )


def seed_lines(path, unit_hash=POST_HASH):
    return [
        line
        for line in path.read_text().splitlines()
        if f'"hash": "{unit_hash}"' in line and '"queued"' in line
    ]


class TestMembers:
    def test_an_article_reruns_with_its_identity_and_stamps(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert len(seed_lines(path)) == 1
        post = ledger.load(path)[POST_HASH]
        assert post.url == POST_URL
        assert post.item == ITEM
        assert post.kind is Kind.X
        assert post.status is Status.QUEUED
        assert post.via == "migration-17"
        assert post.rerun is True
        assert post.job is None
        assert (post.parent, post.depth) == (None, None)
        assert post.engine == ENGINE
        assert post.date == TODAY
        assert post.at == NOW
        assert report.skipped == []
        assert report.anomalies == []
        assert report.actions == [
            (
                "seeded 1 x article rerun(s) — the fixed driver renders an article's code, "
                "links, figures and embedded posts, and pools its figures"
            ),
            (
                f"{ITEM}: the rerun re-reads the article of {POST_URL} — its code, link "
                "targets, figures and embedded posts land in the post's file, and its figures "
                "download beside it"
            ),
        ]

    def test_an_article_higher_up_a_thread_is_found(self, tmp_path, migration):
        # A reply to an article carries the article as its parent's text.
        write_corpus_item(tmp_path)
        write_post_file(tmp_path, f"{ARTICLE}\n\n@bob — Sat Sep 19 10:00:00 +0000 2026\n\nYes.")
        path = write_ledger(tmp_path, post_entry())
        migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.QUEUED

    def test_a_quoted_article_is_found(self, tmp_path, migration):
        body = f"{ORDINARY}\n\n> Quoting @erik: # A Field Guide\n>\n> Blocked is not dead."
        write_corpus_item(tmp_path)
        write_post_file(tmp_path, body)
        path = write_ledger(tmp_path, post_entry())
        migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.QUEUED

    def test_a_landing_whose_engine_does_not_parse_is_a_member(self, tmp_path, migration):
        # No engine carrying the fix writes anything but its own version.
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry(engine="hand-healed"))
        migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.QUEUED

    def test_a_digest_written_from_the_lossy_body_is_named_for_the_session(
        self, tmp_path, migration
    ):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        digest = tmp_path / "state" / "digests" / f"{ITEM}.md"
        digest.parent.mkdir(parents=True)
        digest.write_text("---\nid: x\n---\n", encoding="utf-8")
        write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert report.actions[1] == (
            f"{ITEM}: the rerun re-reads the article of {POST_URL} — its code, link targets, "
            "figures and embedded posts land in the post's file, and its figures download "
            "beside it — the item's digest was written without them and wants rewriting"
        )

    def test_a_renamed_item_reattributes_through_the_post_url(self, tmp_path, migration):
        renamed = "2026-09-20-brain-renamed-abc123"
        write_corpus_item(tmp_path, item_id=renamed)
        write_post_file(tmp_path, item=renamed)
        path = write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        post = ledger.load(path)[POST_HASH]
        assert post.status is Status.QUEUED
        assert post.item == renamed
        assert report.actions[1].startswith(f"{renamed}: the rerun re-reads")

    def test_a_promoted_posts_seed_keeps_its_lineage(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        lineage = work_hash("https://example.test/thread")
        path = write_ledger(tmp_path, post_entry(parent=lineage, depth=2, via="harvest"))
        migration.apply(tmp_path)
        post = ledger.load(path)[POST_HASH]
        assert (post.parent, post.depth) == (lineage, 2)


class TestNonMembers:
    @pytest.mark.parametrize(
        "body",
        [
            ORDINARY,
            f"{ATTRIBUTION}\n\n#hashtag first, then words",
            f"{ATTRIBUTION}\n\nAn intro line\n\n# a heading further down",
            f"{ATTRIBUTION}\n\nthe # sign in passing",
            f"{ORDINARY}\n\n> Quoting @erik: #tag and a quote",
            f"{ATTRIBUTION}\n\n# ",
            f"{ATTRIBUTION}\n\nThanks @bob — great thread\n\n# not a title",
        ],
    )
    def test_an_ordinary_post_is_no_member(self, tmp_path, migration, body):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path, body)
        path = write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []

    @pytest.mark.parametrize("engine", [ENGINE, "0.3.0"])
    def test_an_article_the_fixed_engine_landed_is_no_member(self, tmp_path, migration, engine):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry(engine=engine))
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []

    @pytest.mark.parametrize("status", [Status.MANUAL, Status.QUEUED])
    def test_a_post_not_landed_done_is_left_alone(self, tmp_path, migration, status):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry(status=status))
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is status
        assert report.actions == []

    def test_another_kinds_unit_is_never_a_member(self, tmp_path, migration):
        url = "https://example.test/brain"
        write_corpus_item(tmp_path, urls=(url,))
        page = tmp_path / "enrichment" / ITEM / f"web-{work_hash(url)[:6]}.md"
        page.parent.mkdir(parents=True)
        page.write_text(render_enrichment(url, FETCHED, {}, ARTICLE), encoding="utf-8")
        path = write_ledger(tmp_path, post_entry(url=url, kind=Kind.WEB))
        migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(url)].status is Status.DONE

    def test_a_media_child_is_never_a_member(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        figure = "https://pbs.twimg.com/media/FgAbCdEf.jpg"
        write_post_file(tmp_path, url=figure)
        path = write_ledger(
            tmp_path, post_entry(url=figure, job=Job.MEDIA, parent=POST_HASH, depth=1)
        )
        report = migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(figure)].status is Status.DONE
        assert report.actions == []


class TestIdempotency:
    def test_a_post_migration_16_already_queued_is_never_seeded_again(self, tmp_path, migration):
        # An article that is also a video post: whichever migration runs
        # second finds its live line queued.
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        later = NOW + datetime.timedelta(seconds=1)
        path = write_ledger(
            tmp_path,
            post_entry(),
            post_entry(status=Status.QUEUED, rerun=True, via="migration-16", at=later),
        )
        lines_before = path.read_text().count("\n")
        report = migration.apply(tmp_path)
        assert path.read_text().count("\n") == lines_before
        assert report.actions == []

    def test_a_second_apply_after_the_rerun_drains_finds_nothing(self, tmp_path, migration):
        # The fixed renderer keeps the heading, so only the landing's
        # engine tells the rerun's file from the one it replaced.
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry())
        migration.apply(tmp_path)
        landed = post_entry(
            engine=ENGINE, rerun=True, via="migration-17", at=NOW + datetime.timedelta(days=1)
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(ledger.to_line(landed) + "\n")
        lines_before = path.read_text().count("\n")
        report = migration.apply(tmp_path)
        assert path.read_text().count("\n") == lines_before
        assert (report.actions, report.skipped, report.anomalies) == ([], [], [])


class TestTolerantRead:
    def test_a_missing_ledger_is_a_noop(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert (report.actions, report.skipped, report.anomalies) == ([], [], [])

    def test_a_member_whose_file_is_gone_is_skipped_with_why(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []
        assert report.skipped[0].what == f"article rerun for {POST_URL}"
        assert f"enrichment/{ITEM}/x-{POST_HASH[:6]}.md is gone" in report.skipped[0].why

    def test_an_unreadable_file_is_an_anomaly_and_the_rest_heal(self, tmp_path, migration):
        other_item = "2026-09-19-second-def456"
        other_url = "https://x.com/i/status/4300000000000000002"
        write_corpus_item(tmp_path)
        write_corpus_item(tmp_path, item_id=other_item, urls=(other_url,))
        write_post_file(tmp_path, item=other_item, url=other_url)
        write_post_file(tmp_path).write_bytes(b"\xff\xfe not text")
        path = write_ledger(tmp_path, post_entry(), post_entry(url=other_url, item=other_item))
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[POST_HASH].status is Status.DONE
        assert live[work_hash(other_url)].status is Status.QUEUED
        assert len(report.anomalies) == 1
        assert f"enrichment/{ITEM}/x-{POST_HASH[:6]}.md cannot be read" in report.anomalies[0]

    def test_an_anomaly_survives_a_seedless_run(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path).write_bytes(b"\xff\xfe not text")
        write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert len(report.anomalies) == 1

    def test_unparseable_and_blank_lines_never_stop_the_healing(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"not json at all\n\n{ledger.to_line(post_entry())}\n", encoding="utf-8")
        report = migration.apply(tmp_path)
        assert len(seed_lines(path)) == 1
        assert [s.what for s in report.skipped] == ["ledger line 1"]
        assert "does not parse" in report.skipped[0].why

    def test_a_line_older_than_one_before_it_never_stops_the_read(self, tmp_path, migration):
        other_item = "2026-09-19-second-def456"
        other_url = "https://x.com/i/status/4300000000000000002"
        write_corpus_item(tmp_path)
        write_corpus_item(tmp_path, item_id=other_item, urls=(other_url,))
        write_post_file(tmp_path)
        write_post_file(tmp_path, item=other_item, url=other_url)
        earlier = NOW - datetime.timedelta(days=1)
        path = write_ledger(
            tmp_path,
            post_entry(),
            post_entry(status=Status.MANUAL, at=earlier),
            post_entry(url=other_url, item=other_item),
        )
        migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.QUEUED
        assert ledger.load(path)[work_hash(other_url)].status is Status.QUEUED

    def test_a_parse_skip_survives_a_memberless_ledger(self, tmp_path, migration):
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all\n", encoding="utf-8")
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert [s.what for s in report.skipped] == ["ledger line 1"]

    def test_a_purged_item_is_never_rerun(self, tmp_path, migration):
        exclusions = tmp_path / "state" / "exclusions.tsv"
        exclusions.parent.mkdir(parents=True, exist_ok=True)
        exclusions.write_text(
            f"\n2026-01-01-unrelated-000000\toff topic\n{ITEM}\towner purged it\tfor good\n",
            encoding="utf-8",
        )
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []
        assert report.skipped[0].what == f"article rerun for {ITEM}"
        assert "owner purged it\tfor good" in report.skipped[0].why

    def test_an_excluded_item_with_no_reason_says_so(self, tmp_path, migration):
        exclusions = tmp_path / "state" / "exclusions.tsv"
        exclusions.parent.mkdir(parents=True, exist_ok=True)
        exclusions.write_text(f"{ITEM}\n", encoding="utf-8")
        write_post_file(tmp_path)
        write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert "(state/exclusions.tsv: no reason recorded)" in report.skipped[0].why

    def test_an_unclaimed_member_is_skipped_and_never_stops_the_rest(self, tmp_path, migration):
        orphan_item = "2026-09-19-orphaned-def456"
        orphan_url = "https://x.com/i/status/4300000000000000003"
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        write_post_file(tmp_path, item=orphan_item, url=orphan_url)
        path = write_ledger(tmp_path, post_entry(url=orphan_url, item=orphan_item), post_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.QUEUED
        assert ledger.load(path)[work_hash(orphan_url)].status is Status.DONE
        assert len(report.skipped) == 1
        assert "nothing claims this work" in report.skipped[0].why
        assert f"corpus/2026/{orphan_item}.md" in report.skipped[0].why
