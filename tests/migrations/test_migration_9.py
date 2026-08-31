"""Tests for migration 9: reseed pooled-kind media the 4-file cap refused."""

import datetime

import pytest

from dex_engine.migrations.migration_9 import _OLD_CAP_REASON, build
from dex_engine.pipeline import ledger
from dex_engine.pipeline.types import Job, Kind, LedgerEntry, Status
from dex_engine.pipeline.urls import work_hash

TODAY = datetime.date(2026, 9, 1)
NOW = datetime.datetime(2026, 9, 1, 9, 0, 0, 500000, tzinfo=datetime.UTC)
ENGINE = "0.2.0"

ITEM = "2026-08-31-carousel-abc123"
POST_URL = "https://www.instagram.com/p/DTestCode1/"
POST_HASH = work_hash(POST_URL)
X_URL = "https://x.com/i/status/1234567890"
X_HASH = work_hash(X_URL)


def media_url(index: int, code: str = "DTestCode1") -> str:
    return f"https://uuinstagram.com/images/{code}/{index}"


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


def write_exclusions(root, rows):
    path = root / "state" / "exclusions.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{item}\t{reason}\n" for item, reason in rows), encoding="utf-8")


def page_entry(  # noqa: PLR0913 — a fixture builder mirrors the entry's own fields
    *, url=POST_URL, kind=Kind.INSTAGRAM, status=Status.DONE, item=ITEM, at=NOW, rerun=False
):
    return LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=item,
        kind=kind,
        status=status,
        engine="0.1.11",
        date=datetime.date(2026, 8, 31),
        at=at,
        rerun=rerun,
        via="migration-9" if rerun else None,
    )


def media_entry(  # noqa: PLR0913 — a fixture builder mirrors the entry's own fields
    *,
    url,
    parent=POST_HASH,
    kind=Kind.INSTAGRAM,
    status=Status.SKIPPED,
    reason=_OLD_CAP_REASON,
    item=ITEM,
    at=NOW,
):
    return LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=item,
        kind=kind,
        status=status,
        engine="0.1.11",
        date=datetime.date(2026, 8, 31),
        at=at,
        job=Job.MEDIA,
        parent=parent,
        depth=1,
        reason=reason if status is Status.SKIPPED else None,
        path=f"enrichment/{item}/media-4.jpg" if status is Status.DONE else None,
    )


class TestApply:
    def test_a_truncated_carousel_requeues_the_slide_and_rewalks_the_post(
        self, tmp_path, migration
    ):
        # The field shape: a 5+-slide carousel landed four files and the
        # fifth download was refused terminally; slides 6+ were never
        # ledgered, so only a post re-walk can discover them.
        write_corpus_item(tmp_path)
        path = write_ledger(
            tmp_path,
            page_entry(),
            media_entry(url=media_url(5)),
        )
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        slide = live[work_hash(media_url(5))]
        assert slide.status is Status.QUEUED
        assert slide.rerun is True
        assert slide.via == "migration-9"
        assert slide.job is Job.MEDIA
        assert slide.parent == POST_HASH
        assert slide.depth == 1
        assert slide.engine == ENGINE
        post = live[POST_HASH]
        assert post.status is Status.QUEUED
        assert post.rerun is True
        assert post.via == "migration-9"
        assert report.actions == [
            (
                "seeded 1 cap-refused media rerun(s) (1 instagram, 0 x) and 1 instagram post "
                "re-walk(s) — the pooled cap fetches what the 4-file cap refused"
            )
        ]
        assert report.skipped == []
        assert report.anomalies == []

    def test_an_x_thread_requeues_downloads_and_never_rewalks(self, tmp_path, migration):
        # The x driver always emitted the full pooled list — only the
        # downloads were refused, so the post itself has nothing to re-do.
        write_corpus_item(tmp_path, urls=(X_URL,))
        path = write_ledger(
            tmp_path,
            page_entry(url=X_URL, kind=Kind.X),
            media_entry(url="https://pbs.example.test/photo-5.jpg", parent=X_HASH, kind=Kind.X),
            media_entry(url="https://pbs.example.test/photo-6.jpg", parent=X_HASH, kind=Kind.X),
        )
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[X_HASH].status is Status.DONE  # never reseeded
        for url in ("https://pbs.example.test/photo-5.jpg", "https://pbs.example.test/photo-6.jpg"):
            assert live[work_hash(url)].status is Status.QUEUED
            assert live[work_hash(url)].rerun is True
        assert "2 x" in report.actions[0]
        assert "0 instagram post re-walk(s)" in report.actions[0]

    def test_a_tight_cap_skip_on_an_unpooled_kind_stays(self, tmp_path, migration):
        # A web page's fifth embedded image was refused correctly, then and
        # now — the tight cap is that kind's contract, not a defect.
        write_corpus_item(tmp_path, urls=("https://example.test/page",))
        path = write_ledger(
            tmp_path,
            page_entry(url="https://example.test/page", kind=Kind.WEB),
            media_entry(
                url="https://cdn.example.test/img-5.png",
                parent=work_hash("https://example.test/page"),
                kind=Kind.WEB,
            ),
        )
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[work_hash("https://cdn.example.test/img-5.png")].status is Status.SKIPPED
        assert report.actions == []

    def test_a_pooled_backstop_skip_stays(self, tmp_path, migration):
        # `media cap (100 files) reached` is the fixed engine's own pooled
        # backstop firing — never membership.
        write_corpus_item(tmp_path)
        path = write_ledger(
            tmp_path,
            page_entry(),
            media_entry(url=media_url(101), reason="media cap (100 files) reached"),
        )
        report = migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(media_url(101))].status is Status.SKIPPED
        assert report.actions == []
        assert report.skipped == []
        assert report.anomalies == []

    def test_a_second_apply_finds_no_members(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, page_entry(), media_entry(url=media_url(5)))
        migration.apply(tmp_path)
        lines_after_first = path.read_text().count("\n")
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert path.read_text().count("\n") == lines_after_first

    def test_an_interrupted_apply_resumes_without_reseeding_the_parent(self, tmp_path, migration):
        # Crash after the parent seed, before the child: the child is still
        # a member, and the parent's queued line must not be doubled.
        write_corpus_item(tmp_path)
        later = NOW + datetime.timedelta(seconds=1)
        path = write_ledger(
            tmp_path,
            page_entry(),
            media_entry(url=media_url(5)),
            page_entry(status=Status.QUEUED, rerun=True, at=later),
        )
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[work_hash(media_url(5))].status is Status.QUEUED
        assert "1 instagram" in report.actions[0]
        assert "0 instagram post re-walk(s)" in report.actions[0]
        parent_lines = [
            line for line in path.read_text().splitlines() if f'"hash": "{POST_HASH}"' in line
        ]
        assert len(parent_lines) == 2  # the done line and the one queued seed

    def test_a_purged_item_is_never_reseeded(self, tmp_path, migration):
        # No corpus file and an exclusion on the record: the owner ruled
        # this content out, and a reseed would put the ruling back in the
        # queue.
        # Blank line and a tab inside the reason: the tolerant TSV read
        # splits on the FIRST tab, so the reason keeps its own.
        exclusions = tmp_path / "state" / "exclusions.tsv"
        exclusions.parent.mkdir(parents=True, exist_ok=True)
        exclusions.write_text(f"\n{ITEM}\towner purged it\tfor good\n", encoding="utf-8")
        path = write_ledger(tmp_path, page_entry(), media_entry(url=media_url(5)))
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[work_hash(media_url(5))].status is Status.SKIPPED
        assert live[POST_HASH].status is Status.DONE
        assert report.actions == []
        assert len(report.skipped) == 1
        assert "owner purged it\tfor good" in report.skipped[0].why

    def test_a_renamed_item_reattributes_through_the_post_url(self, tmp_path, migration):
        # The stored item is gone but a live item lists the post: that is a
        # rename, and the seeds write under the live id.
        renamed = "2026-08-31-carousel-renamed-abc123"
        write_corpus_item(tmp_path, item_id=renamed)
        path = write_ledger(tmp_path, page_entry(), media_entry(url=media_url(5)))
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[work_hash(media_url(5))].item == renamed
        assert live[POST_HASH].item == renamed
        assert report.skipped == []

    def test_an_unclaimed_member_is_skipped_with_why(self, tmp_path, migration):
        # No corpus file, no exclusion, nothing lists the post: nothing
        # claims the work, so the reseed has no item to write under.
        path = write_ledger(tmp_path, page_entry(), media_entry(url=media_url(5)))
        report = migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(media_url(5))].status is Status.SKIPPED
        assert report.actions == []
        assert len(report.skipped) == 1
        assert "nothing claims this work" in report.skipped[0].why
        assert f"corpus/2026/{ITEM}.md" in report.skipped[0].why
        assert f"cap reseed for {ITEM}" == report.skipped[0].what

    def test_a_member_whose_parent_line_is_gone_is_an_anomaly(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, media_entry(url=media_url(5)))
        report = migration.apply(tmp_path)
        assert ledger.load(path)[work_hash(media_url(5))].status is Status.SKIPPED
        assert report.actions == []
        assert len(report.anomalies) == 1
        assert POST_HASH in report.anomalies[0]

    def test_a_parse_skip_survives_a_memberless_ledger(self, tmp_path, migration):
        # No members, but a torn line: the early return still carries the
        # skip, or the session never hears the file needs repair.
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

    def test_an_unparseable_line_never_stops_the_healing(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, page_entry(), media_entry(url=media_url(5)))
        with path.open("a", encoding="utf-8") as handle:
            handle.write("not json at all\n")
        report = migration.apply(tmp_path)
        seed_lines = [
            line
            for line in path.read_text().splitlines()
            if work_hash(media_url(5)) in line and '"queued"' in line
        ]
        assert len(seed_lines) == 1
        assert any("does not parse" in s.why for s in report.skipped)
        assert any(s.what == "ledger line 3" for s in report.skipped)

    def test_a_bad_member_never_stops_the_rest(self, tmp_path, migration):
        # One anomalous member (no parent line) and one unclaimed member
        # (no item, no claim) sit BEFORE a healthy one — each is reported
        # and stepped over, never a reason to abandon the walk.
        write_corpus_item(tmp_path)
        orphan_item = "2026-08-30-orphaned-def456"
        orphan_post = "https://www.instagram.com/p/DOrphaned1/"
        path = write_ledger(
            tmp_path,
            media_entry(url=media_url(9, code="DGhostCode"), parent="feedface00"),
            page_entry(url=orphan_post, item=orphan_item),
            media_entry(
                url=media_url(5, code="DOrphaned1"),
                parent=work_hash(orphan_post),
                item=orphan_item,
            ),
            page_entry(),
            media_entry(url=media_url(5)),
        )
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[work_hash(media_url(5))].status is Status.QUEUED
        assert live[POST_HASH].status is Status.QUEUED
        assert len(report.anomalies) == 1
        assert len(report.skipped) == 1
        assert "1 instagram" in report.actions[0]

    def test_mixed_kinds_seed_together_and_only_instagram_rewalks(self, tmp_path, migration):
        # An x member ahead of an instagram one must not swallow the post
        # re-walk: a seedless parent is stepped over, not a stop sign.
        write_corpus_item(tmp_path, urls=(POST_URL, X_URL))
        path = write_ledger(
            tmp_path,
            page_entry(url=X_URL, kind=Kind.X),
            media_entry(url="https://pbs.example.test/photo-9.jpg", parent=X_HASH, kind=Kind.X),
            page_entry(),
            media_entry(url=media_url(5)),
        )
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[POST_HASH].status is Status.QUEUED
        assert live[X_HASH].status is Status.DONE
        assert report.actions == [
            (
                "seeded 2 cap-refused media rerun(s) (1 instagram, 1 x) and 1 instagram post "
                "re-walk(s) — the pooled cap fetches what the 4-file cap refused"
            )
        ]

    def test_two_slides_under_one_post_seed_the_post_once(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        path = write_ledger(
            tmp_path,
            page_entry(),
            media_entry(url=media_url(5)),
            media_entry(url=media_url(6)),
        )
        report = migration.apply(tmp_path)
        parent_lines = [
            line for line in path.read_text().splitlines() if f'"hash": "{POST_HASH}"' in line
        ]
        assert len(parent_lines) == 2  # the done line and exactly one seed
        assert "1 instagram post re-walk(s)" in report.actions[0]

    def test_a_promoted_posts_seed_keeps_its_lineage(self, tmp_path, migration):
        # A harvest-promoted post carries parent and depth of its own —
        # the re-walk seed must keep both or the line cannot exist at all
        # (parent and depth travel together).
        write_corpus_item(tmp_path)
        source_hash = work_hash("https://example.test/roundup")
        promoted = LedgerEntry(
            hash=POST_HASH,
            url=POST_URL,
            item=ITEM,
            kind=Kind.INSTAGRAM,
            status=Status.DONE,
            engine="0.1.11",
            date=datetime.date(2026, 8, 31),
            at=NOW,
            via="harvest",
            parent=source_hash,
            depth=1,
        )
        path = write_ledger(tmp_path, promoted, media_entry(url=media_url(5)))
        migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[POST_HASH].status is Status.QUEUED
        assert live[POST_HASH].parent == source_hash
        assert live[POST_HASH].depth == 1

    def test_the_live_line_wins_whatever_the_file_order(self, tmp_path, migration):
        # A union merge can leave a hash's newest line mid-file, ties are
        # broken by position, and blank or garbled lines sit anywhere — the
        # tolerant read walks the WHOLE file and resolves as `load` does.
        write_corpus_item(tmp_path)
        earlier = NOW - datetime.timedelta(seconds=1)
        lines = [
            ledger.to_line(page_entry()),
            ledger.to_line(page_entry()),  # verbatim duplicate: same at, tie on position
            ledger.to_line(page_entry(at=earlier)),  # superseded line after its successor
            "",
            "not json at all",
            ledger.to_line(media_entry(url=media_url(5))),
        ]
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        report = migration.apply(tmp_path)
        written = path.read_text().splitlines()
        assert any(work_hash(media_url(5)) in line and '"queued"' in line for line in written)
        assert any(f'"hash": "{POST_HASH}"' in line and '"queued"' in line for line in written)
        assert any(s.what == "ledger line 5" for s in report.skipped)
