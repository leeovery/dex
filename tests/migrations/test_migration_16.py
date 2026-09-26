"""Tests for migration 16: rerun the x posts whose video was pooled, never heard."""

import dataclasses
import datetime

import pytest

from dex_engine.migrations.migration_16 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline.enrichment import render_enrichment
from dex_engine.pipeline.types import Job, Kind, LedgerEntry, Need, Status
from dex_engine.pipeline.urls import work_hash

TODAY = datetime.date(2026, 9, 26)
NOW = datetime.datetime(2026, 9, 26, 9, 0, 0, 500000, tzinfo=datetime.UTC)
ENGINE = "0.2.2"

ITEM = "2026-09-20-talk-abc123"
POST_URL = "https://x.com/i/status/4200000000000000001"
POST_HASH = work_hash(POST_URL)
VIDEO_URL = (
    "https://video.twimg.com/amplify_video/4200000000000000009/vid/avc1/1280x720/aB.mp4?tag=16"
)
GIF_URL = "https://video.twimg.com/tweet_video/GfAbCdEfGh.mp4"
PHOTO_URL = "https://pbs.twimg.com/media/PhAbCdEfGh.jpg"
FETCHED = datetime.date(2026, 9, 20)


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


def write_post_file(root, *, item=ITEM, url=POST_URL, heard=False):
    """The post's enrichment file: as the old driver landed it, or once its video was heard."""
    meta: dict[str, str | int | None] = {
        "author": "Ines Duarte (@ines)",
        "tweeted": "Sun Sep 20 17:24:01 +0000 2026",
    }
    body = "@ines — Sun Sep 20 17:24:01 +0000 2026\n\nWatch this.\n\n(video post)"
    if heard:
        meta.update({"enclosure": VIDEO_URL, "via": "whisper-local", "model": "medium"})
        body += "\n\n## Transcript\n\nspoken words"
    else:
        meta["via"] = "fxtwitter"
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
    rerun=False,
    parent=None,
    depth=None,
):
    return LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=item,
        kind=kind,
        status=status,
        engine="0.2.1",
        date=FETCHED,
        at=at,
        rerun=rerun,
        via="migration-16" if rerun else None,
        parent=parent,
        depth=depth,
        reason="parked" if status in (Status.MANUAL, Status.WAITING) else None,
        needs=Need.TRANSCRIBE if status is Status.WAITING else None,
        path=f"enrichment/{item}/{kind.value}-{work_hash(url)[:6]}.md"
        if status is Status.DONE
        else None,
    )


def media_entry(*, url=VIDEO_URL, parent=POST_HASH, item=ITEM, status=Status.DONE):
    return LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=item,
        kind=Kind.X,
        status=status,
        engine="0.2.1",
        date=FETCHED,
        at=NOW,
        job=Job.MEDIA,
        parent=parent,
        depth=1,
        path=f"enrichment/{item}/media-0.mp4" if status is Status.DONE else None,
        reason=None if status in (Status.DONE, Status.QUEUED) else "media exceeds 10MB ceiling",
        attempts=2 if status is Status.BLOCKED else None,
    )


def seed_lines(path, unit_hash=POST_HASH):
    return [
        line
        for line in path.read_text().splitlines()
        if f'"hash": "{unit_hash}"' in line and '"queued"' in line
    ]


class TestMembers:
    def test_a_post_whose_video_downloaded_reruns_with_its_identity_and_stamps(
        self, tmp_path, migration
    ):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert len(seed_lines(path)) == 1
        post = ledger.load(path)[POST_HASH]
        assert post.url == POST_URL
        assert post.item == ITEM
        assert post.kind is Kind.X
        assert post.status is Status.QUEUED
        assert post.via == "migration-16"
        assert post.rerun is True
        assert post.job is None
        assert post.parent is None
        assert post.depth is None
        assert post.engine == ENGINE
        assert post.date == TODAY
        assert post.at == NOW
        # The media child is the engine's to retire when the transcript lands.
        assert ledger.load(path)[work_hash(VIDEO_URL)].status is Status.DONE
        assert report.skipped == []
        assert report.anomalies == []
        assert report.actions[0] == (
            "seeded 1 x post rerun(s) — the fixed driver transcribes a post's video into the "
            "post's own file instead of pooling it as a file to describe"
        )
        assert report.actions[1] == (
            f"{ITEM}: the rerun transcribes the video of {POST_URL} into the post's file; when "
            "the transcript lands, the downloaded video and any description of it leave"
        )

    @pytest.mark.parametrize(
        "status", [Status.SKIPPED, Status.QUEUED, Status.BLOCKED, Status.MANUAL, Status.DEAD]
    )
    def test_the_video_childs_status_does_not_matter(self, tmp_path, migration, status):
        # Skipped over the 10MB ceiling is the common one: no file at all,
        # and the clip never heard either way.
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry(), media_entry(status=status))
        migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.QUEUED

    def test_a_digest_written_without_the_transcript_is_named_for_the_session(
        self, tmp_path, migration
    ):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        digest = tmp_path / "state" / "digests" / f"{ITEM}.md"
        digest.parent.mkdir(parents=True)
        digest.write_text("---\nid: x\n---\n", encoding="utf-8")
        write_ledger(tmp_path, post_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert report.actions[1] == (
            f"{ITEM}: the rerun transcribes the video of {POST_URL} into the post's file; when "
            "the transcript lands, the downloaded video and any description of it leave — the "
            "item's digest was written without the transcript and wants rewriting from it"
        )

    def test_a_renamed_item_reattributes_through_the_post_url(self, tmp_path, migration):
        renamed = "2026-09-20-talk-renamed-abc123"
        write_corpus_item(tmp_path, item_id=renamed)
        write_post_file(tmp_path, item=renamed)
        path = write_ledger(tmp_path, post_entry(), media_entry())
        report = migration.apply(tmp_path)
        post = ledger.load(path)[POST_HASH]
        assert post.status is Status.QUEUED
        assert post.item == renamed
        assert report.actions[1].startswith(f"{renamed}: the rerun transcribes")

    def test_a_promoted_posts_seed_keeps_its_lineage(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        lineage = work_hash("https://example.test/thread")
        path = write_ledger(tmp_path, post_entry(parent=lineage, depth=2), media_entry())
        migration.apply(tmp_path)
        post = ledger.load(path)[POST_HASH]
        assert (post.parent, post.depth) == (lineage, 2)


class TestNonMembers:
    def test_a_gif_is_never_evidence(self, tmp_path, migration):
        # x's silent loop shares the video host; it never transcribes.
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry(), media_entry(url=GIF_URL))
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []

    def test_a_photo_is_never_evidence(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry(), media_entry(url=PHOTO_URL))
        migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE

    def test_a_video_on_a_look_alike_host_is_never_evidence(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        elsewhere = "https://video.twimg.com.example.test/amplify_video/1/vid/v.mp4"
        path = write_ledger(tmp_path, post_entry(), media_entry(url=elsewhere))
        migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE

    def test_another_posts_video_is_never_this_posts_evidence(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(
            tmp_path, post_entry(), media_entry(parent=work_hash("https://x.com/i/status/9"))
        )
        migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE

    @pytest.mark.parametrize("status", [Status.MANUAL, Status.WAITING])
    def test_a_post_not_landed_done_is_left_alone(self, tmp_path, migration, status):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry(status=status), media_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is status
        assert report.actions == []

    def test_another_kinds_unit_is_never_a_member(self, tmp_path, migration):
        url = "https://example.test/talk"
        write_corpus_item(tmp_path, urls=(url,))
        path = write_ledger(
            tmp_path, post_entry(url=url, kind=Kind.WEB), media_entry(parent=work_hash(url))
        )
        migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(url)].status is Status.DONE

    def test_a_media_child_itself_never_seeds(self, tmp_path, migration):
        # A child whose own child is a video: only a page unit reruns.
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        still = media_entry(url=PHOTO_URL)
        path = write_ledger(tmp_path, still, media_entry(parent=still.hash))
        report = migration.apply(tmp_path)
        assert ledger.load(path)[still.hash].status is Status.DONE
        assert report.actions == []

    def test_a_post_whose_video_was_heard_is_left_alone(self, tmp_path, migration):
        # The file carries the video pointer the fixed driver writes.
        write_corpus_item(tmp_path)
        write_post_file(tmp_path, heard=True)
        path = write_ledger(tmp_path, post_entry(), media_entry(status=Status.SKIPPED))
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []
        assert report.skipped == []
        assert report.anomalies == []


class TestIdempotency:
    def test_an_already_queued_post_is_never_seeded_again(self, tmp_path, migration):
        # Migration 17 may have seeded the same post first, or the owner
        # requeued it: the queue already answers it.
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        later = NOW + datetime.timedelta(seconds=1)
        path = write_ledger(
            tmp_path,
            post_entry(),
            media_entry(),
            post_entry(status=Status.QUEUED, rerun=True, at=later),
        )
        lines_before = path.read_text().count("\n")
        report = migration.apply(tmp_path)
        assert path.read_text().count("\n") == lines_before
        assert report.actions == []

    def test_a_second_apply_after_the_rerun_drains_finds_nothing(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = write_ledger(tmp_path, post_entry(), media_entry())
        migration.apply(tmp_path)
        # The rerun landed the transcript and retired the video child.
        write_post_file(tmp_path, heard=True)
        later = NOW + datetime.timedelta(days=1)
        retired = dataclasses.replace(
            media_entry(), status=Status.SKIPPED, path=None, reason="superseded", at=later
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(ledger.to_line(post_entry(at=later)) + "\n")
            handle.write(ledger.to_line(retired) + "\n")
        lines_before = path.read_text().count("\n")
        report = migration.apply(tmp_path)
        assert path.read_text().count("\n") == lines_before
        assert report.actions == []
        assert report.skipped == []
        assert report.anomalies == []


class TestTolerantRead:
    def test_a_missing_ledger_is_a_noop(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert (report.actions, report.skipped, report.anomalies) == ([], [], [])

    def test_a_member_whose_file_is_gone_is_skipped_with_why(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, post_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []
        assert len(report.skipped) == 1
        assert report.skipped[0].what == f"video rerun for {POST_URL}"
        assert f"enrichment/{ITEM}/x-{POST_HASH[:6]}.md is gone" in report.skipped[0].why

    def test_an_unparseable_enrichment_file_is_an_anomaly_and_the_rest_heal(
        self, tmp_path, migration
    ):
        other_item = "2026-09-19-second-def456"
        other_url = "https://x.com/i/status/4200000000000000002"
        other_video = "https://video.twimg.com/ext_tw_video/4200000000000000008/vid/v.mp4"
        write_corpus_item(tmp_path)
        write_corpus_item(tmp_path, item_id=other_item, urls=(other_url,))
        write_post_file(tmp_path, item=other_item, url=other_url)
        torn = write_post_file(tmp_path)
        torn.write_text(f"---\nurl: {POST_URL}\nvia: fxtwitter\n", encoding="utf-8")
        path = write_ledger(
            tmp_path,
            post_entry(),
            media_entry(),
            post_entry(url=other_url, item=other_item),
            media_entry(url=other_video, parent=work_hash(other_url), item=other_item),
        )
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[POST_HASH].status is Status.DONE
        assert live[work_hash(other_url)].status is Status.QUEUED
        assert len(report.anomalies) == 1
        assert f"enrichment/{ITEM}/x-{POST_HASH[:6]}.md" in report.anomalies[0]

    def test_an_anomaly_survives_a_seedless_run(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        torn = write_post_file(tmp_path)
        torn.write_text(f"---\nurl: {POST_URL}\n", encoding="utf-8")
        write_ledger(tmp_path, post_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert len(report.anomalies) == 1

    def test_an_unparseable_ledger_line_never_stops_the_healing(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # A blank line too: union merges leave them, and neither stops the read.
        path.write_text(
            f"not json at all\n\n{ledger.to_line(post_entry())}\n{ledger.to_line(media_entry())}\n",
            encoding="utf-8",
        )
        report = migration.apply(tmp_path)
        assert len(seed_lines(path)) == 1
        assert [s.what for s in report.skipped] == ["ledger line 1"]
        assert "does not parse" in report.skipped[0].why

    def test_a_line_older_than_one_before_it_never_stops_the_read(self, tmp_path, migration):
        # A union merge interleaves two machines' lines, so a hash's older
        # line can follow its newer one; the lines after it still count.
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        earlier = NOW - datetime.timedelta(days=1)
        path = write_ledger(
            tmp_path,
            post_entry(),
            post_entry(status=Status.MANUAL, at=earlier),
            media_entry(),
        )
        migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.QUEUED

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
        path = write_ledger(tmp_path, post_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []
        assert report.skipped[0].what == f"video rerun for {ITEM}"
        assert "owner purged it\tfor good" in report.skipped[0].why

    def test_an_excluded_item_with_no_reason_says_so(self, tmp_path, migration):
        exclusions = tmp_path / "state" / "exclusions.tsv"
        exclusions.parent.mkdir(parents=True, exist_ok=True)
        exclusions.write_text(f"{ITEM}\n", encoding="utf-8")
        write_post_file(tmp_path)
        write_ledger(tmp_path, post_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert "(state/exclusions.tsv: no reason recorded)" in report.skipped[0].why

    def test_an_unclaimed_member_is_skipped_and_never_stops_the_rest(self, tmp_path, migration):
        orphan_item = "2026-09-19-orphaned-def456"
        orphan_url = "https://x.com/i/status/4200000000000000003"
        orphan_video = "https://video.twimg.com/amplify_video/4200000000000000007/vid/v.mp4"
        write_corpus_item(tmp_path)
        write_post_file(tmp_path)
        write_post_file(tmp_path, item=orphan_item, url=orphan_url)
        path = write_ledger(
            tmp_path,
            post_entry(url=orphan_url, item=orphan_item),
            media_entry(url=orphan_video, parent=work_hash(orphan_url), item=orphan_item),
            post_entry(),
            media_entry(),
        )
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.QUEUED
        assert ledger.load(path)[work_hash(orphan_url)].status is Status.DONE
        assert len(report.skipped) == 1
        assert "nothing claims this work" in report.skipped[0].why
        assert f"corpus/2026/{orphan_item}.md" in report.skipped[0].why
