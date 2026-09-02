"""Tests for migration 12: re-walk instagram posts whose stills the park dropped."""

import datetime

import pytest

from dex_engine.migrations.migration_12 import _DROPPED_FIELD, build
from dex_engine.pipeline import ledger
from dex_engine.pipeline.enrichment import render_enrichment
from dex_engine.pipeline.types import Job, Kind, LedgerEntry, Status
from dex_engine.pipeline.urls import work_hash

TODAY = datetime.date(2026, 9, 1)
NOW = datetime.datetime(2026, 9, 1, 9, 0, 0, 500000, tzinfo=datetime.UTC)
ENGINE = "0.2.0"

ITEM = "2026-08-31-carousel-abc123"
POST_URL = "https://www.instagram.com/p/DTestCode1/"
POST_HASH = work_hash(POST_URL)
FETCHED = datetime.date(2026, 8, 31)


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
        "source: manual\nchannel: inbox\nshared_by: alex\ndate: 2026-08-31\n"
        f"urls:\n{listing}"
        "kinds: [instagram]\nstatus: raw\nenrichment: []\n---\n**alex**: note\n",
        encoding="utf-8",
    )
    return path


def write_enrichment(  # noqa: PLR0913 — a fixture builder mirrors the file's own shape
    root,
    *,
    item=ITEM,
    url=POST_URL,
    unit_hash=POST_HASH,
    kind=Kind.INSTAGRAM,
    dropped=2,
    transcribed=True,
):
    """The park (or transcribed) enrichment file, written by the real renderer."""
    meta: dict[str, str | int | None] = {
        "author": "Alex (@alex)",
        "shortcode": "DTestCode1",
        "enclosure": "https://uuinstagram.com/videos/DTestCode1/1",
        "media_via": "uuinstagram.com",
    }
    if dropped is not None:
        meta[_DROPPED_FIELD] = dropped
    body = "**Alex**: a caption"
    if transcribed:
        meta["via"] = "whisper-local"
        body = f"{body}\n\n## Transcript\n\nspoken words"
    path = root / "enrichment" / item / f"{kind.value}-{unit_hash[:6]}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_enrichment(url, FETCHED, meta, body), encoding="utf-8")
    return path


def post_entry(  # noqa: PLR0913 — a fixture builder mirrors the entry's own fields
    *,
    url=POST_URL,
    kind=Kind.INSTAGRAM,
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
        engine="0.1.13",
        date=FETCHED,
        at=at,
        rerun=rerun,
        via="migration-12" if rerun else None,
        parent=parent,
        depth=depth,
        reason="silent reel — no speech to transcribe" if status is Status.MANUAL else None,
        path=f"enrichment/{item}/instagram-{work_hash(url)[:6]}.md"
        if status is Status.DONE
        else None,
    )


def media_entry(*, url, parent=POST_HASH, item=ITEM):
    return LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=item,
        kind=Kind.INSTAGRAM,
        status=Status.DONE,
        engine="0.1.13",
        date=FETCHED,
        at=NOW,
        job=Job.MEDIA,
        parent=parent,
        depth=1,
        path=f"enrichment/{item}/media-1.jpg",
    )


def seed_lines(path, unit_hash=POST_HASH):
    return [
        line
        for line in path.read_text().splitlines()
        if f'"hash": "{unit_hash}"' in line and '"queued"' in line
    ]


class TestApply:
    def test_a_transcribed_post_reseeds_with_its_identity_and_stamps(self, tmp_path, migration):
        # The field shape: a carousel with a video parked for transcription,
        # its stills dropped, the loss recorded as images_dropped — which
        # survived into the finished file when the drain landed.
        write_corpus_item(tmp_path)
        write_enrichment(tmp_path)
        path = write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert len(seed_lines(path)) == 1
        post = ledger.load(path)[POST_HASH]
        assert post.hash == POST_HASH
        assert post.url == POST_URL
        assert post.item == ITEM
        assert post.kind is Kind.INSTAGRAM
        assert post.status is Status.QUEUED
        assert post.via == "migration-12"
        assert post.rerun is True
        assert post.job is None
        assert post.parent is None
        assert post.depth is None
        assert post.engine == ENGINE
        assert post.date == TODAY
        assert post.at == NOW
        assert report.skipped == []
        assert report.anomalies == []
        assert report.actions[0] == (
            "seeded 1 instagram post re-walk(s) — the fixed driver keeps a post's stills "
            "instead of dropping them for its video"
        )

    def test_the_per_post_action_names_the_item_and_the_stills(self, tmp_path, migration):
        # The rerun rewrites content the owner may have closed by hand —
        # every consequence is stated per post, not aggregated away.
        write_corpus_item(tmp_path)
        write_enrichment(tmp_path, dropped=7)
        write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert len(report.actions) == 2
        line = report.actions[1]
        assert line.startswith(f"{ITEM}: 7 still(s) return")
        assert "transcribes the video again" in line
        assert "`enrich mark`" in line
        assert f"enrichment/{ITEM}/" in line
        assert "media-N.md" in line

    def test_a_manual_parked_reel_reseeds(self, tmp_path, migration):
        # The silent-reel case: the post never transcribed, so its park
        # file — images_dropped and all — is what stands on disk.
        write_corpus_item(tmp_path)
        write_enrichment(tmp_path, transcribed=False)
        path = write_ledger(tmp_path, post_entry(status=Status.MANUAL))
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.QUEUED
        assert len(report.actions) == 2

    def test_a_post_whose_file_never_dropped_stills_is_left_alone(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_enrichment(tmp_path, dropped=None)
        path = write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []
        assert report.skipped == []
        assert report.anomalies == []

    def test_a_post_with_no_enrichment_file_is_left_alone(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []

    def test_another_kinds_file_is_never_a_member(self, tmp_path, migration):
        # images_dropped is instagram's own record; no other kind's park
        # can be healed by an instagram re-walk.
        url = "https://www.youtube.com/watch?v=abc12345678"
        write_corpus_item(tmp_path, urls=(url,))
        write_enrichment(tmp_path, url=url, unit_hash=work_hash(url), kind=Kind.YOUTUBE)
        path = write_ledger(tmp_path, post_entry(url=url, kind=Kind.YOUTUBE))
        report = migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(url)].status is Status.DONE
        assert report.actions == []

    def test_a_media_child_never_seeds_and_its_post_seeds_once(self, tmp_path, migration):
        # The child's own enrichment dir is the item's, so the post's file
        # is in reach of the child line too — only the post unit re-walks.
        write_corpus_item(tmp_path)
        write_enrichment(tmp_path)
        slide = "https://uuinstagram.com/images/DTestCode1/2"
        path = write_ledger(tmp_path, post_entry(), media_entry(url=slide))
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[POST_HASH].status is Status.QUEUED
        assert live[work_hash(slide)].status is Status.DONE
        assert len(seed_lines(path)) == 1
        assert len(report.actions) == 2

    def test_an_already_queued_post_is_never_seeded_again(self, tmp_path, migration):
        # An apply interrupted before its rerun, or the owner's own
        # requeue: the file still records the loss, the queue already
        # answers it.
        write_corpus_item(tmp_path)
        write_enrichment(tmp_path)
        later = NOW + datetime.timedelta(seconds=1)
        path = write_ledger(
            tmp_path,
            post_entry(),
            post_entry(status=Status.QUEUED, rerun=True, at=later),
        )
        lines_before = path.read_text().count("\n")
        report = migration.apply(tmp_path)
        assert path.read_text().count("\n") == lines_before
        assert report.actions == []

    def test_a_second_apply_after_the_rerun_finds_nothing(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_enrichment(tmp_path)
        path = write_ledger(tmp_path, post_entry())
        migration.apply(tmp_path)
        # The rerun landed: the fixed driver rewrote the file, and its
        # frontmatter no longer records a loss.
        write_enrichment(tmp_path, dropped=None)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(ledger.to_line(post_entry(at=NOW + datetime.timedelta(days=1))) + "\n")
        lines_before = path.read_text().count("\n")
        report = migration.apply(tmp_path)
        assert path.read_text().count("\n") == lines_before
        assert report.actions == []
        assert report.skipped == []
        assert report.anomalies == []

    def test_a_renamed_item_reattributes_through_the_post_url(self, tmp_path, migration):
        # The stored item is gone but a live item lists the post: that is a
        # rename, and the seed writes under the live id.
        renamed = "2026-08-31-carousel-renamed-abc123"
        write_corpus_item(tmp_path, item_id=renamed)
        write_enrichment(tmp_path, item=renamed)
        path = write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        post = ledger.load(path)[POST_HASH]
        assert post.status is Status.QUEUED
        assert post.item == renamed
        assert report.skipped == []
        assert report.actions[1].startswith(f"{renamed}: 2 still(s)")

    def test_a_purged_item_is_never_reseeded(self, tmp_path, migration):
        # No corpus file and an exclusion on the record: the owner ruled
        # this content out, and a reseed would put the ruling back in the
        # queue. The park file outlives the corpus file, so the loss it
        # records still identifies the member.
        # Blank line, an unrelated row ahead of this item's, and a tab
        # inside the reason: the tolerant TSV read takes a row per line
        # and splits on the FIRST tab, so the reason keeps its own.
        exclusions = tmp_path / "state" / "exclusions.tsv"
        exclusions.parent.mkdir(parents=True, exist_ok=True)
        exclusions.write_text(
            f"\n2026-01-01-unrelated-000000\toff topic\n{ITEM}\towner purged it\tfor good\n",
            encoding="utf-8",
        )
        write_enrichment(tmp_path)
        path = write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []
        assert len(report.skipped) == 1
        assert report.skipped[0].what == f"stills reseed for {ITEM}"
        assert "owner purged it\tfor good" in report.skipped[0].why

    def test_an_unclaimed_member_is_skipped_with_why(self, tmp_path, migration):
        # No corpus file, no exclusion, nothing lists the post: nothing
        # claims the work, so the reseed has no item to write under.
        write_enrichment(tmp_path)
        path = write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.DONE
        assert report.actions == []
        assert len(report.skipped) == 1
        assert "nothing claims this work" in report.skipped[0].why
        assert f"corpus/2026/{ITEM}.md" in report.skipped[0].why

    def test_an_unparseable_enrichment_file_is_an_anomaly(self, tmp_path, migration):
        # A torn write leaves a fence that never closes — unreadable, so
        # the post is left exactly as it is, and named for the session.
        other_item = "2026-08-30-second-def456"
        other_url = "https://www.instagram.com/p/DSecondOne/"
        write_corpus_item(tmp_path)
        write_corpus_item(tmp_path, item_id=other_item, urls=(other_url,))
        write_enrichment(tmp_path, item=other_item, url=other_url, unit_hash=work_hash(other_url))
        torn = write_enrichment(tmp_path)
        torn.write_text("---\nurl: " + POST_URL + "\nimages_dropped: 2\n", encoding="utf-8")
        path = write_ledger(tmp_path, post_entry(), post_entry(url=other_url, item=other_item))
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[POST_HASH].status is Status.DONE
        assert live[work_hash(other_url)].status is Status.QUEUED
        assert len(report.anomalies) == 1
        assert f"enrichment/{ITEM}/instagram-{POST_HASH[:6]}.md" in report.anomalies[0]
        assert len(report.actions) == 2

    def test_an_unparseable_ledger_line_never_stops_the_healing(self, tmp_path, migration):
        # The torn line sits AHEAD of the post's: a union merge relocates
        # a half-written line anywhere, and the lines behind it are still
        # this migration's to heal.
        write_corpus_item(tmp_path)
        write_enrichment(tmp_path)
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"not json at all\n{ledger.to_line(post_entry())}\n", encoding="utf-8")
        report = migration.apply(tmp_path)
        assert len(seed_lines(path)) == 1
        assert any("does not parse" in s.why for s in report.skipped)
        assert any(s.what == "ledger line 1" for s in report.skipped)

    def test_an_unclaimed_member_never_stops_the_rest(self, tmp_path, migration):
        # An unclaimed member sits BEFORE a healthy one: reported and
        # stepped over, never a reason to abandon the walk.
        orphan_item = "2026-08-30-orphaned-def456"
        orphan_url = "https://www.instagram.com/p/DOrphaned1/"
        write_corpus_item(tmp_path)
        write_enrichment(tmp_path)
        write_enrichment(
            tmp_path, item=orphan_item, url=orphan_url, unit_hash=work_hash(orphan_url)
        )
        path = write_ledger(tmp_path, post_entry(url=orphan_url, item=orphan_item), post_entry())
        report = migration.apply(tmp_path)
        assert ledger.load(path)[POST_HASH].status is Status.QUEUED
        assert ledger.load(path)[work_hash(orphan_url)].status is Status.DONE
        assert len(report.skipped) == 1
        assert len(report.actions) == 2

    def test_an_anomaly_survives_a_seedless_run(self, tmp_path, migration):
        # Nothing to seed, but a file that cannot be read: the early
        # return still carries the anomaly, or the session never hears
        # the file needs repair.
        write_corpus_item(tmp_path)
        torn = write_enrichment(tmp_path)
        torn.write_text(f"---\nurl: {POST_URL}\n{_DROPPED_FIELD}: 2\n", encoding="utf-8")
        write_ledger(tmp_path, post_entry())
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert len(report.anomalies) == 1

    def test_a_parse_skip_survives_a_memberless_ledger(self, tmp_path, migration):
        # No posts at all, but a torn line: the early return still carries
        # the skip, or the session never hears the file needs repair.
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all\n", encoding="utf-8")
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert len(report.skipped) == 1
        assert report.skipped[0].what == "ledger line 1"

    def test_a_missing_ledger_is_a_noop(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []
        assert report.anomalies == []

    def test_a_promoted_posts_seed_keeps_its_lineage(self, tmp_path, migration):
        # A harvest-promoted post carries parent and depth of its own —
        # the re-walk seed must keep both or the line cannot exist at all
        # (parent and depth travel together).
        write_corpus_item(tmp_path)
        write_enrichment(tmp_path)
        source_hash = work_hash("https://example.test/roundup")
        path = write_ledger(tmp_path, post_entry(parent=source_hash, depth=1))
        migration.apply(tmp_path)
        post = ledger.load(path)[POST_HASH]
        assert post.status is Status.QUEUED
        assert post.parent == source_hash
        assert post.depth == 1

    def test_the_live_line_wins_whatever_the_file_order(self, tmp_path, migration):
        # A union merge can leave a hash's newest line mid-file, ties are
        # broken by position, and blank or garbled lines sit anywhere — the
        # tolerant read walks the WHOLE file and resolves as `load` does.
        write_corpus_item(tmp_path)
        write_enrichment(tmp_path)
        earlier = NOW - datetime.timedelta(seconds=1)
        lines = [
            ledger.to_line(post_entry()),
            ledger.to_line(post_entry()),  # verbatim duplicate: same at, tie on position
            ledger.to_line(post_entry(at=earlier)),  # superseded line after its successor
            "",
            "not json at all",
        ]
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        report = migration.apply(tmp_path)
        assert len(seed_lines(path)) == 1
        assert any(s.what == "ledger line 5" for s in report.skipped)
