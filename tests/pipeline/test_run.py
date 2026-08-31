"""Tests for pipeline/run.py: the orchestrator, with fake drivers."""

import dataclasses
import datetime
import io
import json
import os
import shutil
import ssl
import zipfile
from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from dex_engine import atomic, corpus
from dex_engine.capabilities import Capabilities
from dex_engine.drivers.file import FileDriver
from dex_engine.drivers.github import GitHubDriver
from dex_engine.drivers.podcast import PodcastDriver
from dex_engine.drivers.transport import HttpResponse
from dex_engine.drivers.web import WebDriver
from dex_engine.exclude import run_exclude
from dex_engine.lint import run_lint
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.classify import ProviderInputError, classify_connection
from dex_engine.pipeline.digest import item_digest
from dex_engine.pipeline.enrichment import _yaml_value, read_enrichment, render_enrichment
from dex_engine.pipeline.ownership import work_identity
from dex_engine.pipeline.run import (
    _SNIFF_PREFIX_BYTES,
    MAX_BLOCKED_ATTEMPTS,
    MAX_DEPTH,
    MAX_URLS_PER_ITEM,
    MEDIA_MAX_FILES,
    RunContext,
    _sniff_bytes,
    is_drainable,
    is_media_file,
    no_providers,
)
from dex_engine.pipeline.types import (
    Asset,
    Cap,
    Config,
    Content,
    Format,
    Instance,
    Job,
    Kind,
    LedgerEntry,
    MediaFetch,
    Missing,
    Need,
    NeedsCapability,
    Outcome,
    Redetected,
    Refused,
    Status,
    Unusable,
    WorkUnit,
)
from dex_engine.pipeline.urls import work_hash
from tests.capabilities.conftest import FakeExtractor, FakeTranscriber, fixture_bytes
from tests.conftest import FakeDriver
from tests.drivers.conftest import FakeGh as FakeGhApi
from tests.drivers.conftest import FakeTransport, fixture_text, gh_contents
from tests.pipeline.test_issues import FakeGh
from tests.test_atomic import failing_fdopen

TODAY = datetime.date(2026, 8, 20)
NOW = datetime.datetime(2026, 8, 20, 9, 30, 15, 250000, tzinfo=datetime.UTC)
ITEM = "2026-08-19-example-55ad7b"
URL = "https://example.test/post"

# Real image leads: the media stage names a downloaded file after its bytes.
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"


def write_item(
    instance: Instance,
    item_id: str = ITEM,
    urls: list[str] | None = None,
    media: list[str] | None = None,
    kinds: list[str] | None = None,
) -> Path:
    item = corpus.CorpusItem(
        id=item_id,
        source="discord",
        channel="general",
        shared_by="Alex",
        date=datetime.date(2026, 8, 19),
        urls=urls if urls is not None else [URL],
        kinds=kinds if kinds is not None else ["web"],
        media=media if media is not None else [],
        body="the owner's note\n",
    )
    path = instance.corpus_dir / "2026" / f"{item_id}.md"
    corpus.write_item(path, item)
    return path


def refuse_download(url, _cache_dir, _stem):
    raise AssertionError(f"unexpected audio download of {url!r}")


def refuse_gh(args):
    # Hermetic default: the filer's soft edge turns this into an
    # "issue filing failed" note — never a network call, never a crash.
    raise OSError(f"no gh in tests (called with {args[:2]})")


def make_ctx(  # noqa: PLR0913 — the builder mirrors RunContext's seams
    instance: Instance,
    driver: FakeDriver,
    *,
    config: Config | None = None,
    drivers=None,
    today=None,
    now=None,
    engine_version: str = "0.2.0",
    transport=None,
    provider_available=no_providers,
    capabilities=None,
    download_audio=refuse_download,
    sleep=None,
    gh=None,
) -> RunContext:
    return RunContext(
        instance=instance,
        config=config if config is not None else Config(),
        drivers=drivers if drivers is not None else [driver],
        today=today if today is not None else (lambda: TODAY),
        now=now if now is not None else (lambda: NOW),
        engine_version=engine_version,
        transport=transport if transport is not None else FakeTransport({}),
        provider_available=provider_available,
        capabilities=capabilities,
        download_audio=download_audio,
        sleep=sleep if sleep is not None else (lambda _seconds: None),
        gh=gh if gh is not None else refuse_gh,
    )


def entry_for(ctx: RunContext, url: str = URL) -> LedgerEntry:
    return ledger.load(ctx.instance.ledger_path)[work_hash(url)]


class TestSeedAndDone:
    def test_captured_urls_are_ledger_entries_from_birth(self, instance):
        write_item(instance)
        driver = FakeDriver()
        run_mod.run(make_ctx(instance, driver))
        lines = [
            json.loads(line)
            for line in instance.ledger_path.read_text().split("\n")
            if line.strip()
        ]
        assert [line["status"] for line in lines] == ["queued", "done"]  # birth, then outcome

    def test_done_writes_deterministic_output_and_entry(self, instance):
        write_item(instance)
        driver = FakeDriver()
        ctx = make_ctx(instance, driver)
        run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.status is Status.DONE
        assert entry.path == f"enrichment/{ITEM}/web-{entry.hash[:6]}.md"
        assert entry.title == "t"
        assert entry.engine == "0.2.0"
        assert entry.date == TODAY
        assert (instance.root / entry.path).exists()

    def test_item_frontmatter_refreshed_via_corpus(self, instance):
        path = write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        item = corpus.read_item(path)
        assert item.status == "enriched"
        assert item.enrichment == [f"web-{work_hash(URL)[:6]}.md"]
        assert item.body == "the owner's note\n"  # byte-exact through the refresh

    def test_report_renders_via_the_surface(self, instance):
        write_item(instance)
        report = run_mod.run(make_ctx(instance, FakeDriver()))
        assert report.startswith("## Enrich run — 1 unit processed")
        assert ITEM in report
        assert "1 new" in report

    def test_limit_processes_that_many_and_leaves_the_remainder_queued(self, instance):
        urls = [f"https://example.test/p{n}" for n in range(3)]
        write_item(instance, urls=urls)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx, limit=2)
        statuses = sorted(e.status.value for e in ledger.load(instance.ledger_path).values())
        assert statuses == ["done", "done", "queued"]  # the cohort drains across runs
        run_mod.run(ctx)
        statuses = {e.status for e in ledger.load(instance.ledger_path).values()}
        assert statuses == {Status.DONE}

    def test_a_runs_ledger_writes_share_one_append_handle(self, instance, monkeypatch):
        # Six lines (three births, three outcomes), one open for append:
        # the per-line reopen is the regression this pins against. Every
        # line is flushed as written, so the single open changes no
        # reader's view — the report's read-back below still sees all six.
        urls = [f"https://example.test/p{n}" for n in range(3)]
        write_item(instance, urls=urls)
        opens: list[str] = []
        real_open = Path.open

        def counting_open(self, mode="r", *args, **kwargs):
            if self == instance.ledger_path and "a" in mode:
                opens.append(mode)
            return real_open(self, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", counting_open)
        run_mod.run(make_ctx(instance, FakeDriver()))
        lines = [line for line in instance.ledger_path.read_text().split("\n") if line.strip()]
        assert len(lines) == 6
        assert len(opens) == 1

    def test_a_mid_run_ledger_rewrite_never_orphans_later_outcomes(self, instance):
        # compact/exclude/sync in another terminal during a long run: the
        # rewrite is a temp file plus one replace, a NEW inode at the same
        # path. The run's held handle must follow the path — a handle that
        # keeps the replaced inode writes every later outcome into an
        # orphan, the report claims work the ledger never took, and the
        # read-back misdiagnoses the loss as a backwards clock.
        urls = [f"https://example.test/p{n}" for n in range(3)]
        write_item(instance, urls=urls)
        fetched: list[str] = []

        def fetch(unit):
            fetched.append(unit.url)
            if len(fetched) == 2:
                ledger.compact(instance.ledger_path)  # another terminal's compact
            return Content(meta={}, body="b" * 400)

        report = run_mod.run(make_ctx(instance, FakeDriver(fetch_fn=fetch)))
        loaded = ledger.load(instance.ledger_path)
        assert all(loaded[work_hash(url)].status is Status.DONE for url in urls)
        assert "did not take effect" not in report

    def test_a_failed_output_write_is_never_counted_as_new_material(self, instance):
        # A file squatting where the enrichment directory belongs: the
        # mkdir raises, the unit ledgers `error` — and the report must not
        # say "1 new enrichment file" and list the item under Needs
        # writing up for a write that never landed.
        write_item(instance)
        (instance.enrichment_dir / ITEM).write_text("a file where the directory belongs")
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.status is Status.ERROR
        assert "new enrichment file" not in report
        assert "Needs writing up" not in report

    def test_two_items_sharing_a_url_dedupe_by_hash(self, instance):
        write_item(instance, "2026-08-19-first-aaaaaa")
        write_item(instance, "2026-08-19-second-bbbbbb")
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        assert len(entries) == 1
        assert entries[work_hash(URL)].item == "2026-08-19-first-aaaaaa"

    def test_one_malformed_capture_url_parks_that_url_never_aborts(self, instance):
        bad_url = "https://[bad-ipv6"
        write_item(instance, urls=[bad_url, URL])
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.run(ctx)  # completes — no abort
        entries = ledger.load(instance.ledger_path)
        bad = entries[work_hash(bad_url)]
        assert bad.status is Status.MANUAL
        assert "unfetchable capture URL" in (bad.reason or "")
        assert entries[work_hash(URL)].status is Status.DONE  # the rest proceeded
        assert "unfetchable capture URL" in report  # parked rows are printed

    def test_a_driver_canonical_that_raises_parks_that_url_never_aborts(self, instance):
        # Seeding caught only ValueError, so a driver's canonical raising
        # anything else took the whole run down — while the ownership map
        # broad-caught the same call and resolved the unit on the raw URL's
        # hash. One URL, two identities and two outcomes; now both paths
        # read a refusal the same way and the park keys the raw URL, which
        # is exactly what `work_identity` falls back to.
        bad_url = "https://example.test/explodes"

        class Exploding(FakeDriver):
            def canonical(self, url: str) -> str:
                if url == bad_url:
                    raise TypeError("expected str, got tuple")
                return super().canonical(url)

        write_item(instance, urls=[bad_url, URL])
        ctx = make_ctx(instance, Exploding())
        report = run_mod.run(ctx)  # completes — no abort
        entries = ledger.load(instance.ledger_path)
        parked = entries[work_hash(bad_url)]
        assert parked.status is Status.MANUAL
        assert "unfetchable capture URL" in (parked.reason or "")
        assert "TypeError" in (parked.reason or "")
        assert entries[work_hash(URL)].status is Status.DONE  # the rest proceeded
        assert "unfetchable capture URL" in report
        assert work_identity(bad_url, [Exploding()]) == parked.hash

    def test_one_malformed_corpus_item_is_skipped_and_reported_never_fatal(self, instance):
        write_item(instance)
        bad = instance.corpus_dir / "2026" / "2026-08-19-broken-ffffff.md"
        bad.write_text("---\nid: broken\n")  # unterminated frontmatter
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.run(ctx)  # completes — no abort
        assert entry_for(ctx).status is Status.DONE  # the readable item enriched
        flat = " ".join(report.split())  # notes wrap at width
        assert "corpus item corpus/2026/2026-08-19-broken-ffffff.md is unreadable" in flat
        assert "unterminated frontmatter" in flat
        assert "repair the file by hand" in flat
        # Every seeding verb shares the containment through seed_from_corpus:
        transcribe_report = run_mod.run_transcribe(make_ctx(instance, FakeDriver()))
        assert "is unreadable" in " ".join(transcribe_report.split())

    def test_non_utf8_corpus_file_is_skipped_and_reported_never_fatal(self, instance):
        write_item(instance)
        bad = instance.corpus_dir / "2026" / "2026-08-19-binary-eeeeee.md"
        bad.write_bytes(b"---\nid: x\n---\n\xff\xfe not text")
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.run(ctx)  # completes — no abort
        assert entry_for(ctx).status is Status.DONE  # the readable item enriched
        flat = " ".join(report.split())
        assert "corpus item corpus/2026/2026-08-19-binary-eeeeee.md is unreadable" in flat

    def test_directory_matching_the_corpus_glob_is_skipped_never_fatal(self, instance):
        write_item(instance)
        (instance.corpus_dir / "2026" / "notes.md").mkdir()
        report = run_mod.run(make_ctx(instance, FakeDriver()))  # completes — no abort
        flat = " ".join(report.split())
        assert "corpus item corpus/2026/notes.md is unreadable" in flat

    def test_escaping_media_path_parks_manual_at_seed_never_read(self, instance):
        # `media:` frontmatter is owner-editable data: an absolute path or a
        # ../ climb must park as a bad seed, not have its bytes read into
        # the pipeline.
        outside = instance.root.parent / "secret.pdf"
        outside.write_bytes(b"%PDF-1.4 not for the corpus")
        write_item(instance, media=[str(outside), "../secret.pdf"])
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        for work_key in (f"file:{outside}", "file:../secret.pdf"):
            entry = entries[work_hash(work_key)]
            assert entry.status is Status.MANUAL
            assert entry.kind is Kind.FILE
            assert "outside the instance root" in (entry.reason or "")
        assert "outside the instance root" in report  # parked rows are printed

    def test_media_path_with_nul_byte_parks_manual_and_the_run_completes(self, instance):
        # A NUL byte cannot resolve to any path; the poisoned entry must
        # park as a bad seed while the rest of the corpus still runs.
        write_item(instance, media=["media/bad\x00name.pdf"])
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)  # completes — no abort
        entries = ledger.load(instance.ledger_path)
        poisoned = entries[work_hash("file:media/bad\x00name.pdf")]
        assert poisoned.status is Status.MANUAL
        assert poisoned.kind is Kind.FILE
        assert entries[work_hash(URL)].status is Status.DONE  # the rest proceeded

    def test_missing_media_file_parks_manual_at_seed(self, instance):
        # A stated path no file answers to (an import wrote the attachment
        # under a sanitized name and recorded the other one). Nothing
        # fetches it and nothing describes it, so the parked line is the
        # only place the gap is ever said out loud.
        write_item(instance, media=["media/aaaaaa/shot.png"])
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash("file:media/aaaaaa/shot.png")]
        assert entry.status is Status.MANUAL
        assert entry.kind is Kind.FILE
        assert "not on disk" in (entry.reason or "")
        assert "Needs you" in report
        assert "stated media file is not on disk" in report
        assert "Describe these" not in report  # never countable describe work

    def test_a_missing_document_path_parks_rather_than_queueing_a_file_unit(self, instance):
        # A document extension is the one shape that reached the file
        # driver anyway, seeded off the name alone — a queued unit for a
        # file nobody can extract. It parks like every other absent path.
        write_item(instance, media=["media/aaaaaa/report.pdf"])
        run_mod.run(make_ctx(instance, FakeDriver()))
        entry = ledger.load(instance.ledger_path)[work_hash("file:media/aaaaaa/report.pdf")]
        assert entry.status is Status.MANUAL
        assert entry.format is None

    def test_the_parked_phantom_is_recorded_once(self, instance):
        # The line is the state: a second run finds the unit already held
        # and re-records nothing, or every run re-parks what it just parked.
        write_item(instance, media=["media/aaaaaa/shot.png"])
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        first = ledger.load(instance.ledger_path)[work_hash("file:media/aaaaaa/shot.png")]
        report = run_mod.run(ctx)
        assert ledger.load(instance.ledger_path)[work_hash("file:media/aaaaaa/shot.png")] == first
        assert "not on disk" not in report  # a held unit is not this run's news

    def test_unreadable_media_file_is_noted_and_seeding_continues(self, instance, monkeypatch):
        # Seeding's one file read: an unreadable media file (permissions, a
        # vanished LFS object) is judgment work, never fatal to the run.
        media = instance.root / "media" / "aaaaaa" / "paper.pdf"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"%PDF-1.4 fine on disk")
        write_item(instance, media=["media/aaaaaa/paper.pdf"])

        def unreadable(_path):
            raise PermissionError("Operation not permitted")

        monkeypatch.setattr(run_mod, "_sniff_bytes", unreadable)
        report = run_mod.run(make_ctx(instance, FakeDriver()))  # completes — no abort
        flat = " ".join(report.split())
        assert "media file media/aaaaaa/paper.pdf could not be read" in flat
        assert "Operation not permitted" in flat
        entries = ledger.load(instance.ledger_path)
        assert work_hash("file:media/aaaaaa/paper.pdf") not in entries  # nothing guessed
        assert entries[work_hash(URL)].status is Status.DONE  # the rest proceeded

    def test_enrichment_frontmatter_quotes_unsafe_values(self, instance):
        write_item(instance)
        fetch = lambda _unit: Content(meta={"title": "Ledgers: a Field Report"}, body="b" * 400)  # noqa: E731
        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        content = (instance.root / entry_for(ctx).path).read_text()
        assert 'title: "Ledgers: a Field Report"' in content
        # The url line is a value like any other — a work key can carry a
        # colon-space (`file:media/plan: v2.pdf`) and unparse the block.
        assert content.startswith(f'---\nurl: "{URL}"\nfetched: 2026-08-20\n')


class TestFrontmatterRefresh:
    """The derived-frontmatter contract: status/enrichment converge with disk."""

    def test_media_only_item_converges_on_the_next_run(self, instance):
        # A kinds:[image] capture seeds no drainable unit; the session
        # hand-writes the media description — the run still reconciles it.
        item_id = "2026-08-19-photo-abc123"
        path = write_item(
            instance, item_id, urls=[], kinds=["image"], media=[f"media/{item_id}/photo.jpg"]
        )
        photo = instance.root / "media" / item_id / "photo.jpg"
        photo.parent.mkdir(parents=True)
        photo.write_bytes(PNG_BYTES)
        enrichment = instance.enrichment_dir / item_id / "media-0.md"
        enrichment.parent.mkdir(parents=True)
        enrichment.write_text("what the photo depicts, all legible text\n", encoding="utf-8")
        run_mod.run(make_ctx(instance, FakeDriver()))
        item = corpus.read_item(path)
        assert item.status == "enriched"
        assert item.enrichment == ["media-0.md"]
        assert item.body == "the owner's note\n"  # byte-exact through the refresh

    def test_mark_refreshes_the_owning_item_in_the_same_call(self, instance):
        path = write_item(instance)
        fetch = lambda _unit: Unusable(evidence="paywalled")  # noqa: E731
        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        assert corpus.read_item(path).status == "raw"
        healed = instance.enrichment_dir / ITEM / "web-healed.md"
        healed.parent.mkdir(parents=True)
        healed.write_text("hand-fetched content\n", encoding="utf-8")
        run_mod.mark(ctx, URL, Status.DONE, path=f"enrichment/{ITEM}/web-healed.md")
        item = corpus.read_item(path)
        assert item.status == "enriched"
        assert item.enrichment == ["web-healed.md"]

    def test_record_pass_refreshes_the_items_frontmatter(self, instance):
        item_id = "2026-08-19-scan-def456"
        path = write_item(instance, item_id, urls=[], kinds=["image"])
        item_dir = instance.enrichment_dir / item_id
        item_dir.mkdir(parents=True)
        (item_dir / "media-0.md").write_text("described by hand\n", encoding="utf-8")
        run_mod.record_pass(make_ctx(instance, FakeDriver()), item_id, "digest")
        item = corpus.read_item(path)
        assert item.status == "enriched"
        assert item.enrichment == ["media-0.md"]

    def test_correct_frontmatter_is_not_rewritten(self, instance):
        untouched = write_item(instance, "2026-08-19-note-aaa111", urls=[], kinds=["text"])
        write_item(instance)
        before = untouched.stat().st_mtime_ns
        run_mod.run(make_ctx(instance, FakeDriver()))
        assert untouched.stat().st_mtime_ns == before

    def test_unlistable_enrichment_filename_never_kills_the_run(self, instance):
        # A session may hand-write an enrichment file whose NAME the corpus
        # schema cannot hold; the full-corpus sweep must note it — not die
        # corpus-wide on every verb until someone renames the file.
        write_item(instance)
        other = write_item(instance, "2026-08-19-other-bbb222", urls=["https://example.test/o"])
        bad_dir = instance.enrichment_dir / ITEM
        bad_dir.mkdir(parents=True)
        (bad_dir / "Notes, part 2.md").write_text("hand-written\n", encoding="utf-8")
        report = run_mod.run(make_ctx(instance, FakeDriver()))  # completes — no abort
        flat = " ".join(report.split())
        assert f"item {ITEM}: an enrichment file cannot be listed" in flat
        assert "Notes, part 2.md" in flat
        assert "rename it" in flat
        assert corpus.read_item(other).status == "enriched"  # the sweep went on

    def test_mark_still_heals_when_the_listing_cannot_refresh(self, instance):
        write_item(instance)
        fetch = lambda _unit: Unusable(evidence="paywalled")  # noqa: E731
        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        item_dir = instance.enrichment_dir / ITEM
        item_dir.mkdir(parents=True)
        (item_dir / "web-healed.md").write_text("hand-fetched content\n", encoding="utf-8")
        (item_dir / "[draft].md").write_text("also hand-written\n", encoding="utf-8")
        confirmation = run_mod.mark(ctx, URL, Status.DONE, path=f"enrichment/{ITEM}/web-healed.md")
        assert confirmation.endswith("done")  # the heal landed, quietly
        assert entry_for(ctx).status is Status.DONE

    VIDEO_URL = "https://example.test/video"

    def _mixed_item(self, instance, second: Outcome):
        """An item whose blog unit lands and whose video unit gets ``second``."""
        path = write_item(instance, urls=[URL, self.VIDEO_URL])

        def fetch(unit):
            if unit.url == self.VIDEO_URL:
                return second
            return Content(meta={"title": "t"}, body="substantial body " * 30)

        return path, make_ctx(instance, FakeDriver(fetch_fn=fetch))

    def _waiting_video(self) -> NeedsCapability:
        return NeedsCapability(
            need=Need.TRANSCRIBE,
            meta={"title": "v"},
            body="## Description\n\nwhat the video covers",
            reason="no captions available",
        )

    def test_an_outstanding_unit_keeps_the_whole_item_raw(self, instance):
        path, ctx = self._mixed_item(instance, self._waiting_video())
        run_mod.run(ctx)
        item = corpus.read_item(path)
        assert item.status == "raw"  # the transcript is still owed
        assert len(item.enrichment) == 2  # both units wrote what they had

    def test_the_item_flips_to_enriched_when_the_last_unit_lands(self, instance):
        path, ctx = self._mixed_item(instance, self._waiting_video())
        run_mod.run(ctx)
        assert corpus.read_item(path).status == "raw"
        video_out = f"enrichment/{ITEM}/web-{work_hash(self.VIDEO_URL)[:6]}.md"
        run_mod.mark(ctx, self.VIDEO_URL, Status.DONE, path=video_out)
        assert corpus.read_item(path).status == "enriched"

    def test_a_dead_unit_never_holds_the_item_hostage(self, instance):
        dead = Missing(evidence="404")
        path, ctx = self._mixed_item(instance, dead)
        run_mod.run(ctx)
        assert corpus.read_item(path).status == "enriched"  # confirmed gone owes nothing

    def test_skipped_and_manual_units_differ(self, instance):
        skipped = Unusable(evidence="deliberately not fetched", rescuable=False)
        path, ctx = self._mixed_item(instance, skipped)
        run_mod.run(ctx)
        assert corpus.read_item(path).status == "enriched"
        run_mod.mark(ctx, self.VIDEO_URL, Status.MANUAL, reason="reopened by hand")
        assert corpus.read_item(path).status == "raw"

    def test_mark_and_pass_work_on_an_item_with_outstanding_units(self, instance):
        path, ctx = self._mixed_item(instance, self._waiting_video())
        run_mod.run(ctx)
        assert run_mod.record_pass(ctx, ITEM, "harvest").endswith(ITEM)
        confirmation = run_mod.mark(ctx, URL, Status.SKIPPED, reason="superseded by hand")
        assert confirmation.endswith("skipped")
        assert corpus.read_item(path).status == "raw"  # the video is still waiting

    def _landed_video(self) -> Content:
        return Content(meta={"title": "v"}, body="the video body " * 30)

    def test_a_capped_run_leaves_the_item_raw_with_work_still_queued(self, instance):
        # --limit drains part of the cohort (as does a cap-deferred rerun):
        # the undrained unit is QUEUED, not parked, and queued work is
        # still owed — the item must not read as complete just because
        # nothing failed.
        path, ctx = self._mixed_item(instance, self._landed_video())
        run_mod.run(ctx, limit=1)
        assert corpus.read_item(path).status == "raw"
        run_mod.run(ctx)  # the rest of the cohort
        assert corpus.read_item(path).status == "enriched"

    def test_an_unreadable_ledger_leaves_the_derived_status_alone(self, instance):
        # The item has to be ENRICHED before the ledger goes bad, or the
        # assertion holds on a fixture that was already raw and says
        # nothing about what "leaves it alone" means.
        path, ctx = self._mixed_item(instance, self._landed_video())
        run_mod.run(ctx)
        assert corpus.read_item(path).status == "enriched"
        instance.ledger_path.write_text("{not json at all\n", encoding="utf-8")
        assert run_mod.record_pass(ctx, ITEM, "digest").endswith(ITEM)  # the write stands
        assert corpus.read_item(path).status == "enriched"

    def test_mark_for_an_excluded_items_unit_still_heals(self, instance):
        path = write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        path.unlink()  # the item was excluded; its ledger unit remains
        confirmation = run_mod.mark(ctx, URL, Status.SKIPPED, reason="item excluded")
        assert confirmation.endswith("skipped")
        assert entry_for(ctx).status is Status.SKIPPED


class TestIncompleteItemsOnTheReport:
    """The run says WHY an item is still raw — never left to inference."""

    VIDEO_URL = "https://example.test/video"

    def _ctx(self, instance, second: Outcome):
        write_item(instance, urls=[URL, self.VIDEO_URL])

        def fetch(unit):
            if unit.url == self.VIDEO_URL:
                return second
            return Content(meta={"title": "t"}, body="substantial body " * 30)

        return make_ctx(instance, FakeDriver(fetch_fn=fetch))

    def _run_capturing(self, ctx, monkeypatch) -> tuple[str, dict]:
        """Run, returning the report AND the payload the surface was handed.

        The negative cases assert on the payload: "no row for this item" is
        the run's behaviour, and a whole-report substring only tests it for
        as long as the heading happens to spell the same word.
        """
        captured: dict[str, dict] = {}
        rendered = run_mod.surfaces.render

        def spy(surface, payload):
            captured[surface] = dict(payload)
            return rendered(surface, payload)

        monkeypatch.setattr(run_mod.surfaces, "render", spy)
        return run_mod.run(ctx), captured["enrich-report"]

    def _fetch_capturing(self, ctx, urls, monkeypatch) -> tuple[str, dict]:
        """The same spy over ``enrich fetch`` — the other run that reports."""
        captured: dict[str, dict] = {}
        rendered = run_mod.surfaces.render

        def spy(surface, payload):
            captured[surface] = dict(payload)
            return rendered(surface, payload)

        monkeypatch.setattr(run_mod.surfaces, "render", spy)
        return run_mod.fetch_urls(ctx, ITEM, urls), captured["enrich-report"]

    def test_the_report_names_the_shape_of_what_is_missing(self, instance):
        waiting = NeedsCapability(
            need=Need.TRANSCRIBE, meta={"title": "v"}, reason="no captions available"
        )
        report = run_mod.run(self._ctx(instance, waiting))
        flat = " ".join(report.split())
        assert "### Not finished — 1 item stays raw until every unit lands" in flat
        assert f"**{ITEM}** ↳ 1 of 2 units landed — 1 waiting on transcription" in flat

    def test_a_complete_item_gets_no_line(self, instance, monkeypatch):
        dead = Missing(evidence="404")
        report, payload = self._run_capturing(self._ctx(instance, dead), monkeypatch)
        assert "incomplete" not in payload  # the key is omitted, not merely empty
        assert "still raw until every unit lands" not in report

    def test_the_payload_carries_the_counts(self, instance, monkeypatch):
        waiting = NeedsCapability(need=Need.TRANSCRIBE, reason="no captions available")
        _report, payload = self._run_capturing(self._ctx(instance, waiting), monkeypatch)
        assert payload["incomplete"] == [
            {
                "item": ITEM,
                "landed": 1,
                "total": 2,
                "outstanding": [{"status": "waiting", "needs": "transcribe", "count": 1}],
            }
        ]

    def test_an_untouched_item_is_not_listed(self, instance, monkeypatch):
        # The standing view is `enrich status`; the run reports on what it
        # touched, so a long-parked item does not repeat every run.
        waiting = NeedsCapability(need=Need.TRANSCRIBE, reason="no captions available")
        ctx = self._ctx(instance, waiting)
        run_mod.run(ctx)
        report, payload = self._run_capturing(ctx, monkeypatch)  # nothing drainable now
        assert "incomplete" not in payload
        assert "still raw until every unit lands" not in report

    def test_cap_fire_markers_are_not_counted_as_units(self, instance, monkeypatch):
        # A fetch batch that overran the URL cap recorded refused work.
        # Counted as landed, the shape read "15 of 16 units landed" for an
        # item that only ever admitted twelve.
        captured = [f"https://hub.test/page-{n}" for n in range(MAX_URLS_PER_ITEM)]
        write_item(instance, urls=captured)

        def fetch(unit):
            if unit.url == captured[0]:
                return NeedsCapability(need=Need.TRANSCRIBE, reason="no captions available")
            return Content(meta={}, body="b" * 400)

        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        refused = [f"https://hub.test/extra-{n}" for n in range(4)]
        report, payload = self._fetch_capturing(ctx, refused, monkeypatch)
        capped = [e for e in ledger.load(instance.ledger_path).values() if e.cap is not None]
        assert len(capped) == len(refused)
        assert payload["incomplete"] == [
            {
                "item": ITEM,
                "landed": MAX_URLS_PER_ITEM - 1,
                "total": MAX_URLS_PER_ITEM,  # the admitted cohort, refusals excluded
                "outstanding": [{"status": "waiting", "needs": "transcribe", "count": 1}],
            }
        ]
        flat = " ".join(report.split())
        assert f"**{ITEM}** ↳ 11 of 12 units landed — 1 waiting on transcription" in flat

    def test_the_other_sections_still_render(self, instance):
        waiting = NeedsCapability(need=Need.TRANSCRIBE, reason="no captions available")
        report = run_mod.run(self._ctx(instance, waiting))
        assert "### Needs writing up — 1 item has new material" in report
        assert "### Waiting on the engine — 1 entry it retries by itself" in report


class TestReEntryCaps:
    """The caps bound promotion, and promotion is `enrich fetch` alone."""

    def test_the_depth_cap_refuses_the_fifth_hop_of_a_real_chain(self, instance):
        # Walked the way a session walks one: fetch the page, promote the
        # link it names, fetch THAT under --parent. Depths 1..4 admit; the
        # fifth is out of the queue's reach and says so.
        write_item(instance, urls=["https://chain.test/0"])
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        parent = work_hash("https://chain.test/0")
        for depth in range(1, MAX_DEPTH + 1):
            url = f"https://chain.test/{depth}"
            run_mod.fetch_urls(ctx, ITEM, [url], parent=parent)
            landed = ledger.load(instance.ledger_path)[work_hash(url)]
            assert landed.status is Status.DONE
            assert landed.depth == depth
            parent = landed.hash
        beyond = f"https://chain.test/{MAX_DEPTH + 1}"
        report = run_mod.fetch_urls(ctx, ITEM, [beyond], parent=parent)
        refused = ledger.load(instance.ledger_path)[work_hash(beyond)]
        assert refused.status is Status.SKIPPED
        assert refused.cap is Cap.DEPTH
        assert refused.depth == MAX_DEPTH + 1
        assert refused.forced is False
        # The owner asked, so the refusal is answered — honestly, with no
        # route it does not have.
        flat = " ".join(report.split())
        assert f"not fetched — {beyond}: depth cap ({MAX_DEPTH}) reached" in flat
        assert "no --force route" in flat

    def test_force_does_not_reach_past_the_depth_bound(self, instance):
        # --force is the URL budget's override. Depth is a hard bound
        # and offering --force against it would promise what it cannot do.
        write_item(instance, urls=["https://chain.test/0"])
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        parent = work_hash("https://chain.test/0")
        for depth in range(1, MAX_DEPTH + 1):
            url = f"https://chain.test/{depth}"
            run_mod.fetch_urls(ctx, ITEM, [url], parent=parent)
            parent = ledger.load(instance.ledger_path)[work_hash(url)].hash
        beyond = f"https://chain.test/{MAX_DEPTH + 1}"
        run_mod.fetch_urls(ctx, ITEM, [beyond], parent=parent, force=True)
        refused = ledger.load(instance.ledger_path)[work_hash(beyond)]
        assert refused.status is Status.SKIPPED  # still refused
        assert refused.cap is Cap.DEPTH
        assert refused.forced is False  # nothing was waived

    def test_the_url_cap_is_per_item(self, instance):
        # The budget bounds how much of the web ONE item drags in: an item
        # standing at its cap says nothing about the item beside it.
        other = "2026-08-19-second-bbbbbb"
        write_item(instance, urls=[f"https://hub.test/page-{n}" for n in range(MAX_URLS_PER_ITEM)])
        write_item(instance, other, urls=["https://elsewhere.test/root"])
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        run_mod.fetch_urls(ctx, ITEM, ["https://hub.test/extra"])
        run_mod.fetch_urls(ctx, other, ["https://elsewhere.test/docs"])
        entries = ledger.load(instance.ledger_path)
        refused = entries[work_hash("https://hub.test/extra")]
        assert refused.status is Status.SKIPPED
        assert refused.cap is Cap.URL_REQUESTED
        assert entries[work_hash("https://elsewhere.test/docs")].status is Status.DONE

    def test_a_capture_is_not_a_re_entry_the_caps_read(self, instance):
        # The caps bound re-entry. Every captured URL is depth 0, the
        # queue's own front door, and what the owner shared is not a
        # promotion for a bound to turn away.
        captured = [f"https://hub.test/page-{n}" for n in range(MAX_URLS_PER_ITEM + 3)]
        write_item(instance, urls=captured)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        assert len(entries) == len(captured)
        assert all(e.status is Status.DONE for e in entries.values())
        assert not [e for e in entries.values() if e.cap is not None]


class TestParking:
    def test_waiting_parks_with_reason_until_a_provider_appears(self, instance, flippable_provider):
        # The FlippableProvider auto-drain: a waiting cohort ignores
        # runs until available() flips, then drains through its driver.
        write_item(instance)
        calls = {"n": 0}

        def fetch(_unit):
            calls["n"] += 1
            if calls["n"] == 1:
                return NeedsCapability(need=Need.EXTRACT, reason="no extractor for this format")
            return Content(meta={}, body="b" * 400)

        driver = FakeDriver(fetch_fn=fetch)
        ctx = make_ctx(instance, driver, provider_available=flippable_provider)
        report = run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.status is Status.WAITING
        assert entry.needs is Need.EXTRACT
        assert entry.reason == "no extractor for this format"
        assert "no extractor for this format" in report  # parked rows are printed

        run_mod.run(ctx)  # provider still unavailable — waiting ignores runs
        assert calls["n"] == 1

        flippable_provider.ok = True
        run_mod.run(ctx)
        assert entry_for(ctx).status is Status.DONE

    def test_blocked_retries_then_escalates_to_manual_at_five_attempts(self, instance):
        write_item(instance)
        fetch = lambda _unit: Refused(evidence="HTTP 403")  # noqa: E731
        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        for expected_attempts in range(1, MAX_BLOCKED_ATTEMPTS):
            run_mod.run(ctx)
            entry = entry_for(ctx)
            assert entry.status is Status.BLOCKED
            assert entry.attempts == expected_attempts
        run_mod.run(ctx)  # the 5th failure
        entry = entry_for(ctx)
        assert entry.status is Status.MANUAL
        assert entry.reason == "still blocked after 5 attempts — HTTP 403"
        run_mod.run(ctx)  # manual is parked for judgment, never mechanical
        assert entry_for(ctx).status is Status.MANUAL

    def test_provider_input_error_maps_to_manual(self, instance):
        write_item(instance)

        def fetch(_unit):
            raise ProviderInputError("corrupt input from https://secret.test/x")

        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.status is Status.MANUAL
        assert "secret.test" not in (entry.reason or "")

    def test_error_path_scrubs_and_retries_only_on_newer_engine(self, instance):
        write_item(instance)

        def fetch(_unit):
            raise RuntimeError("exploded at /Users/owner/base fetching https://secret.test/a")

        driver = FakeDriver(fetch_fn=fetch)
        ctx = make_ctx(instance, driver)
        report = run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.status is Status.ERROR
        assert "RuntimeError" in (entry.error or "")
        assert "/Users/owner" not in (entry.error or "")
        assert "secret.test" not in (entry.error or "")
        assert "secret.test" not in report

        run_mod.run(ctx)  # same engine — retrying a deterministic bug is waste
        assert len(driver.fetched) == 1
        newer = make_ctx(instance, driver, engine_version="0.2.1")
        run_mod.run(newer)
        assert len(driver.fetched) == 2

    def test_engine_bug_after_the_fetch_ledgers_error_never_aborts(self, instance):
        # The per-unit try covers ALL per-unit processing: here the
        # OUTPUT WRITE fails (the item's enrichment path is a file), and the
        # run must ledger `error` and carry on.
        write_item(instance)
        (instance.enrichment_dir / ITEM).write_text("a file where a directory belongs")
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.run(ctx)  # completes — no crash
        entry = entry_for(ctx)
        assert entry.status is Status.ERROR
        assert entry.error  # scrubbed message recorded
        assert "## Enrich run" in report

    def test_partial_registries_park_manual_never_crash(self, instance):
        # Every work-unit kind has a driver in the shipped registry; a
        # registry that lacks one (tests, exotic wiring) parks honestly.
        podcast = LedgerEntry(
            hash=work_hash("https://pod.example.test/ep1"),
            url="https://pod.example.test/ep1",
            item=ITEM,
            kind=Kind.PODCAST,
            status=Status.QUEUED,
            engine="0.2.0",
            date=TODAY,
        )
        ledger.append(instance.ledger_path, podcast)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[podcast.hash]
        assert entry.status is Status.MANUAL
        assert entry.reason == "no driver for kind 'podcast' in this registry"

    def test_file_detection_reroutes_and_never_reaches_the_web_driver(self, instance):
        pdf_url = "https://example.test/whitepaper"
        write_item(instance, urls=[pdf_url])
        transport = FakeTransport(
            {pdf_url: HttpResponse(status=200, content_type="application/pdf", body=b"")}
        )
        driver = FakeDriver()
        ctx = make_ctx(instance, driver, transport=transport)
        run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash(pdf_url)]
        assert entry.kind is Kind.FILE
        assert str(entry.format) == "pdf"
        assert entry.status is Status.MANUAL  # this registry has no file driver
        assert driver.fetched == []  # never handed to the web driver


class TestOutcomeMapping:
    """The one total match: what a driver FOUND maps onto status here only.

    One test per arm, each pinning the status, the carried reason, and the
    side effect that makes the arm's meaning distinct — so re-routing any
    arm onto any other arm's action kills at least one of these.
    """

    def apply(self, instance: Instance, outcome: Outcome, **ctx_kwargs) -> RunContext:
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver(fetch_fn=lambda _unit: outcome), **ctx_kwargs)
        run_mod.run(ctx)
        return ctx

    def test_content_lands_done_with_output_and_title(self, instance):
        ctx = self.apply(instance, Content(meta={"title": "T"}, body="b" * 400))
        entry = entry_for(ctx)
        assert entry.status is Status.DONE
        assert entry.path == f"enrichment/{ITEM}/web-{entry.hash[:6]}.md"
        assert entry.title == "T"
        assert (instance.root / str(entry.path)).is_file()
        assert entry.reason is None

    def test_missing_is_the_only_road_to_dead(self, instance):
        ctx = self.apply(instance, Missing(evidence="HTTP 404"))
        entry = entry_for(ctx)
        assert entry.status is Status.DEAD
        assert entry.reason == "HTTP 404"

    def test_a_transient_refusal_takes_the_blocked_lifecycle(self, instance):
        ctx = self.apply(instance, Refused(evidence="HTTP 429"))
        entry = entry_for(ctx)
        assert entry.status is Status.BLOCKED
        assert entry.attempts == 1
        assert entry.reason == "HTTP 429"

    def test_a_permanent_refusal_parks_manual_with_no_attempts(self, instance):
        evidence = "payment/login required (HTTP 402)"
        ctx = self.apply(instance, Refused(evidence=evidence, permanent=True))
        entry = entry_for(ctx)
        assert entry.status is Status.MANUAL
        assert entry.attempts is None  # retrying can never change the answer
        assert entry.reason == evidence

    def test_rescuable_unusable_parks_manual(self, instance):
        ctx = self.apply(instance, Unusable(evidence="thin-extraction"))
        entry = entry_for(ctx)
        assert entry.status is Status.MANUAL
        assert entry.reason == "thin-extraction"

    def test_unrescuable_unusable_closes_skipped(self, instance):
        evidence = "github root url — nothing to fetch"
        ctx = self.apply(instance, Unusable(evidence=evidence, rescuable=False))
        entry = entry_for(ctx)
        assert entry.status is Status.SKIPPED
        assert entry.reason == evidence
        # A skip owes nothing: the item completes instead of parking raw,
        # which is the whole distance between skipped and manual here.
        item = corpus.read_item(ctx.instance.corpus_dir / "2026" / f"{ITEM}.md")
        assert item.status == "enriched"

    def test_needs_capability_parks_waiting_and_writes_the_partial_content(self, instance):
        outcome = NeedsCapability(
            need=Need.TRANSCRIBE,
            meta={"title": "ep"},
            body="## Notes\n\nresolved show notes",
            reason="episode resolved — audio awaits transcription",
        )
        ctx = self.apply(instance, outcome)
        entry = entry_for(ctx)
        assert entry.status is Status.WAITING
        assert entry.needs is Need.TRANSCRIBE
        assert entry.reason == "episode resolved — audio awaits transcription"
        park = instance.enrichment_dir / ITEM / f"web-{entry.hash[:6]}.md"
        assert park.is_file()  # written now, completed by the drain
        assert entry.path is None  # not an output — the unit still owes its work

    def test_a_bodyless_needs_capability_writes_no_park_file(self, instance):
        ctx = self.apply(instance, NeedsCapability(need=Need.EXTRACT, reason="no extractor"))
        entry = entry_for(ctx)
        assert entry.status is Status.WAITING
        assert entry.needs is Need.EXTRACT
        assert not (instance.enrichment_dir / ITEM).exists()

    def test_redetected_relabels_the_unit_and_drains_the_corrected_kind(self, instance):
        write_item(instance)
        web = FakeDriver(
            kind=Kind.WEB, fetch_fn=lambda _u: Redetected(kind=Kind.FILE, format=Format.PDF)
        )
        file_driver = FakeDriver(
            kind=Kind.FILE, fetch_fn=lambda _u: Content(meta={}, body="b" * 400)
        )
        ctx = make_ctx(instance, web, drivers=[web, file_driver])
        run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.kind is Kind.FILE
        assert entry.format is Format.PDF
        assert entry.status is Status.DONE
        assert entry.via == "sniff"
        assert len(web.fetched) == 1  # the correction re-routed, never re-fetched as web


class TestHttpOnlySources:
    """The TLS-failure http fallback: only for sources actually shared as http."""

    HTTP_URL = "http://legacy.example.test/post"
    CANONICAL = "https://legacy.example.test/post"

    @staticmethod
    def tls_refusal() -> Outcome:
        # Through the real classifier, so the typed tls marker is the one
        # the production path carries — never a hand-built lookalike.
        return classify_connection(ssl.SSLError(1, "TLSV1_ALERT_PROTOCOL_VERSION")).to_outcome()

    def scheme_split_driver(self) -> FakeDriver:
        """TLS-refuses every https fetch; serves content over http."""

        def fetch(unit: WorkUnit) -> Outcome:
            if unit.url.startswith("https://"):
                return self.tls_refusal()
            return Content(meta={"title": "t"}, body="substantial body " * 30)

        return FakeDriver(fetch_fn=fetch)

    def test_http_shared_source_refetches_over_http_and_lands(self, instance):
        write_item(instance, urls=[self.HTTP_URL])
        driver = self.scheme_split_driver()
        ctx = make_ctx(instance, driver)
        report = run_mod.run(ctx)
        assert [unit.url for unit in driver.fetched] == [self.CANONICAL, self.HTTP_URL]
        entry = entry_for(ctx, self.CANONICAL)
        assert entry.status is Status.DONE
        # Identity and ledger semantics unchanged: the line still carries
        # the canonical https URL; only the wire request downgraded.
        assert entry.url == self.CANONICAL
        assert entry.path is not None
        assert (instance.root / entry.path).exists()
        # The retry is visible on the report — it otherwise shows an https
        # URL the owner never shared.
        assert "refetched over http" in report

    def test_https_shared_source_never_downgrades_on_tls_failure(self, instance):
        write_item(instance, urls=[self.CANONICAL])
        driver = self.scheme_split_driver()
        ctx = make_ctx(instance, driver)
        report = run_mod.run(ctx)
        assert [unit.url for unit in driver.fetched] == [self.CANONICAL]  # no http attempt
        entry = entry_for(ctx, self.CANONICAL)
        assert entry.status is Status.BLOCKED
        assert entry.reason is not None
        assert entry.reason.startswith("TLS failure")
        assert "refetched over http" not in report

    def test_a_non_tls_refusal_never_downgrades_even_when_http_shared(self, instance):
        write_item(instance, urls=[self.HTTP_URL])
        driver = FakeDriver(fetch_fn=lambda _u: Refused(evidence="HTTP 403"))
        ctx = make_ctx(instance, driver)
        run_mod.run(ctx)
        assert [unit.url for unit in driver.fetched] == [self.CANONICAL]
        assert entry_for(ctx, self.CANONICAL).status is Status.BLOCKED

    def test_a_promoted_http_url_falls_back_and_lands(self, instance):
        # A promotion never lives in frontmatter, so the frontmatter
        # license can never cover it: the http-share fact is recorded on
        # the ledger line at the admission door, and it is what lets the
        # TLS fallback fire — unrecorded, the unit parked blocked→manual
        # forever.
        write_item(instance)  # the capture itself is ordinary https

        def fetch(unit: WorkUnit) -> Outcome:
            if unit.url == self.CANONICAL:
                return self.tls_refusal()
            return Content(meta={"title": "t"}, body="substantial body " * 30)

        driver = FakeDriver(fetch_fn=fetch)
        ctx = make_ctx(instance, driver)
        run_mod.run(ctx)
        report = run_mod.fetch_urls(ctx, ITEM, [self.HTTP_URL])
        entry = entry_for(ctx, self.CANONICAL)
        assert entry.status is Status.DONE
        assert entry.http_shared is True
        assert self.HTTP_URL in [unit.url for unit in driver.fetched]
        assert "refetched over http" in report

    def test_a_promoted_https_url_never_downgrades_on_tls_failure(self, instance):
        write_item(instance)

        def fetch(unit: WorkUnit) -> Outcome:
            if unit.url == self.CANONICAL:
                return self.tls_refusal()
            return Content(meta={"title": "t"}, body="substantial body " * 30)

        driver = FakeDriver(fetch_fn=fetch)
        ctx = make_ctx(instance, driver)
        run_mod.run(ctx)
        run_mod.fetch_urls(ctx, ITEM, [self.CANONICAL])
        entry = entry_for(ctx, self.CANONICAL)
        assert entry.status is Status.BLOCKED
        assert entry.http_shared is False
        assert self.HTTP_URL not in [unit.url for unit in driver.fetched]

    def test_the_promotion_flag_licenses_the_redrain(self, instance):
        # The flag survives on every superseding line, so a blocked retry
        # in a LATER run — where nothing rebuilds the promotion — is still
        # licensed to downgrade.
        write_item(instance)
        healed = {"up": False}

        def fetch(unit: WorkUnit) -> Outcome:
            if unit.url == self.CANONICAL:
                return self.tls_refusal()
            if unit.url == self.HTTP_URL and not healed["up"]:
                return self.tls_refusal()
            return Content(meta={"title": "t"}, body="substantial body " * 30)

        driver = FakeDriver(fetch_fn=fetch)
        ctx = make_ctx(instance, driver)
        run_mod.run(ctx)
        run_mod.fetch_urls(ctx, ITEM, [self.HTTP_URL])  # both schemes down: parks blocked
        blocked = entry_for(ctx, self.CANONICAL)
        assert blocked.status is Status.BLOCKED
        assert blocked.http_shared is True
        healed["up"] = True
        run_mod.run(make_ctx(instance, driver))  # a fresh run's redrain
        entry = entry_for(ctx, self.CANONICAL)
        assert entry.status is Status.DONE
        assert entry.http_shared is True

    def test_the_fallback_fetch_takes_the_normal_outcome_road(self, instance):
        # An http retry that ALSO fails classifies exactly as a normal
        # fetch would — same outcomes, same statuses.
        write_item(instance, urls=[self.HTTP_URL])

        def fetch(unit: WorkUnit) -> Outcome:
            if unit.url.startswith("https://"):
                return self.tls_refusal()
            return Missing(evidence="HTTP 404")

        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        entry = entry_for(ctx, self.CANONICAL)
        assert entry.status is Status.DEAD
        assert entry.reason == "HTTP 404"


class TestRerun:
    def seed_rerun(self, ctx: RunContext) -> None:
        entry = entry_for(ctx)
        requeued = dataclasses.replace(
            entry, status=Status.QUEUED, path=None, title=None, rerun=True, via="migration-2"
        )
        ledger.append(ctx.instance.ledger_path, requeued)

    def test_unchanged_rerun_does_not_rewrite_and_is_not_new_material(self, instance):
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        out = instance.root / entry_for(ctx).path
        before = (out.read_text(), out.stat().st_mtime_ns)

        self.seed_rerun(ctx)
        report = run_mod.run(ctx)
        assert (out.read_text(), out.stat().st_mtime_ns) == before  # byte-compare held
        assert "Needs writing up" not in report  # nothing new to write up
        assert "Already stored" in report  # but the unit is still accounted for
        assert entry_for(ctx).status is Status.DONE

    def test_an_explicit_fetch_of_stored_urls_never_reads_as_nothing_happened(self, instance):
        # The field defect: `enrich fetch` on two URLs the item's harvest had
        # already fetched printed "2 units processed / done 2" directly above
        # "Nothing to report from this run: no new material" — the run
        # contradicting itself over content sitting on disk. The session that
        # hit it only noticed by listing the enrichment directory by hand.
        write_item(instance)
        extra = ["https://example.test/one", "https://example.test/two"]
        driver = FakeDriver(
            fetch_fn=lambda unit: Content(meta={}, body=f"stable prose for {unit.url} " * 20)
        )
        run_mod.run(make_ctx(instance, driver))
        run_mod.fetch_urls(make_ctx(instance, driver), ITEM, extra)  # first promotion

        report = run_mod.fetch_urls(make_ctx(instance, driver), ITEM, extra, force=True)
        assert "Nothing to report from this run" not in report
        assert "Already stored — 2 units re-fetched to what was on disk" in report
        assert ITEM in report
        assert "Needs writing up" not in report  # accounted for, not queued for writing

    def test_a_rerun_that_really_changed_is_still_a_rewrite(self, instance):
        # The other side of the counter: `unchanged` must not swallow a
        # rewrite that genuinely replaced the stored body.
        write_item(instance)
        run_mod.run(make_ctx(instance, FakeDriver()))
        richer = FakeDriver(fetch_fn=lambda _unit: Content(meta={}, body="a different body " * 40))
        ctx = make_ctx(instance, richer)
        self.seed_rerun(ctx)
        report = run_mod.run(ctx)
        assert "1 rewritten" in report
        assert "Already stored" not in report

    def test_a_rerun_that_halves_the_stored_body_keeps_the_stored_file(self, instance):
        # The field defect: a rerun's live fetch was refused, wayback served
        # the member-only preview, and the run traded a complete article for
        # the stub — reported as new material.
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        done = entry_for(ctx)
        out = instance.root / done.path
        before = (out.read_text(), out.stat().st_mtime_ns)

        stub = FakeDriver(fetch_fn=lambda _unit: Content(meta={"title": "s"}, body="preview " * 20))
        ctx = make_ctx(instance, stub)
        self.seed_rerun(ctx)
        report = run_mod.run(ctx)
        assert (out.read_text(), out.stat().st_mtime_ns) == before  # stored body stands
        entry = entry_for(ctx)
        assert entry.status is Status.DONE
        assert entry.path == done.path
        assert entry.title == done.title  # the stub's title never lands either
        assert "kept the stored body" in report
        assert "delete " in report  # the way through, if the shrink is the truth
        assert "rewritten" not in report
        assert "Already stored" in report  # accounted for, not new material

    def test_a_modest_shrink_is_still_a_rewrite(self, instance):
        # The guard is for a body that lost half of itself, not for an edit:
        # a trimmed page must keep landing or every correction parks.
        write_item(instance)
        run_mod.run(make_ctx(instance, FakeDriver()))
        original = len("substantial body " * 30)
        trimmed = FakeDriver(
            fetch_fn=lambda _unit: Content(meta={}, body="x" * (original * 3 // 5))
        )
        ctx = make_ctx(instance, trimmed)
        self.seed_rerun(ctx)
        report = run_mod.run(ctx)
        assert "1 rewritten" in report
        assert "kept the stored body" not in report

    def test_a_squatting_neighbour_does_not_trigger_the_keep(self, instance):
        # The stored file must prove it is this unit's own by the URL it
        # records — a file at this name holding another URL's content is
        # not content the guard protects, and the write proceeds over it.
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        entry_hash = work_hash(ctx.drivers[0].canonical(URL))
        out = instance.enrichment_dir / ITEM / f"web-{entry_hash[:6]}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            render_enrichment("https://elsewhere.test/other", TODAY, {}, "another page " * 200),
            encoding="utf-8",
        )
        small = FakeDriver(fetch_fn=lambda _unit: Content(meta={}, body="short but mine " * 5))
        report = run_mod.run(make_ctx(instance, small))
        fields, body = read_enrichment(out)
        assert fields["url"] == URL  # the squatter was overwritten, not kept
        assert "short but mine" in body
        assert "kept the stored body" not in report

    def test_unchanged_rerun_on_a_later_day_still_compares_equal(self, instance):
        # The fetched: stamp changes every run by definition — the mask must
        # hold across a date change or every rerun would report as changed.
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        out = instance.root / entry_for(ctx).path
        before = (out.read_bytes(), out.stat().st_mtime_ns)

        self.seed_rerun(ctx)
        later = make_ctx(instance, FakeDriver(), today=lambda: datetime.date(2026, 9, 1))
        report = run_mod.run(later)
        assert (out.read_bytes(), out.stat().st_mtime_ns) == before  # bytes untouched
        assert "Needs writing up" not in report
        assert "Already stored" in report

    def test_a_failed_rewrite_leaves_the_enrichment_whole(self, instance, monkeypatch):
        # A plain write truncates before it writes: an interrupted run left an
        # enrichment file whose frontmatter fence never closes, taking its
        # thread markers and its re-fetch pointer with it.
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        out = instance.root / str(entry_for(ctx).path)
        intact = out.read_text()

        self.seed_rerun(ctx)
        rewritten = FakeDriver(
            fetch_fn=lambda _unit: Content(meta={"title": "t"}, body="a different body " * 30)
        )
        failing_fdopen(monkeypatch)
        run_mod.run(make_ctx(instance, rewritten))
        assert out.read_text() == intact
        assert [p.name for p in out.parent.iterdir()] == [out.name]  # no temp orphan

    @given(
        pages=st.dictionaries(
            keys=st.integers(min_value=0, max_value=30),
            values=st.text(alphabet="abcdefgh .\n", min_size=1, max_size=120),
            min_size=1,
            max_size=4,
        )
    )
    @settings(max_examples=20, deadline=None)
    def test_identical_rerun_is_byte_idempotent(self, tmp_path_factory, pages):
        # The property behind the fixed examples above: over generated
        # ledgers and fetch results, an identical rerun leaves every output
        # byte-identical (no rewrite at all) and mints no new units — only
        # audit-trail lines for hashes that already exist.
        root = tmp_path_factory.mktemp("rerun-property")
        instance = Instance(root=root)
        for directory in (
            instance.corpus_dir,
            instance.state_dir,
            instance.enrichment_dir,
            instance.cache_dir,
        ):
            directory.mkdir()
        urls = {f"https://example.test/p{n}": body for n, body in pages.items()}
        write_item(instance, urls=list(urls))

        def fetch(unit):
            return Content(meta={"title": "t"}, body=urls[unit.url])

        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        item_dir = instance.enrichment_dir / ITEM
        snapshot = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in item_dir.iterdir()
        }
        before = ledger.load(instance.ledger_path)
        for entry in before.values():
            ledger.append(
                instance.ledger_path,
                dataclasses.replace(
                    entry,
                    status=Status.QUEUED,
                    path=None,
                    title=None,
                    rerun=True,
                    via="migration-2",
                ),
            )

        run_mod.run(make_ctx(instance, FakeDriver(fetch_fn=fetch)))
        after = ledger.load(instance.ledger_path)
        assert {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in item_dir.iterdir()
        } == snapshot
        assert set(after) == set(before)  # no new units minted, audit lines only
        assert all(entry.status is Status.DONE for entry in after.values())

    def test_changed_rerun_overwrites_in_place_never_duplicates(self, instance):
        write_item(instance)
        body = {"text": "first body " * 40}
        fetch = lambda _unit: Content(meta={}, body=body["text"])  # noqa: E731
        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)

        body["text"] = "rewritten body " * 40
        self.seed_rerun(ctx)
        report = run_mod.run(ctx)
        item_dir = instance.enrichment_dir / ITEM
        files = sorted(p.name for p in item_dir.glob("*.md"))
        assert files == [f"web-{work_hash(URL)[:6]}.md"]  # overwrite, never a second file
        assert "rewritten body" in (item_dir / files[0]).read_text()
        assert "1 rewritten" in report


class TestSupersededOutputDrop:
    """A landing drops the unit's earlier output whatever name it carries."""

    def test_a_hand_named_output_for_the_same_url_leaves_on_the_landing(self, instance):
        # The field defect: a unit healed by hand pre-migration carried a
        # name outside the engine's <kind>-<hash6> set; the migration-seeded
        # rerun landed the engine-named file beside it and the item listed
        # two views of one unit forever.
        write_item(instance)
        item_dir = instance.enrichment_dir / ITEM
        item_dir.mkdir(parents=True, exist_ok=True)
        healed = item_dir / "healed-by-hand.md"
        healed.write_text(f'---\nurl: "{URL}"\nfetched: "2026-07-01"\n---\n\nthe old view\n')
        run_mod.run(make_ctx(instance, FakeDriver()))
        assert not healed.exists()
        assert sorted(p.name for p in item_dir.glob("*.md")) == [f"web-{work_hash(URL)[:6]}.md"]

    def test_a_neighbours_output_stays_whatever_it_is_named(self, instance):
        # The URL is the proof of ownership: another unit's file in the same
        # directory records another URL and is never this landing's to drop.
        write_item(instance)
        item_dir = instance.enrichment_dir / ITEM
        item_dir.mkdir(parents=True, exist_ok=True)
        neighbour = item_dir / "web-aaaaaa.md"
        neighbour.write_text('---\nurl: "https://elsewhere.test/page"\n---\n\ntheir view\n')
        run_mod.run(make_ctx(instance, FakeDriver()))
        assert neighbour.exists()

    def test_an_unreadable_candidate_is_left_alone_and_stops_nothing(self, instance):
        # A file that cannot be read is not proof of anything — it stays,
        # the landing that met it still completes, and the walk keeps
        # going: a stale twin sorting AFTER the unreadable file still
        # leaves (the walk is sorted so the order is a fact, not a fluke).
        write_item(instance)
        item_dir = instance.enrichment_dir / ITEM
        item_dir.mkdir(parents=True, exist_ok=True)
        garbled = item_dir / "a-garbled.md"
        garbled.write_bytes(b"\xff\xfe broken bytes")
        stale = item_dir / "z-healed-by-hand.md"
        stale.write_text(f'---\nurl: "{URL}"\n---\n\nthe old view\n')
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        assert garbled.exists()
        assert not stale.exists()
        assert entry_for(ctx).status is Status.DONE


class TestIsMediaFile:
    """The one predicate: the cap counts it, the digest lists it, lint compares it."""

    @pytest.mark.parametrize(
        ("name", "media"),
        [
            ("media-0.png", True),
            ("media-12.jpg", True),
            ("a1b2c3-asset-0.png", True),
            # The session's written description of a capture, not a file a
            # page can embed.
            ("media-0.md", False),
            # An atomic write killed before its replace: half a file.
            ("media-0.png.a1b2c3d4.tmp", False),
            ("a1b2c3-asset-0.png.a1b2c3d4.tmp", False),
            ("web-abc123.md", False),
            ("transcript.txt", False),
        ],
    )
    def test_the_media_family(self, tmp_path, name, media):
        path = tmp_path / name
        path.write_bytes(b"bytes")
        assert is_media_file(path) is media

    def test_a_directory_wearing_a_media_name_is_not_a_media_file(self, tmp_path):
        (tmp_path / "media-0.png").mkdir()
        assert is_media_file(tmp_path / "media-0.png") is False


class TestMediaStage:
    IMG1 = "https://cdn.example.test/a.png"
    IMG2 = "https://cdn.example.test/b.jpg"

    def media_fetch(self, urls):
        def fetch(_unit):
            return Content(meta={}, body="b" * 400, media=list(urls))

        return fetch

    def test_media_downloads_are_ledgered_with_parents_kind(self, instance):
        write_item(instance)
        transport = FakeTransport(
            {
                self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"png"),
                self.IMG2: HttpResponse(status=200, content_type="image/jpeg", body=b"jpg"),
            }
        )
        driver = FakeDriver(kind=Kind.X, fetch_fn=self.media_fetch([self.IMG1, self.IMG2]))
        ctx = make_ctx(instance, driver, transport=transport)
        run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        media = entries[work_hash(self.IMG1)]
        assert media.job is Job.MEDIA
        assert media.kind is Kind.X  # the parent's kind
        assert media.parent == work_hash(URL)
        assert media.status is Status.DONE
        assert media.path == f"enrichment/{ITEM}/media-0.png"
        assert (instance.root / media.path).read_bytes() == b"png"
        assert entries[work_hash(self.IMG2)].path == f"enrichment/{ITEM}/media-1.jpg"

    def test_a_downloaded_media_file_lands_atomically(self, instance, monkeypatch):
        # A media file is content, not a scratch artifact: a run killed
        # mid-download must not leave a half-image standing where the
        # ledger says a whole one is. Asserting the seam, because a plain
        # write_bytes leaves no evidence behind once it succeeds.
        written: list[Path] = []
        real = atomic.write_bytes

        def spy(path, data):
            written.append(path)
            real(path, data)

        monkeypatch.setattr(run_mod.atomic, "write_bytes", spy)
        write_item(instance)
        transport = FakeTransport(
            {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"png")}
        )
        driver = FakeDriver(kind=Kind.X, fetch_fn=self.media_fetch([self.IMG1]))
        run_mod.run(make_ctx(instance, driver, transport=transport))
        standing = instance.root / f"enrichment/{ITEM}/media-0.png"
        assert standing.read_bytes() == b"png"
        assert standing in written

    def test_a_failed_media_write_leaves_the_standing_file_whole(self, instance, monkeypatch):
        write_item(instance)
        transport = FakeTransport(
            {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"png")}
        )
        driver = FakeDriver(kind=Kind.X, fetch_fn=self.media_fetch([self.IMG1]))
        run_mod.run(make_ctx(instance, driver, transport=transport))
        standing = instance.root / f"enrichment/{ITEM}/media-0.png"
        before = sorted(p.name for p in standing.parent.iterdir())

        failing_fdopen(monkeypatch)
        with pytest.raises(OSError, match="No space left"):
            run_mod.atomic.write_bytes(standing, b"truncated")
        assert standing.read_bytes() == b"png"
        # No half-written temp left beside it.
        assert sorted(p.name for p in standing.parent.iterdir()) == before

    def test_media_config_none_ledgers_the_emit_and_rests_the_unit(self, instance):
        # `none` gates the FETCH, never the emit. Dropping a driver's
        # media URLs at the emit site left no ledger line, no note, and no
        # recovery when the config flipped back — the media was simply
        # gone. The emitted unit now rests exactly like the redrain's:
        # ledgered, unfetched, no attempts burned, the withheld-cohort
        # note fired once.
        write_item(instance)
        driver = FakeDriver(fetch_fn=self.media_fetch([self.IMG1]))
        transport = FakeTransport(
            {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"png")}
        )
        report = run_mod.run(
            make_ctx(
                instance, driver, config=Config(media_fetch=MediaFetch.NONE), transport=transport
            )
        )
        resting = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert resting.job is Job.MEDIA
        assert resting.status is Status.QUEUED
        assert resting.attempts is None
        assert resting.parent == work_hash(URL)
        assert ("GET", self.IMG1) not in transport.calls
        assert not (instance.root / f"enrichment/{ITEM}/media-0.png").exists()
        assert "media_fetch is `none`" in report  # the withheld cohort, noted once

    def test_a_rested_media_emit_drains_when_the_config_flips_back(self, instance):
        write_item(instance)
        driver = FakeDriver(fetch_fn=self.media_fetch([self.IMG1]))
        transport = FakeTransport(
            {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"png")}
        )
        run_mod.run(
            make_ctx(
                instance, driver, config=Config(media_fetch=MediaFetch.NONE), transport=transport
            )
        )
        run_mod.run(make_ctx(instance, driver, transport=transport))  # `lead` again
        drained = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert drained.status is Status.DONE
        assert (instance.root / f"enrichment/{ITEM}/media-0.png").read_bytes() == b"png"

    def test_media_config_none_gates_the_redrain_too(self, instance):
        # Run 1 under `lead`: the download 503s and the media unit parks
        # blocked, an ordinary ledgered unit now.
        write_item(instance)
        outage = HttpResponse(status=503, content_type="text/html", body=b"")
        driver = FakeDriver(fetch_fn=self.media_fetch([self.IMG1]))
        run_mod.run(make_ctx(instance, driver, transport=FakeTransport({self.IMG1: outage})))
        parked = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert parked.status is Status.BLOCKED

        # The owner sets `none`; the redrain must not download it — none
        # means none on every path, and the unit rests exactly where it
        # parked: same status, no attempts burned, nothing on disk.
        healed = FakeTransport(
            {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"png")}
        )
        report = run_mod.run(
            make_ctx(instance, driver, config=Config(media_fetch=MediaFetch.NONE), transport=healed)
        )
        resting = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert resting.status is Status.BLOCKED
        assert resting.attempts == 1
        assert ("GET", self.IMG1) not in healed.calls
        assert not (instance.root / f"enrichment/{ITEM}/media-0.png").exists()
        assert "media_fetch" in report

        # Turned back on, the unit resumes cleanly through the redrain.
        run_mod.run(make_ctx(instance, driver, transport=healed))
        assert ledger.load(instance.ledger_path)[work_hash(self.IMG1)].status is Status.DONE
        assert (instance.root / f"enrichment/{ITEM}/media-0.png").read_bytes() == b"png"

    def _park_blocked_media(self, instance) -> FakeDriver:
        """One media unit parked ``blocked`` by a 503 under an active config."""
        write_item(instance)
        outage = HttpResponse(status=503, content_type="text/html", body=b"")
        driver = FakeDriver(fetch_fn=self.media_fetch([self.IMG1]))
        run_mod.run(make_ctx(instance, driver, transport=FakeTransport({self.IMG1: outage})))
        assert ledger.load(instance.ledger_path)[work_hash(self.IMG1)].status is Status.BLOCKED
        return driver

    def test_a_resting_media_unit_waits_on_the_owner_not_the_engine(self, instance):
        # Under `none` the drain defers every media job, so the standing
        # view's "Waiting on the engine — it retries by itself" was false:
        # the unit waits on the owner turning media_fetch back on, and the
        # row must say so.
        driver = self._park_blocked_media(instance)
        report = run_mod.status_report(
            make_ctx(instance, driver, config=Config(media_fetch=MediaFetch.NONE))
        )
        assert "Waiting on the engine" not in report
        yours = report.split("### Needs you", 1)[1]
        assert self.IMG1 in yours
        assert "media_fetch is `none` — stays parked until it is turned back on" in yours
        assert "attempt 1 of 5" not in report  # retry framing, and nothing retries

    def test_a_parked_media_unit_under_an_active_config_still_waits_on_the_engine(self, instance):
        driver = self._park_blocked_media(instance)
        report = run_mod.status_report(make_ctx(instance, driver))
        assert "### Waiting on the engine — 1 entry it retries by itself" in report
        assert "attempt 1 of 5" in report

    def test_a_manual_media_unit_keeps_its_own_reason_under_none(self, instance):
        # `manual` media rows already wait on the owner, and flipping the
        # config resumes nothing for them — their stated reason stands.
        write_item(instance)
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(self.IMG1),
                url=self.IMG1,
                item=ITEM,
                kind=Kind.WEB,
                status=Status.MANUAL,
                engine="0.2.0",
                date=datetime.date(2026, 8, 19),
                job=Job.MEDIA,
                parent=work_hash(URL),
                depth=1,
                reason="download failed 5 times — inspect by hand",
            ),
        )
        report = run_mod.status_report(
            make_ctx(instance, FakeDriver(), config=Config(media_fetch=MediaFetch.NONE))
        )
        yours = report.split("### Needs you", 1)[1]
        assert "download failed 5 times — inspect by hand" in yours
        assert "media_fetch is `none`" not in yours

    def test_oversize_media_is_skipped_with_reason(self, instance):
        write_item(instance)
        huge = HttpResponse(
            status=200, content_type="image/png", body=b"x" * (run_mod.MEDIA_MAX_BYTES + 1)
        )
        ctx = make_ctx(
            instance,
            FakeDriver(fetch_fn=self.media_fetch([self.IMG1])),
            transport=FakeTransport({self.IMG1: huge}),
        )
        run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert entry.status is Status.SKIPPED
        assert entry.reason == "media exceeds 10MB ceiling"

    def test_the_media_get_carries_the_byte_ceiling(self, instance):
        # The ceiling rides the fetch so the transport stops reading one
        # byte past it — a GET without it buffers a lying server's whole
        # body before the size check ever runs.
        write_item(instance)
        img = HttpResponse(status=200, content_type="image/png", body=b"p")
        transport = FakeTransport({self.IMG1: img})
        ctx = make_ctx(
            instance, FakeDriver(fetch_fn=self.media_fetch([self.IMG1])), transport=transport
        )
        run_mod.run(ctx)
        bounded = [
            limit
            for (method, url), limit in zip(transport.calls, transport.limits, strict=True)
            if url == self.IMG1 and method == "GET"
        ]
        assert bounded == [run_mod.MEDIA_MAX_BYTES]

    def test_transient_media_failure_is_blocked_and_redrains_without_a_driver(self, instance):
        write_item(instance)
        outage = HttpResponse(status=503, content_type="text/html", body=b"")
        driver = FakeDriver(fetch_fn=self.media_fetch([self.IMG1]))
        ctx = make_ctx(instance, driver, transport=FakeTransport({self.IMG1: outage}))
        run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert entry.status is Status.BLOCKED
        assert entry.attempts == 1

        healed = FakeTransport(
            {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"png")}
        )
        ctx2 = make_ctx(instance, driver, transport=healed)
        run_mod.run(ctx2)
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert entry.status is Status.DONE
        # The media redrain never went through a driver fetch:
        assert all(unit.url != self.IMG1 for unit in driver.fetched)

    def test_an_empty_media_body_is_blocked_and_never_written(self, instance):
        # The Instagram embed proxies answer an out-of-range media index
        # with a 200 and no body at all; written through, that is an empty
        # `media-0.png` the ledger calls done.
        write_item(instance)
        hollow = HttpResponse(status=200, content_type="image/png", body=b"")
        ctx = make_ctx(
            instance,
            FakeDriver(fetch_fn=self.media_fetch([self.IMG1])),
            transport=FakeTransport({self.IMG1: hollow}),
        )
        run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert entry.status is Status.BLOCKED
        assert entry.reason == "media URL returned an empty response body"
        assert entry.attempts == 1
        assert entry.path is None
        assert not any((instance.enrichment_dir / ITEM).glob("media-*"))

    def test_an_empty_media_body_redrains_when_the_server_serves_bytes(self, instance):
        write_item(instance)
        hollow = HttpResponse(status=200, content_type="image/png", body=b"")
        driver = FakeDriver(fetch_fn=self.media_fetch([self.IMG1]))
        run_mod.run(make_ctx(instance, driver, transport=FakeTransport({self.IMG1: hollow})))
        assert ledger.load(instance.ledger_path)[work_hash(self.IMG1)].status is Status.BLOCKED

        healed = FakeTransport(
            {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"png")}
        )
        run_mod.run(make_ctx(instance, driver, transport=healed))
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert entry.status is Status.DONE
        assert (instance.root / f"enrichment/{ITEM}/media-0.png").read_bytes() == b"png"

    @pytest.mark.parametrize(
        ("body", "shape"),
        [
            (b"<!doctype html><html><body>Page not found</body></html>", "HTML"),
            (b'<?xml version="1.0"?><error><code>404</code></error>', "XML"),
            (b'{"error": "not found"}', "JSON"),
        ],
    )
    def test_a_document_body_is_blocked_and_never_written(self, instance, body, shape):
        # A framework's catch-all route answers an og:image URL with the
        # site's own HTML under a 200; written through, that is 89KB of
        # markup stored as `media-0.jpg` and a unit the ledger calls done.
        write_item(instance)
        page = HttpResponse(status=200, content_type="image/png", body=body)
        ctx = make_ctx(
            instance,
            FakeDriver(fetch_fn=self.media_fetch([self.IMG1])),
            transport=FakeTransport({self.IMG1: page}),
        )
        run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert entry.status is Status.BLOCKED
        assert entry.reason == f"media URL answered with {shape}, not media bytes"
        assert entry.attempts == 1
        assert entry.path is None
        assert not any((instance.enrichment_dir / ITEM).glob("media-*"))

    def test_a_permanent_document_body_escalates_to_manual(self, instance):
        # A bot wall clears on a later run; a catch-all route never does.
        # Blocked is what tells those two apart, and `enrich mark` heals
        # what lands in manual.
        write_item(instance)
        page = HttpResponse(status=200, content_type="text/html", body=b"<html>shell</html>")
        ctx = make_ctx(
            instance,
            FakeDriver(fetch_fn=self.media_fetch([self.IMG1])),
            transport=FakeTransport({self.IMG1: page}),
        )
        for _ in range(MAX_BLOCKED_ATTEMPTS):
            run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert entry.status is Status.MANUAL
        assert entry.reason == (
            f"still blocked after {MAX_BLOCKED_ATTEMPTS} attempts — "
            "media URL answered with HTML, not media bytes"
        )
        assert not any((instance.enrichment_dir / ITEM).glob("media-*"))

    def test_the_bytes_name_the_file_never_the_url(self, instance):
        # CDNs content-negotiate: a `.jpg` URL answers with PNG bytes, and
        # an extension that lies about its body breaks every reader after.
        write_item(instance)
        ctx = make_ctx(
            instance,
            FakeDriver(fetch_fn=self.media_fetch([self.IMG2])),
            transport=FakeTransport(
                {self.IMG2: HttpResponse(status=200, content_type="image/jpeg", body=PNG_BYTES)}
            ),
        )
        run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG2)]
        assert entry.path == f"enrichment/{ITEM}/media-0.png"
        assert (instance.root / entry.path).read_bytes() == PNG_BYTES

    def test_unrecognized_bytes_keep_the_urls_extension(self, instance):
        write_item(instance)
        opaque = b"\x00\x01\x02\x03 a format nobody named"
        ctx = make_ctx(
            instance,
            FakeDriver(fetch_fn=self.media_fetch([self.IMG2])),
            transport=FakeTransport(
                {self.IMG2: HttpResponse(status=200, content_type="image/jpeg", body=opaque)}
            ),
        )
        run_mod.run(ctx)
        assert ledger.load(instance.ledger_path)[work_hash(self.IMG2)].path == (
            f"enrichment/{ITEM}/media-0.jpg"
        )

    def _jpeg_in_the_slot(self, instance) -> FakeDriver:
        """One media unit downloaded from a `.jpg` URL, JPEG bytes and all."""
        write_item(instance)
        driver = FakeDriver(fetch_fn=self.media_fetch([self.IMG2]))
        transport = FakeTransport(
            {self.IMG2: HttpResponse(status=200, content_type="image/jpeg", body=JPEG_BYTES)}
        )
        run_mod.run(make_ctx(instance, driver, transport=transport))
        assert sorted(p.name for p in (instance.enrichment_dir / ITEM).glob("media-*")) == [
            "media-0.jpg"
        ]
        return driver

    def _redrain_after_crash(self, instance, driver, body: bytes) -> None:
        """Redrain the media unit: drop its outcome line, serve ``body`` again."""
        lines = [line for line in instance.ledger_path.read_text().split("\n") if line.strip()]
        assert json.loads(lines[-1])["hash"] == work_hash(self.IMG2)
        instance.ledger_path.write_text("\n".join(lines[:-1]) + "\n")
        transport = FakeTransport(
            {self.IMG2: HttpResponse(status=200, content_type="image/jpeg", body=body)}
        )
        run_mod.run(make_ctx(instance, driver, transport=transport))

    def test_a_redownload_in_a_new_format_replaces_the_slot(self, instance):
        # The slot names the file and the bytes name its extension, so a
        # CDN that switches format between runs must not leave `media-0.png`
        # standing beside `media-0.jpg` — two files in one slot, both
        # spending the item's media cap.
        self._redrain_after_crash(instance, self._jpeg_in_the_slot(instance), PNG_BYTES)
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG2)]
        assert entry.status is Status.DONE
        assert entry.path == f"enrichment/{ITEM}/media-0.png"
        assert sorted(p.name for p in (instance.enrichment_dir / ITEM).glob("media-*")) == [
            "media-0.png"
        ]

    def test_a_replaced_slot_leaves_the_session_description_alone(self, instance):
        # `media-0.md` is the session's description of the capture, not a
        # file in the slot — the sweep that clears the old format must
        # never take it.
        driver = self._jpeg_in_the_slot(instance)
        described = instance.enrichment_dir / ITEM / "media-0.md"
        described.write_text("the photographed page, described\n", encoding="utf-8")

        self._redrain_after_crash(instance, driver, PNG_BYTES)
        assert described.read_text() == "the photographed page, described\n"
        assert sorted(p.name for p in described.parent.glob("media-0.*")) == [
            "media-0.md",
            "media-0.png",
        ]

    def test_a_replaced_slot_sweeps_an_interrupted_downloads_temp(self, instance):
        # No reader counts a `.tmp` as media any more, so the slot sweep is
        # the only thing left that clears one — it names them explicitly
        # rather than leaning on the media predicate.
        driver = self._jpeg_in_the_slot(instance)
        orphan = instance.enrichment_dir / ITEM / f"media-0.jpg.a1b2c3d4{atomic.TEMP_SUFFIX}"
        orphan.write_bytes(b"half a download")

        self._redrain_after_crash(instance, driver, PNG_BYTES)
        assert not orphan.exists()
        assert sorted(p.name for p in orphan.parent.glob("media-0.*")) == ["media-0.png"]

    def test_an_orphaned_temp_never_spends_the_media_cap(self, instance):
        # Before the exclusion the orphan was the item's fourth media-family
        # file and the download it displaced was refused permanently.
        item_dir = instance.enrichment_dir / ITEM
        item_dir.mkdir(parents=True)
        for n in range(MEDIA_MAX_FILES - 1):
            (item_dir / f"aaaaaa-asset-{n}.png").write_bytes(b"asset")
        (item_dir / f"media-3.png.a1b2c3d4{atomic.TEMP_SUFFIX}").write_bytes(b"half a download")
        write_item(instance)
        ctx = make_ctx(
            instance,
            FakeDriver(fetch_fn=self.media_fetch([self.IMG1])),
            transport=FakeTransport(
                {self.IMG1: HttpResponse(status=200, content_type="image/png", body=PNG_BYTES)}
            ),
        )
        run_mod.run(ctx)
        assert ledger.load(instance.ledger_path)[work_hash(self.IMG1)].status is Status.DONE
        assert (item_dir / "media-0.png").is_file()

    def test_media_cap_of_four_files_per_item(self, instance):
        write_item(instance)
        urls = [f"https://cdn.example.test/img-{n}.png" for n in range(MEDIA_MAX_FILES + 2)]
        responses = {
            url: HttpResponse(status=200, content_type="image/png", body=b"p") for url in urls
        }
        ctx = make_ctx(
            instance,
            FakeDriver(fetch_fn=self.media_fetch(urls)),
            transport=FakeTransport(responses),
        )
        run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        files = sorted(p.name for p in (instance.enrichment_dir / ITEM).glob("media-*.png"))
        assert len(files) == MEDIA_MAX_FILES
        skipped = [entries[work_hash(url)] for url in urls[MEDIA_MAX_FILES:]]
        assert all(e.status is Status.SKIPPED for e in skipped)
        assert all(e.reason == f"media cap ({MEDIA_MAX_FILES} files) reached" for e in skipped)

    def test_dead_media_siblings_never_spend_the_file_cap(self, instance):
        # The wild shape: an X thread pooling 6 photos whose first two 404.
        # The cap bounds FILES, so all four survivors must land — a cap that
        # counted ledger positions instead recorded a terminal `skipped
        # media cap` on two units with only two files on disk, and terminal
        # means lost forever, with a false reason on the record.
        urls = [f"https://cdn.example.test/p{n}.png" for n in range(MEDIA_MAX_FILES + 2)]
        gone = HttpResponse(status=404, content_type="text/html", body=b"")
        responses = {
            url: HttpResponse(status=200, content_type="image/png", body=b"p") for url in urls[2:]
        }
        responses[urls[0]] = gone
        responses[urls[1]] = gone
        write_item(instance)
        ctx = make_ctx(
            instance,
            FakeDriver(fetch_fn=self.media_fetch(urls)),
            transport=FakeTransport(responses),
        )
        run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        statuses = [entries[work_hash(url)].status for url in urls]
        assert statuses == [Status.DEAD, Status.DEAD] + [Status.DONE] * MEDIA_MAX_FILES
        files = sorted(p.name for p in (instance.enrichment_dir / ITEM).glob("media-*"))
        assert len(files) == MEDIA_MAX_FILES
        for url in urls[2:]:
            path = entries[work_hash(url)].path
            assert path is not None
            assert (instance.root / path).is_file()

    def test_a_session_written_media_description_never_unlocks_the_cap(self, instance):
        # `media-N.md` is the session's description of a media capture, not a
        # media file — `_media_file_count` excludes it and the slot check
        # must too, or the item ends with five media-family files and a
        # downloaded `media-0.png` beside a `media-0.md` about something else.
        item_dir = instance.enrichment_dir / ITEM
        item_dir.mkdir(parents=True)
        for n in range(MEDIA_MAX_FILES):
            (item_dir / f"aaaaaa-asset-{n}.png").write_bytes(b"asset")
        (item_dir / "media-0.md").write_text("the captured photo, described\n", encoding="utf-8")
        write_item(instance)
        ctx = make_ctx(
            instance,
            FakeDriver(fetch_fn=self.media_fetch([self.IMG1])),
            transport=FakeTransport(
                {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"png")}
            ),
        )
        run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert entry.status is Status.SKIPPED
        assert entry.reason == f"media cap ({MEDIA_MAX_FILES} files) reached"
        assert not (item_dir / "media-0.png").exists()

    def test_media_entries_are_born_queued_before_the_download(self, instance):
        # Entries exist from birth — a crash mid-download must leave a queued
        # via:media line the next run's redrain path picks up.
        write_item(instance)
        transport = FakeTransport(
            {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"p")}
        )
        ctx = make_ctx(
            instance, FakeDriver(fetch_fn=self.media_fetch([self.IMG1])), transport=transport
        )
        run_mod.run(ctx)
        audit = [
            json.loads(line)
            for line in instance.ledger_path.read_text().split("\n")
            if line.strip() and json.loads(line)["hash"] == work_hash(self.IMG1)
        ]
        assert [line["status"] for line in audit] == ["queued", "done"]
        assert all(line["job"] == "media" for line in audit)

    def test_media_3xx_is_blocked_never_a_run_crash(self, instance):
        # The totality pin at the media stage: a redirect-loop 302 surfaced
        # here used to raise out of classify_http and abort the whole run.
        write_item(instance)
        loop = HttpResponse(status=302, content_type="text/html", body=b"")
        ctx = make_ctx(
            instance,
            FakeDriver(fetch_fn=self.media_fetch([self.IMG1])),
            transport=FakeTransport({self.IMG1: loop}),
        )
        report = run_mod.run(ctx)  # completes — no crash
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert entry.status is Status.BLOCKED
        assert entry.reason == "unexpected HTTP 302"
        assert "## Enrich run" in report

    def test_declared_oversize_media_is_refused_without_a_get(self, instance):
        write_item(instance)
        huge_by_declaration = HttpResponse(
            status=200,
            content_type="image/png",
            body=b"",
            content_length=run_mod.MEDIA_MAX_BYTES + 1,
        )
        transport = FakeTransport({self.IMG1: huge_by_declaration})
        ctx = make_ctx(
            instance, FakeDriver(fetch_fn=self.media_fetch([self.IMG1])), transport=transport
        )
        run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert entry.status is Status.SKIPPED
        assert "Content-Length" in (entry.reason or "")
        media_calls = [call for call in transport.calls if call[1] == self.IMG1]
        assert media_calls == [("HEAD", self.IMG1)]  # the body was never fetched

    def og_image_page(self, content: str) -> bytes:
        return (
            f'<html><head><meta property="og:image" content="{content}">'
            "</head><body>page</body></html>"
        ).encode()

    def web_ctx(self, instance, transport):
        web = WebDriver(transport=transport, extract=lambda _html: "extracted body " * 40)
        return make_ctx(instance, FakeDriver(), drivers=[web], transport=transport)

    def test_relative_og_image_resolves_and_downloads(self, instance):
        # The media stage fetches verbatim, so a page-relative og:image used
        # to reach the transport as an unfetchable URL and take the run down.
        hero = "https://example.test/img/hero.png"
        transport = FakeTransport(
            {
                URL: HttpResponse(
                    status=200, content_type="text/html", body=self.og_image_page("/img/hero.png")
                ),
                hero: HttpResponse(status=200, content_type="image/png", body=b"png"),
            }
        )
        write_item(instance)
        ctx = self.web_ctx(instance, transport)
        run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        assert entries[work_hash(URL)].status is Status.DONE
        assert entries[work_hash(hero)].path == f"enrichment/{ITEM}/media-0.png"

    def test_protocol_relative_og_image_takes_the_page_scheme(self, instance):
        hero = "https://cdn.example.test/hero.png"
        transport = FakeTransport(
            {
                URL: HttpResponse(
                    status=200,
                    content_type="text/html",
                    body=self.og_image_page("//cdn.example.test/hero.png"),
                ),
                hero: HttpResponse(status=200, content_type="image/png", body=b"png"),
            }
        )
        write_item(instance)
        run_mod.run(self.web_ctx(instance, transport))
        assert ledger.load(instance.ledger_path)[work_hash(hero)].status is Status.DONE

    def test_unfetchable_media_url_is_charged_to_the_media_unit(self, instance):
        # A host with a space: the transport refuses it as a ValueError, so
        # the old inline download superseded the PARENT's done line with an
        # error and killed every later run. It never reaches a fetch now.
        bad = "https://exa mple.test/a.png"
        write_item(instance)
        transport = FakeTransport({})
        driver = FakeDriver(fetch_fn=self.media_fetch([bad]))
        ctx = make_ctx(instance, driver, transport=transport)
        report = run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        assert entries[work_hash(URL)].status is Status.DONE  # the parent stands
        media = entries[work_hash(bad)]
        assert media.status is Status.MANUAL
        assert media.job is Job.MEDIA
        assert media.parent == work_hash(URL)
        assert "unfetchable media URL" in (media.reason or "")
        assert all(call[1] != bad for call in transport.calls)  # never fetched
        assert bad in report

        # And the next run is clean: the park is terminal, nothing retries.
        run_mod.run(make_ctx(instance, driver, transport=transport))
        assert ledger.load(instance.ledger_path)[work_hash(bad)].status is Status.MANUAL

    def test_crash_window_redrain_overwrites_its_own_media_file(self, instance):
        # Birth line, file written, crash before the outcome line. The
        # redrain must overwrite media-0 — never write media-1 beside the
        # orphan a disk scan would take for someone else's slot.
        write_item(instance)
        transport = FakeTransport(
            {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"png")}
        )
        driver = FakeDriver(fetch_fn=self.media_fetch([self.IMG1]))
        run_mod.run(make_ctx(instance, driver, transport=transport))
        lines = [line for line in instance.ledger_path.read_text().split("\n") if line.strip()]
        assert json.loads(lines[-1])["hash"] == work_hash(self.IMG1)
        instance.ledger_path.write_text("\n".join(lines[:-1]) + "\n")  # the crash
        assert (instance.enrichment_dir / ITEM / "media-0.png").exists()

        run_mod.run(make_ctx(instance, driver, transport=transport))
        entry = ledger.load(instance.ledger_path)[work_hash(self.IMG1)]
        assert entry.status is Status.DONE
        assert entry.path == f"enrichment/{ITEM}/media-0.png"
        assert sorted(p.name for p in (instance.enrichment_dir / ITEM).glob("media-*")) == [
            "media-0.png"
        ]

    def test_media_does_not_count_toward_the_url_cap(self, instance):
        write_item(instance)
        transport = FakeTransport(
            {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"p")}
        )

        def fetch(unit):
            media = [self.IMG1] if unit.depth == 0 else []
            return Content(meta={}, body="b" * 400, media=media)

        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch), transport=transport)
        run_mod.run(ctx)
        run_mod.fetch_urls(ctx, ITEM, ["https://example.test/docs"])
        drain = run_mod._Drain(ctx=ctx)  # noqa: SLF001 — asserting the counting rule directly
        assert drain.fetched_count(ITEM) == 2  # seed + child; media excluded


class TestFetchedCountStaysARecount:
    """The maintained count answers exactly what a full recount would.

    The cap check reads a per-item table kept beside the entries instead of
    recounting the ledger per admission; this pins the table to the recount
    it replaced, across every way the drain's state moves — admissions,
    cap fires, --force past the bound, outcomes that give budget back,
    media lines that never spend it, and ownership re-resolving under a
    rename.
    """

    def _recount(self, drain, item_id):
        # Today's rule, spelled independently of the maintained table.
        return sum(
            1
            for entry in drain.entries.values()
            if drain.owner_of(entry) == item_id
            and entry.job is None
            and entry.status is not Status.SKIPPED
        )

    def _agrees(self, drain, *item_ids):
        for item_id in item_ids:
            assert drain.fetched_count(item_id) == self._recount(drain, item_id)

    def test_agreement_across_admissions_force_outcomes_and_renames(self, instance):
        urls = [f"https://example.test/p{n}" for n in range(MAX_URLS_PER_ITEM)]
        write_item(instance, urls=urls)
        drain = run_mod._Drain(ctx=make_ctx(instance, FakeDriver()))  # noqa: SLF001 — the table under test is drain state
        drain.seed_from_corpus()
        self._agrees(drain, ITEM)

        # A promotion refused at the cap writes only a skipped marker.
        parent = drain.entries[work_hash(urls[0])]
        extra = "https://example.test/extra"
        refused = drain.admit(ITEM, extra, via="harvest", parent=parent)
        assert refused.cap is Cap.URL_REQUESTED
        self._agrees(drain, ITEM)

        # --force admits past the bound: the waived fire plus a queued line.
        forced = drain.admit(ITEM, extra, via="harvest", parent=parent, force=True)
        assert forced.entry is not None
        self._agrees(drain, ITEM)

        # A skip landing on an admitted unit gives its budget back...
        drain.record_outcome(
            drain.entries[work_hash(urls[1])], status=Status.SKIPPED, reason="thin-extraction"
        )
        self._agrees(drain, ITEM)

        # ...and a media line never spent any.
        drain.record(
            LedgerEntry(
                hash=work_hash("https://cdn.example.test/m.png"),
                url="https://cdn.example.test/m.png",
                item=ITEM,
                kind=Kind.WEB,
                status=Status.QUEUED,
                engine="seed",
                date=datetime.date.min,
                job=Job.MEDIA,
                parent=parent.hash,
                depth=1,
            )
        )
        self._agrees(drain, ITEM)

        # A rename re-attributes every unit: the counts must follow the
        # corpus's new answer, not the stored strings.
        renamed = "2026-08-19-renamed-55ad7b"
        write_item(instance, renamed, urls=[*urls, extra])
        (instance.corpus_dir / "2026" / f"{ITEM}.md").unlink()
        drain.resolve_owners()
        self._agrees(drain, ITEM, renamed)
        assert drain.fetched_count(renamed) == self._recount(drain, renamed) > 0


class TestExtractAssets:
    def asset_fetch(self, assets):
        def fetch(_unit):
            return Content(meta={}, body="b" * 400, assets=list(assets))

        return fetch

    def test_assets_do_not_count_toward_the_url_cap(self, instance):
        # The 12-URL cap bounds fetched PAGES; asset entries are byte-writes
        # of embedded images — a document rich in assets must not spend the
        # item's URL budget.
        write_item(instance)
        assets = [Asset(data=b"png", suggested_ext="png"), Asset(data=b"jpg", suggested_ext="jpg")]
        ctx = make_ctx(instance, FakeDriver(fetch_fn=self.asset_fetch(assets)))
        run_mod.run(ctx)
        drain = run_mod._Drain(ctx=ctx)  # noqa: SLF001 — asserting the counting rule directly
        assert drain.fetched_count(ITEM) == 1  # the page alone; both assets excluded
        ledgered = ledger.load(ctx.instance.ledger_path)
        assert sum(1 for e in ledgered.values() if e.job is Job.ASSET) == 2

    def test_assets_write_under_media_caps_ledgered_extract_asset(self, instance):
        write_item(instance)
        assets = [
            Asset(data=b"png-bytes", suggested_ext="png"),
            Asset(data=b"jpg-bytes", suggested_ext="jpg"),
        ]
        ctx = make_ctx(instance, FakeDriver(fetch_fn=self.asset_fetch(assets)))
        run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        parent_hash = work_hash(URL)
        rows = sorted((e for e in entries.values() if e.job is Job.ASSET), key=lambda e: e.url)
        assert len(rows) == 2
        first = rows[0]
        assert first.status is Status.DONE
        assert first.parent == parent_hash
        assert first.kind is Kind.WEB  # the parent's kind
        assert first.url == f"enrichment/{ITEM}/{parent_hash[:6]}-asset-0.png"
        assert first.path == first.url
        assert (instance.root / first.path).read_bytes() == b"png-bytes"

    def test_oversize_asset_is_skipped_with_reason(self, instance):
        write_item(instance)
        huge = Asset(data=b"x" * (run_mod.MEDIA_MAX_BYTES + 1), suggested_ext="png")
        ctx = make_ctx(instance, FakeDriver(fetch_fn=self.asset_fetch([huge])))
        run_mod.run(ctx)
        row = next(e for e in ledger.load(instance.ledger_path).values() if e.job is Job.ASSET)
        assert row.status is Status.SKIPPED
        assert "10MB" in (row.reason or "")
        assert not (instance.root / row.url).exists()

    def test_the_four_file_cap_is_shared_with_the_media_stage(self, instance):
        write_item(instance)
        item_dir = instance.enrichment_dir / ITEM
        item_dir.mkdir(parents=True)
        for n in range(3):
            (item_dir / f"media-{n}.png").write_bytes(b"m")  # three media downloads already
        assets = [Asset(data=b"a", suggested_ext="png"), Asset(data=b"b", suggested_ext="png")]
        ctx = make_ctx(instance, FakeDriver(fetch_fn=self.asset_fetch(assets)))
        run_mod.run(ctx)
        rows = sorted(
            (e for e in ledger.load(instance.ledger_path).values() if e.job is Job.ASSET),
            key=lambda e: e.url,
        )
        assert [row.status for row in rows] == [Status.DONE, Status.SKIPPED]
        assert "media cap" in (rows[1].reason or "")

    def test_asset_reruns_overwrite_never_duplicate_or_respam_the_ledger(self, instance):
        write_item(instance)
        assets = [Asset(data=b"png-bytes", suggested_ext="png")]
        ctx = make_ctx(instance, FakeDriver(fetch_fn=self.asset_fetch(assets)))
        run_mod.run(ctx)
        entry = entry_for(ctx)
        requeued = dataclasses.replace(
            entry, status=Status.QUEUED, path=None, title=None, rerun=True, via="migration-2"
        )
        ledger.append(instance.ledger_path, requeued)
        run_mod.run(make_ctx(instance, FakeDriver(fetch_fn=self.asset_fetch(assets))))
        item_dir = instance.enrichment_dir / ITEM
        asset_files = sorted(p.name for p in item_dir.glob("*-asset-*"))
        assert asset_files == [f"{work_hash(URL)[:6]}-asset-0.png"]  # overwrite, no duplicate
        audit = [
            json.loads(line)
            for line in instance.ledger_path.read_text().split("\n")
            if line.strip() and json.loads(line).get("job") == "asset"
        ]
        assert len(audit) == 1  # the unchanged rerun added no audit line


class TestIsDrainable:
    def base_entry(self, **overrides):
        base = LedgerEntry(
            hash="73bd784849",
            url=URL,
            item=ITEM,
            kind=Kind.WEB,
            status=Status.QUEUED,
            engine="0.2.0",
            date=TODAY,
        )
        return dataclasses.replace(base, **overrides)

    def ctx(self, instance, provider=None):
        return make_ctx(instance, FakeDriver(), provider_available=provider or no_providers)

    @pytest.mark.parametrize("status", list(Status))
    def test_total_over_every_status(self, instance, status):
        # Exhaustiveness: a valid entry of EVERY status gets a bool, no crash.
        overrides = {
            Status.WAITING: {"needs": Need.TRANSCRIBE},
            Status.BLOCKED: {"attempts": 1},
            Status.ERROR: {"error": "boom"},
            Status.MANUAL: {"reason": "r"},
            Status.SKIPPED: {"reason": "r"},
            Status.DONE: {},
            Status.DEAD: {},
            Status.QUEUED: {},
        }[status]
        entry = self.base_entry(status=status, **overrides)
        assert is_drainable(entry, self.ctx(instance)) in (True, False)

    def test_semantics_table(self, instance, flippable_provider):
        ctx = self.ctx(instance, flippable_provider)
        assert is_drainable(self.base_entry(), ctx) is True
        assert is_drainable(self.base_entry(status=Status.DONE), ctx) is False
        assert is_drainable(self.base_entry(status=Status.DEAD), ctx) is False
        assert is_drainable(self.base_entry(status=Status.SKIPPED, reason="r"), ctx) is False
        assert is_drainable(self.base_entry(status=Status.MANUAL, reason="r"), ctx) is False
        assert is_drainable(self.base_entry(status=Status.BLOCKED, attempts=4), ctx) is True
        assert is_drainable(self.base_entry(status=Status.BLOCKED, attempts=5), ctx) is False
        waiting = self.base_entry(status=Status.WAITING, needs=Need.TRANSCRIBE)
        assert is_drainable(waiting, ctx) is False
        flippable_provider.ok = True
        assert is_drainable(waiting, ctx) is True
        error = self.base_entry(status=Status.ERROR, error="boom", engine="0.2.0")
        assert is_drainable(error, ctx) is False  # ctx runs 0.2.0 — same engine
        newer = make_ctx(instance, FakeDriver(), engine_version="0.3.0")
        assert is_drainable(error, newer) is True

    def test_unparseable_stored_engine_is_drainable_once_not_a_crash(self, instance):
        # A hand-healed line with a garbage engine value must not crash
        # queue-building; the retry re-stamps a valid engine and heals it.
        relic = self.base_entry(status=Status.ERROR, error="boom", engine="hand-healed")
        assert is_drainable(relic, self.ctx(instance)) is True


class TestVerbs:
    def test_fetch_urls_ledgers_children_and_drains_them(self, instance):
        write_item(instance)
        driver = FakeDriver()
        ctx = make_ctx(instance, driver)
        run_mod.run(ctx)
        extra = "https://example.test/pricing"
        report = run_mod.fetch_urls(ctx, ITEM, [extra])
        entry = ledger.load(instance.ledger_path)[work_hash(extra)]
        assert entry.status is Status.DONE
        assert entry.via == "harvest"
        assert entry.parent == work_hash(URL)  # the item's primary work unit
        assert entry.depth == 1
        assert "1 unit processed" in report

    def test_fetch_urls_reports_a_cross_item_url_and_the_batch_continues(self, instance):
        # One URL enriches under one item — but raising aborted the batch
        # mid-flight, losing the report for URLs already ledgered.
        write_item(instance, "2026-08-19-first-aaaaaa", urls=[URL])
        write_item(instance, "2026-08-19-second-bbbbbb", urls=["https://example.test/other"])
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        good = "https://example.test/docs"
        report = run_mod.fetch_urls(ctx, "2026-08-19-second-bbbbbb", [URL, good])
        flat = " ".join(report.split())
        assert "already enriches under item 2026-08-19-first-aaaaaa" in flat
        # The unit stayed with its owner, provenance intact:
        entries = ledger.load(instance.ledger_path)
        assert entries[work_hash(URL)].item == "2026-08-19-first-aaaaaa"
        assert entries[work_hash(URL)].status is Status.DONE
        assert entries[work_hash(good)].item == "2026-08-19-second-bbbbbb"
        assert entries[work_hash(good)].status is Status.DONE  # the batch continued

    def test_fetch_urls_on_the_items_own_unit_is_a_rerun_in_place(self, instance):
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        run_mod.fetch_urls(ctx, ITEM, [URL])  # re-fetch the primary URL itself
        entry = entry_for(ctx)
        assert entry.status is Status.DONE
        assert entry.parent is None  # never re-parented under itself
        assert entry.depth is None
        assert entry.rerun is True
        # And the primary work unit is still resolvable for --parent-less fetches:
        run_mod.fetch_urls(ctx, ITEM, ["https://example.test/docs"])
        child = ledger.load(instance.ledger_path)[work_hash("https://example.test/docs")]
        assert child.parent == work_hash(URL)

    def test_fetch_urls_serves_every_co_claimant_of_a_shared_unit(self, instance):
        # Seeding hands a shared URL's unit to the first item and the line
        # names only that one, but every item listing the URL owns it:
        # resolved as equality with the stored name, only the first
        # claimant could ever `enrich fetch`, and the second was told it
        # had no primary work unit — both implied causes false.
        write_item(instance, ALPHA)
        write_item(instance, BRAVO)  # lists the same URL; alpha wins the dedupe
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        promotions = (
            (ALPHA, "https://example.test/a-doc"),
            (BRAVO, "https://example.test/b-doc"),
        )
        for item_id, url in promotions:
            run_mod.fetch_urls(ctx, item_id, [url])
            child = ledger.load(instance.ledger_path)[work_hash(url)]
            assert child.item == item_id
            assert child.parent == work_hash(URL)  # the shared unit, defaulted
            assert child.depth == 1
            assert child.status is Status.DONE

    def test_fetch_urls_unknown_item_is_loud(self, instance):
        ctx = make_ctx(instance, FakeDriver())
        with pytest.raises(ValueError, match="unknown corpus item"):
            run_mod.fetch_urls(ctx, "2026-01-01-nope-000000", ["https://example.test/x"])

    def test_fetch_urls_parks_a_non_http_url_and_the_batch_continues(self, instance):
        # The sniff HEAD refuses non-http(s) URLs with a ValueError; that
        # parks the ONE bad URL — the rest of the batch drains and the
        # report renders (no partial ledger writes with no report).
        write_item(instance)
        bad = "ftp://example.test/archive.tar"
        good = "https://example.test/docs"
        transport = FakeTransport(
            {bad: ValueError(f"transport fetches http(s) URLs only, got {bad!r}")}
        )
        ctx = make_ctx(instance, FakeDriver(), transport=transport)
        run_mod.run(ctx)
        report = run_mod.fetch_urls(ctx, ITEM, [bad, good])
        entries = ledger.load(instance.ledger_path)
        parked = entries[work_hash(bad)]
        assert parked.status is Status.MANUAL
        assert "unfetchable fetch URL" in (parked.reason or "")
        assert entries[work_hash(good)].status is Status.DONE  # the batch continued
        assert "unfetchable fetch URL" in report  # parked rows are printed

    def test_fetch_urls_respects_the_cap_and_force_exceeds_it(self, instance):
        urls = [f"https://example.test/p{n}" for n in range(MAX_URLS_PER_ITEM)]
        write_item(instance, urls=urls)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        extra = "https://example.test/extra"
        run_mod.fetch_urls(ctx, ITEM, [extra])
        refused = ledger.load(instance.ledger_path)[work_hash(extra)]
        assert refused.status is Status.SKIPPED
        assert refused.cap is Cap.URL_REQUESTED
        assert "url cap" in (refused.reason or "")
        assert refused.forced is False  # a refusal that stood — the drift reading's

        # A repeat WITHOUT --force is refused again — the refusal marker is
        # not an admitted unit and must never sneak in as a "rerun":
        run_mod.fetch_urls(ctx, ITEM, [extra])
        assert ledger.load(instance.ledger_path)[work_hash(extra)].status is Status.SKIPPED

        run_mod.fetch_urls(ctx, ITEM, [extra], force=True)
        forced = ledger.load(instance.ledger_path)[work_hash(extra)]
        assert forced.status is Status.DONE
        # The cap fire stayed in the audit trail, marked as the waiver it is:
        waived = [
            json.loads(line)
            for line in instance.ledger_path.read_text().split("\n")
            if line.strip() and json.loads(line).get("forced")
        ]
        assert len(waived) == 1
        assert waived[0]["cap"] == Cap.URL_REQUESTED.value
        assert "exceeded by --force" in waived[0]["reason"]

    def test_a_capped_fetch_refusal_is_surfaced_with_its_force_route(self, instance):
        # An owner-requested refusal IS surfaced — a bare "skipped 1" leaves
        # the --force affordance unreachable.
        urls = [f"https://example.test/p{n}" for n in range(MAX_URLS_PER_ITEM)]
        write_item(instance, urls=urls)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        extra = "https://example.test/extra"
        report = run_mod.fetch_urls(ctx, ITEM, [extra])
        flat = " ".join(report.split())
        assert extra in flat
        assert "url cap (12 per item) reached — rerun with --force to exceed" in flat

    def test_repeat_capped_refusals_do_not_append_identical_lines(self, instance):
        urls = [f"https://example.test/p{n}" for n in range(MAX_URLS_PER_ITEM)]
        write_item(instance, urls=urls)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        extra = "https://example.test/extra"
        run_mod.fetch_urls(ctx, ITEM, [extra])
        before = instance.ledger_path.read_text()
        report = run_mod.fetch_urls(ctx, ITEM, [extra])  # refused again
        assert instance.ledger_path.read_text() == before  # nothing new to say
        assert "--force" in " ".join(report.split())  # still answered

    def test_cap_markers_are_recognized_by_the_flag_not_the_wording(self, instance):
        urls = [f"https://example.test/p{n}" for n in range(MAX_URLS_PER_ITEM)]
        write_item(instance, urls=urls)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        extra = "https://example.test/extra"
        run_mod.fetch_urls(ctx, ITEM, [extra])
        refused = ledger.load(instance.ledger_path)[work_hash(extra)]
        # Reword the marker's prose entirely — recognition must not read it.
        ledger.append(
            instance.ledger_path,
            dataclasses.replace(refused, reason="twelve fetches is plenty for one item"),
        )
        run_mod.fetch_urls(ctx, ITEM, [extra])
        again = ledger.load(instance.ledger_path)[work_hash(extra)]
        assert again.status is Status.SKIPPED
        assert again.cap is Cap.URL_REQUESTED

    def test_a_marked_skip_with_cap_lookalike_prose_is_not_a_cap_marker(self, instance):
        # Session-authored --reason text lives in the same namespace as the
        # engine's cap wording; only the typed flag may route.
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        run_mod.mark(
            ctx, URL, Status.SKIPPED, reason=f"url cap ({MAX_URLS_PER_ITEM} per item) reached"
        )
        run_mod.fetch_urls(ctx, ITEM, [URL])
        entry = ledger.load(instance.ledger_path)[work_hash(URL)]
        assert entry.status is Status.DONE  # requeued in place and re-fetched
        assert entry.rerun is True

    def test_mark_heals_through_the_verb(self, instance):
        write_item(instance)
        fetch = lambda _unit: Refused(evidence="HTTP 403")  # noqa: E731
        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        confirmation = run_mod.mark(ctx, URL, Status.DONE, path=f"enrichment/{ITEM}/web-000000.md")
        assert work_hash(URL)[:10] in confirmation
        entry = entry_for(ctx)
        assert entry.status is Status.DONE
        assert entry.path == f"enrichment/{ITEM}/web-000000.md"

    def test_mark_done_carries_the_prior_outputs_forward(self, instance):
        # A heal never erases what it does not correct: re-marking a done
        # entry (or overriding just its path) keeps the recorded outputs.
        # (A non-done line cannot HOLD path/title under the schema, so a heal from
        # such a line has nothing to carry — the audit trail keeps history.)
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())  # default fetch records path + title "t"
        run_mod.run(ctx)
        first = entry_for(ctx)
        run_mod.mark(ctx, URL, Status.DONE)  # re-stamp heal, no flags
        healed = entry_for(ctx)
        assert healed.path == first.path
        assert healed.title == first.title
        run_mod.mark(ctx, URL, Status.DONE, path=f"enrichment/{ITEM}/web-corrected.md")
        corrected = entry_for(ctx)
        assert corrected.path == f"enrichment/{ITEM}/web-corrected.md"
        assert corrected.title == first.title  # title survives a path correction

    def test_mark_waiting_can_set_the_need(self, instance):
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        run_mod.mark(ctx, URL, Status.WAITING, needs=run_mod.Need.TRANSCRIBE)
        entry = entry_for(ctx)
        assert entry.status is Status.WAITING
        assert entry.needs is Need.TRANSCRIBE

    def test_mark_waiting_without_a_need_anywhere_is_loud(self, instance):
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        with pytest.raises(ValueError, match="needs"):
            run_mod.mark(ctx, URL, Status.WAITING)

    def test_mark_unknown_url_is_loud(self, instance):
        ctx = make_ctx(instance, FakeDriver())
        with pytest.raises(ValueError, match="no ledger entry"):
            run_mod.mark(ctx, "https://example.test/never-seen", Status.DEAD)

    def test_mark_manual_without_reason_is_loud(self, instance):
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        with pytest.raises(ValueError, match="reason"):
            run_mod.mark(ctx, URL, Status.MANUAL)

    def _torn_ledger(self, instance) -> RunContext:
        """A run's clean ledger with an interrupted append's tail behind it."""
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        with instance.ledger_path.open("a", encoding="utf-8") as f:
            f.write('{"hash": "73bd78')
        return ctx

    def test_mark_on_a_torn_ledger_names_the_line_and_the_repair(self, instance):
        # mark bricks on the tear — it must, the ledger is unreadable —
        # but "unparseable ledger line" alone left the owner with no line
        # to delete and no sanction to delete it under.
        ctx = self._torn_ledger(instance)
        with pytest.raises(ValueError, match="delete this torn line") as err:
            run_mod.mark(ctx, URL, Status.SKIPPED, reason="ruled out")
        assert "enrichment-ledger.jsonl:3" in str(err.value)

    def test_compact_on_a_torn_ledger_names_the_line_and_the_repair(self, instance):
        ctx = self._torn_ledger(instance)
        with pytest.raises(ValueError, match="delete this torn line") as err:
            run_mod.compact(ctx)
        assert "enrichment-ledger.jsonl:3" in str(err.value)

    def test_mark_normalizes_a_multiline_reason_so_the_item_view_still_renders(self, instance):
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        run_mod.mark(ctx, URL, Status.MANUAL, reason="paywalled;\nrescue\tby hand")
        assert entry_for(ctx).reason == "paywalled; rescue by hand"
        view = run_mod.status_report(ctx, item_id=ITEM)  # would raise PayloadError on \n
        assert "paywalled; rescue by hand" in " ".join(view.split())

    def test_mark_whitespace_only_reason_on_manual_is_loud_not_blank(self, instance):
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        with pytest.raises(ValueError, match="reason"):
            run_mod.mark(ctx, URL, Status.MANUAL, reason=" \n\t ")

    def test_mark_heals_a_bad_seed_parked_on_its_raw_url(self, instance):
        # A bad seed is keyed on the URL exactly as captured — canonicalizing
        # it is what failed — so a canonical-only lookup never reaches it and
        # the whole class would be unhealable.
        bad_url = "https://[bad-ipv6"
        write_item(instance, urls=[bad_url])
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        assert ledger.load(instance.ledger_path)[work_hash(bad_url)].status is Status.MANUAL
        confirmation = run_mod.mark(ctx, bad_url, Status.DEAD, reason="the host never existed")
        healed = ledger.load(instance.ledger_path)[work_hash(bad_url)]
        assert healed.status is Status.DEAD
        assert healed.url == bad_url  # the entry keeps its own key
        assert work_hash(bad_url) in confirmation

    def test_mark_heals_a_via_media_entry_by_its_verbatim_url(self, instance):
        # Media URLs are ledgered verbatim — signed params ARE the resource —
        # so the canonical hash of one can never meet its stored key.
        signed = "https://cdn.example.test/hero.png?si=abc123"
        write_item(instance)
        fetch = lambda _unit: Content(meta={}, body="b" * 400, media=[signed])  # noqa: E731
        outage = HttpResponse(status=503, content_type="image/png", body=b"")
        ctx = make_ctx(
            instance, FakeDriver(fetch_fn=fetch), transport=FakeTransport({signed: outage})
        )
        run_mod.run(ctx)
        assert ledger.load(instance.ledger_path)[work_hash(signed)].status is Status.BLOCKED
        run_mod.mark(ctx, signed, Status.SKIPPED, reason="the signed link expired")
        healed = ledger.load(instance.ledger_path)[work_hash(signed)]
        assert healed.status is Status.SKIPPED
        assert healed.job is Job.MEDIA  # the routing mark carried forward

    def test_mark_prefers_the_canonical_match_when_both_keys_exist(self, instance):
        raw = f"{URL}?si=abc123"  # canonicalizes to URL; ledgered verbatim as media
        write_item(instance)
        fetch = lambda _unit: Content(meta={}, body="b" * 400, media=[raw])  # noqa: E731
        image = HttpResponse(status=200, content_type="image/png", body=b"png")
        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch), transport=FakeTransport({raw: image}))
        run_mod.run(ctx)
        run_mod.mark(ctx, raw, Status.DEAD, reason="the page is gone")
        entries = ledger.load(instance.ledger_path)
        assert entries[work_hash(URL)].status is Status.DEAD  # canonical wins
        assert entries[work_hash(raw)].status is Status.DONE  # the media line untouched

    def test_verbs_survive_a_non_utf8_corpus_item(self, instance):
        # The derived-frontmatter refresh follows the write: a
        # UnicodeDecodeError there used to exit 1 AFTER the ledger (or
        # passes) line had landed, and the retry duplicated the record.
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        (instance.corpus_dir / "2026" / f"{ITEM}.md").write_bytes(b"---\nid: x\n---\n\xff\xfe")
        run_mod.mark(ctx, URL, Status.DEAD, reason="the source is gone")
        assert entry_for(ctx).status is Status.DEAD
        assert run_mod.record_pass(ctx, ITEM, "digest").startswith("recorded")
        assert instance.passes_path.read_text().count("\n") == 1  # one record, no retry

    def test_mark_error_is_refused(self, instance):
        ctx = make_ctx(instance, FakeDriver())
        with pytest.raises(ValueError, match="engine outcomes"):
            run_mod.mark(ctx, URL, Status.ERROR)

    def test_record_pass_appends_with_harvest_rules_version(self, instance):
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.record_pass(ctx, ITEM, "harvest")
        run_mod.record_pass(ctx, ITEM, "digest")
        lines = [
            json.loads(line)
            for line in instance.passes_path.read_text().split("\n")
            if line.strip()
        ]
        assert lines[0] == {
            "stage": "harvest",
            "item": ITEM,
            "date": "2026-08-20",
            "rules": run_mod.HARVEST_RULES_VERSION,
        }
        assert "rules" not in lines[1]

    def test_record_pass_unknown_stage_is_loud(self, instance):
        ctx = make_ctx(instance, FakeDriver())
        with pytest.raises(ValueError, match="stage"):
            run_mod.record_pass(ctx, ITEM, "meditate")

    def test_record_pass_rejects_an_item_that_does_not_exist(self, instance):
        ctx = make_ctx(instance, FakeDriver())
        with pytest.raises(ValueError, match="no corpus item"):
            run_mod.record_pass(ctx, "2026-08-19-typo-ffffff", "digest")
        assert not instance.passes_path.exists()  # nothing was written

    def test_record_pass_rejects_a_multi_line_blob_of_ids(self, instance):
        # The field defect: 23 ids pasted as one argument were accepted as
        # one item and written as one record no item could ever claim —
        # while the session read every stage as covered.
        write_item(instance)
        blob = f"{ITEM}\n2026-08-19-second-aaaaaa\n2026-08-19-third-bbbbbb"
        ctx = make_ctx(instance, FakeDriver())
        with pytest.raises(ValueError, match="no corpus item"):
            run_mod.record_pass(ctx, blob, "digest")
        assert not instance.passes_path.exists()

    def test_record_pass_error_names_the_blob_on_one_line_and_bounded(self, instance):
        blob = "\n".join(f"2026-08-19-item-{i:06d}" for i in range(23))
        ctx = make_ctx(instance, FakeDriver())
        with pytest.raises(ValueError, match="no corpus item") as excinfo:
            run_mod.record_pass(ctx, blob, "digest")
        message = str(excinfo.value)
        assert "\n" not in message
        assert len(message) < 200

    def test_compact_reports_removed_lines(self, instance):
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)  # queued + done = 2 lines, 1 hash
        assert run_mod.compact(ctx) == "compacted: 1 superseded line removed"


class TestStatusReport:
    def test_counts_waiting_cohorts_and_orphans(self, instance):
        write_item(instance)
        fetch = lambda _unit: NeedsCapability(need=Need.TRANSCRIBE, reason="no captions")  # noqa: E731
        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        orphan_dir = instance.enrichment_dir / "2026-08-19-orphan-abcdef"
        orphan_dir.mkdir()
        (orphan_dir / "web-abc123.md").write_text("enriched, never digested")
        report = run_mod.status_report(ctx)
        assert "waiting" in report
        assert "transcribe" in report
        assert "2026-08-19-orphan-abcdef" in report
        assert "### Digest these" in report

    def test_parked_work_that_outlived_its_run_is_still_named(self, instance):
        # The session-end invariant: the entries that survive a session are
        # waiting/blocked/error/manual, each parked for a stated reason and
        # each printed. The run report prints what ITS run parked, so a unit
        # parked months ago appeared on no surface at all — `**manual** 1`
        # was the whole of it, while its item sat permanently raw.
        write_item(instance)
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(URL),
                url=URL,
                item=ITEM,
                kind=Kind.WEB,
                status=Status.MANUAL,
                engine="0.2.0",
                date=datetime.date(2026, 5, 19),
                reason="no extractor for this format",
            ),
        )
        ctx = make_ctx(instance, FakeDriver())
        assert "Nothing to report from this run" in run_mod.run(ctx)  # nothing drainable
        report = run_mod.status_report(ctx)
        assert "### Needs you — 1 entry only you can move" in report
        assert ITEM in report
        assert URL in report
        assert "no extractor for this format" in report

    def test_a_parked_unit_is_named_under_the_item_that_owns_it(self, instance):
        # The standing view asks the corpus who owns the unit, like every
        # other surface: a renamed item's parked work is the renamed item's.
        write_item(instance, "2026-08-19-new-slug-55ad7b")
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(URL),
                url=URL,
                item="2026-08-19-old-slug-55ad7b",
                kind=Kind.WEB,
                status=Status.MANUAL,
                engine="0.2.0",
                date=datetime.date(2026, 5, 19),
                reason="rescue by hand",
            ),
        )
        report = run_mod.status_report(make_ctx(instance, FakeDriver()))
        assert "- **2026-08-19-new-slug-55ad7b** · `manual`" in report
        assert "old-slug" not in report

    def test_a_waiting_unit_under_an_active_provider_reads_as_drainable(self, instance):
        # The field defect: the same output listed whisper as active and
        # captioned the waiting unit "retries when a provider appears".
        write_item(instance)
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(URL),
                url=URL,
                item=ITEM,
                kind=Kind.WEB,
                status=Status.WAITING,
                needs=Need.TRANSCRIBE,
                reason="no captions",
                engine="0.2.0",
                date=TODAY,
            ),
        )
        caps = Capabilities(transcribers=(FakeTranscriber(),), extractors=())
        report = run_mod.status_report(
            make_ctx(instance, FakeDriver(), capabilities=caps, provider_available=caps.available)
        )
        assert "a provider is active — drains on the next run" in report
        assert "retries when a provider appears" not in report

        bare = run_mod.status_report(make_ctx(instance, FakeDriver()))
        assert "retries when a provider appears" in bare  # no provider: the old truth

    def test_a_unit_on_the_cognitive_floor_is_the_readers_work_not_the_engines(self, instance):
        # The same defect from the other side: the capability report lists
        # the floor as OCR's active provider while nothing mechanical will
        # ever appear for the unit, and the standing view captioned it
        # "retries when a provider appears" under the engine's retry list.
        write_item(instance)
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(URL),
                url=URL,
                item=ITEM,
                kind=Kind.WEB,
                status=Status.WAITING,
                needs=Need.OCR,
                reason="no mechanical OCR provider",
                engine="0.2.0",
                date=TODAY,
            ),
        )
        caps = Capabilities(transcribers=(), extractors=())
        report = run_mod.status_report(
            make_ctx(instance, FakeDriver(), capabilities=caps, provider_available=caps.available)
        )
        assert "Needs you — 1 entry only you can move" in report
        assert "no provider will appear — you read this one" in report
        assert "retries when a provider appears" not in report
        assert "### ocr — 1 provider" in report  # the floor, still listed active

    def test_a_format_no_extractor_supports_is_cognitive_though_another_is_active(self, instance):
        # The capability report is a format-blind inventory: with a
        # CSV-only extractor active, a PDF unit still falls to the floor.
        # Read as "a provider is active" it captioned the PDF unit a retry.
        write_item(instance)
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(URL),
                url=URL,
                item=ITEM,
                kind=Kind.FILE,
                format=Format.PDF,
                status=Status.WAITING,
                needs=Need.EXTRACT,
                reason="no mechanical extractor supports pdf",
                engine="0.2.0",
                date=TODAY,
            ),
        )
        caps = Capabilities(
            transcribers=(),
            extractors=(FakeExtractor("csv-only", formats=frozenset({Format.CSV})),),
        )
        report = run_mod.status_report(
            make_ctx(instance, FakeDriver(), capabilities=caps, provider_available=caps.available)
        )
        assert "**csv-only** · `active`" in report
        assert "no provider will appear — you read this one" in report
        assert "retries when a provider appears" not in report

    def test_item_view_lists_every_unit_with_provenance(self, instance):
        write_item(instance)

        def fetch(unit):
            if unit.depth == 0:
                return Content(meta={"title": "t"}, body="substantial body " * 30)
            return NeedsCapability(need=Need.TRANSCRIBE, reason="no captions")

        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        run_mod.fetch_urls(ctx, ITEM, ["https://example.test/child"])
        report = run_mod.status_report(ctx, item_id=ITEM)
        assert report.startswith(f"## Item {ITEM} — 2 units")
        assert "done" in report
        assert f"↳ wrote `enrichment/{ITEM}/web-" in report
        assert "waiting" in report
        assert "needs `transcribe`" in report
        assert "· via harvest, depth 1" in report
        assert "capabilities" not in report  # a ledger query, not the summary

    def test_item_view_without_units_is_honest(self, instance):
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.status_report(ctx, item_id="2026-08-19-note-aaaaaa")
        assert "No ledger work units" in report

    def test_digested_items_are_not_orphans(self, instance):
        item_dir = instance.enrichment_dir / ITEM
        item_dir.mkdir()
        out = item_dir / "web-abc123.md"
        out.write_text("enriched")
        instance.digests_dir.mkdir(parents=True)
        digest = instance.digests_dir / f"{ITEM}.md"
        digest.write_text("digested")
        past = out.stat().st_mtime - 100
        os.utime(out, (past, past))  # enrichment predates the digest
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.status_report(ctx)
        assert ITEM not in report

    def _enriched_and_digested(self, instance, *, enriched, digested) -> None:
        """One item with a done ledger line and a recorded digest pass."""
        item_dir = instance.enrichment_dir / ITEM
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / "web-abc123.md").write_text("enriched", encoding="utf-8")
        instance.digests_dir.mkdir(parents=True, exist_ok=True)
        (instance.digests_dir / f"{ITEM}.md").write_text("digested", encoding="utf-8")
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(URL),
                url=URL,
                item=ITEM,
                kind=Kind.WEB,
                status=Status.DONE,
                engine="0.2.0",
                date=enriched,
                path=f"enrichment/{ITEM}/web-abc123.md",
            ),
        )
        instance.passes_path.write_text(
            json.dumps({"stage": "digest", "item": ITEM, "date": digested.isoformat()}) + "\n",
            encoding="utf-8",
        )

    def test_a_clone_still_sees_stale_enrichment(self, instance):
        # Every file carries the checkout's mtime after a clone; the dates
        # in the ledger and the pass record are what survive git.
        self._enriched_and_digested(
            instance, enriched=datetime.date(2026, 8, 20), digested=datetime.date(2026, 8, 18)
        )
        stamp = (instance.digests_dir / f"{ITEM}.md").stat().st_mtime
        for path in instance.root.rglob("*"):
            if path.is_file():
                os.utime(path, (stamp, stamp))
        assert run_mod.digest_orphans(instance) == [ITEM]

    def test_same_day_enrich_then_digest_is_not_stale(self, instance):
        day = datetime.date(2026, 8, 20)
        self._enriched_and_digested(instance, enriched=day, digested=day)
        assert run_mod.digest_orphans(instance) == []

    def test_a_digest_pass_after_the_enrichment_is_not_stale(self, instance):
        self._enriched_and_digested(
            instance, enriched=datetime.date(2026, 8, 18), digested=datetime.date(2026, 8, 20)
        )
        assert run_mod.digest_orphans(instance) == []

    def test_enrichment_with_no_digest_is_still_an_orphan(self, instance):
        self._enriched_and_digested(
            instance, enriched=datetime.date(2026, 8, 20), digested=datetime.date(2026, 8, 20)
        )
        (instance.digests_dir / f"{ITEM}.md").unlink()
        assert run_mod.digest_orphans(instance) == [ITEM]

    def test_an_unreadable_ledger_makes_no_staleness_claim(self, instance):
        self._enriched_and_digested(
            instance, enriched=datetime.date(2026, 8, 20), digested=datetime.date(2026, 8, 18)
        )
        instance.ledger_path.write_text("{not json\n", encoding="utf-8")
        assert run_mod.digest_orphans(instance) == []

    SECOND_URL = "https://example.test/second"

    def _second_done_unit(self, instance, on: datetime.date) -> None:
        """Another landed unit on the same item, with its own date."""
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(self.SECOND_URL),
                url=self.SECOND_URL,
                item=ITEM,
                kind=Kind.WEB,
                status=Status.DONE,
                engine="0.2.0",
                date=on,
                path=f"enrichment/{ITEM}/web-def456.md",
            ),
        )

    def test_the_newest_landing_decides_staleness_not_the_oldest(self, instance):
        # Two units, one digest pass between them: the item was digested
        # after the first landed and before the second, so it IS stale.
        self._enriched_and_digested(
            instance, enriched=datetime.date(2026, 8, 18), digested=datetime.date(2026, 8, 19)
        )
        self._second_done_unit(instance, datetime.date(2026, 8, 20))
        assert run_mod.digest_orphans(instance) == [ITEM]

    def test_the_newest_digest_pass_decides_it_too_not_the_oldest(self, instance):
        # Two digest passes, the enrichment between them: the item has
        # been digested since it last changed, so it is NOT stale.
        self._enriched_and_digested(
            instance, enriched=datetime.date(2026, 8, 19), digested=datetime.date(2026, 8, 18)
        )
        with instance.passes_path.open("a", encoding="utf-8") as passes:
            passes.write(json.dumps({"stage": "digest", "item": ITEM, "date": "2026-08-20"}) + "\n")
        assert run_mod.digest_orphans(instance) == []

    def test_a_digest_predating_the_pass_record_is_not_stale(self, instance):
        # The deliberate legacy exemption: a digest written before passes
        # were recorded at all has no date to compare. No dates on either
        # side is no claim — never a staleness finding by default.
        self._enriched_and_digested(
            instance, enriched=datetime.date(2026, 8, 20), digested=datetime.date(2026, 8, 18)
        )
        instance.passes_path.write_text("", encoding="utf-8")
        assert run_mod.digest_orphans(instance) == []

    PARKED_URL = "https://example.test/parked"

    def _second_unit(self, instance, status: Status, **fields) -> None:
        """A second unit on the same item — the one that owes the work."""
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(self.PARKED_URL),
                url=self.PARKED_URL,
                item=ITEM,
                kind=Kind.WEB,
                status=status,
                engine="0.2.0",
                date=datetime.date(2026, 8, 20),
                reason="rescue by hand",
                **fields,
            ),
        )

    def test_an_item_still_owing_a_unit_is_never_a_digest_orphan(self, instance):
        # A `manual` unit holds the item `raw`, and the skill forbids
        # digesting a raw item — so listing it as a digest to do reports
        # the same permanently-parked item on every run, forever.
        self._enriched_and_digested(
            instance, enriched=datetime.date(2026, 8, 20), digested=datetime.date(2026, 8, 20)
        )
        (instance.digests_dir / f"{ITEM}.md").unlink()
        self._second_unit(instance, Status.MANUAL)
        assert run_mod.digest_orphans(instance) == []

    def test_a_stale_digest_is_not_claimed_while_a_unit_is_still_owed(self, instance):
        self._enriched_and_digested(
            instance, enriched=datetime.date(2026, 8, 20), digested=datetime.date(2026, 8, 18)
        )
        self._second_unit(instance, Status.BLOCKED, attempts=2)
        assert run_mod.digest_orphans(instance) == []

    def test_the_item_is_listed_the_moment_its_last_unit_lands(self, instance):
        # The other half: the skip is owed work, not the parked status.
        self._enriched_and_digested(
            instance, enriched=datetime.date(2026, 8, 20), digested=datetime.date(2026, 8, 20)
        )
        (instance.digests_dir / f"{ITEM}.md").unlink()
        self._second_unit(instance, Status.MANUAL)
        self._second_unit(instance, Status.SKIPPED)  # last line per hash wins
        assert run_mod.digest_orphans(instance) == [ITEM]


class TestIssueFiling:
    """The filer wiring: error outcomes reach it; the report says so."""

    def _crashing_ctx(self, instance, gh):
        write_item(instance)

        def fetch(_unit):
            raise RuntimeError("engine bug")

        return make_ctx(instance, FakeDriver(fetch_fn=fetch), gh=gh)

    def test_error_outcomes_file_and_appear_on_the_report(self, instance):
        gh = FakeGh()
        report = run_mod.run(self._crashing_ctx(instance, gh))
        assert len(gh.of("create")) == 1
        assert "**Reported upstream** — 1 issue filed" in report
        assert "filed engine issue" in report
        assert (instance.state_dir / "issue-reports.jsonl").exists()

    def test_world_failures_never_file(self, instance):
        write_item(instance)
        gh = FakeGh()
        fetch = lambda _unit: Refused(evidence="HTTP 503")  # noqa: E731
        run_mod.run(make_ctx(instance, FakeDriver(fetch_fn=fetch), gh=gh))
        assert gh.calls == []  # blocked/dead/manual/waiting are not engine bugs

    def test_report_issues_false_files_nothing_but_records_locally(self, instance):
        gh = FakeGh()
        ctx = dataclasses.replace(
            self._crashing_ctx(instance, gh), config=Config(report_issues=False)
        )
        report = run_mod.run(ctx)
        assert gh.calls == []
        assert "reported upstream" not in report
        # The gate stops upstream filing only — the observation still lands
        # in the local memory, filed: false, and the report says so.
        assert "recorded locally" in report
        record = json.loads((instance.state_dir / "issue-reports.jsonl").read_text().split("\n")[0])
        assert record["filed"] is False

    def test_filer_failure_is_a_note_and_the_run_completes(self, instance):
        report = run_mod.run(self._crashing_ctx(instance, refuse_gh))
        assert "issue filing failed" in report
        assert "error" in report  # the unit outcome is still ledgered and reported

    def test_torn_memory_never_loses_the_run_report(self, instance):
        (instance.state_dir / "issue-reports.jsonl").write_text('{"note": "torn"}\n')
        report = run_mod.run(self._crashing_ctx(instance, FakeGh()))
        assert "issue filing failed" in report
        assert "## Enrich run" in report  # the report itself survived


class TestNoSourceItems:
    """Text/image-only captures seed nothing — the report derives them."""

    def _text_item(self, instance, item_id="2026-08-19-a-thought-aaaaaa"):
        item = corpus.CorpusItem(
            id=item_id,
            source="inbox",
            channel="inbox",
            shared_by="Alex",
            date=datetime.date(2026, 8, 19),
            urls=[],
            kinds=["text"],
            body="a standalone observation\n",
        )
        corpus.write_item(instance.corpus_dir / "2026" / f"{item_id}.md", item)
        return item_id

    def test_raw_text_item_surfaces_as_cognitive_work(self, instance):
        item_id = self._text_item(instance)
        report = run_mod.run(make_ctx(instance, FakeDriver()))
        assert item_id in report
        assert "no-source item — awaiting description + digest" in report

    def test_digested_item_stops_listing(self, instance):
        item_id = self._text_item(instance)
        instance.digests_dir.mkdir(parents=True, exist_ok=True)
        (instance.digests_dir / f"{item_id}.md").write_text("digested\n")
        report = run_mod.run(make_ctx(instance, FakeDriver()))
        assert "no-source item" not in report

    def test_items_with_units_are_not_no_source(self, instance):
        write_item(instance)  # has a URL — its units drive the report instead
        report = run_mod.run(make_ctx(instance, FakeDriver()))
        assert "no-source item" not in report

    def test_a_capture_that_seeded_nothing_still_derives_raw(self, instance):
        # The one item the directory listing still answers for: no units and
        # no files is a text-only share, which owes its description and its
        # digest — and an `any()` over no units would call it finished.
        item_id = self._text_item(instance)
        run_mod.run(make_ctx(instance, FakeDriver()))
        path = instance.corpus_dir / "2026" / f"{item_id}.md"
        assert corpus.read_item(path).status == "raw"

    def test_only_the_full_run_derives_the_listing(self, instance):
        self._text_item(instance)
        report = run_mod.run_transcribe(make_ctx(instance, FakeDriver()))
        assert "no-source item" not in report


class TestClosedWithoutContentItems:
    """Every unit dead or ruled out — the item owes its digest from the note."""

    ALL_DEAD_REASON = "every source dead or ruled out — awaiting description + digest from the note"

    def _dead_ctx(self, instance):
        write_item(instance)
        return make_ctx(instance, FakeDriver(fetch_fn=lambda _u: Missing(evidence="HTTP 404")))

    def test_named_on_the_landing_run_report(self, instance):
        report = run_mod.run(self._dead_ctx(instance))
        assert ITEM in report
        assert self.ALL_DEAD_REASON in report

    def test_listed_by_the_digest_backstop_on_both_surfaces(self, instance):
        run_mod.run(self._dead_ctx(instance))
        assert run_mod.digest_orphans(instance) == [ITEM]
        report = run_mod.status_report(make_ctx(instance, FakeDriver()))
        assert "Digest these" in report
        assert ITEM in report

    def test_drops_off_both_surfaces_once_digested(self, instance):
        ctx = self._dead_ctx(instance)
        run_mod.run(ctx)
        instance.digests_dir.mkdir(parents=True, exist_ok=True)
        (instance.digests_dir / f"{ITEM}.md").write_text("digested\n", encoding="utf-8")
        run_mod.record_pass(ctx, ITEM, "digest")
        assert run_mod.digest_orphans(instance) == []
        report = run_mod.run(self._dead_ctx(instance))
        assert self.ALL_DEAD_REASON not in report

    def test_an_item_with_one_landed_unit_is_not_all_dead(self, instance):
        write_item(instance, urls=[URL, "https://example.test/alive"])

        def fetch(unit):
            if unit.url == URL:
                return Missing(evidence="HTTP 404")
            return Content(meta={"title": "t"}, body="substantial body " * 30)

        report = run_mod.run(make_ctx(instance, FakeDriver(fetch_fn=fetch)))
        assert self.ALL_DEAD_REASON not in report

    def test_a_dead_and_skipped_item_owes_the_same_note_digest(self, instance):
        # The skills' own heal: a dead unit's sibling is marked skipped
        # with the reason stated. The item owes exactly what an all-dead
        # one owes — dead-only as the predicate made it vanish from the
        # report and the backstop while its digest stayed owed.
        write_item(instance, urls=[URL, "https://example.test/sibling"])
        ctx = make_ctx(instance, FakeDriver(fetch_fn=lambda _u: Missing(evidence="HTTP 404")))
        run_mod.run(ctx)
        run_mod.mark(
            ctx,
            "https://example.test/sibling",
            Status.SKIPPED,
            reason="sibling of a dead link — ruled out",
        )
        report = run_mod.run(ctx)
        assert self.ALL_DEAD_REASON in report
        assert run_mod.digest_orphans(instance) == [ITEM]

    def test_an_all_skipped_item_owes_the_same_note_digest(self, instance):
        write_item(instance)
        nothing_there = Unusable(evidence="a site root — no content unit", rescuable=False)
        report = run_mod.run(make_ctx(instance, FakeDriver(fetch_fn=lambda _u: nothing_there)))
        assert self.ALL_DEAD_REASON in report
        assert run_mod.digest_orphans(instance) == [ITEM]

    def test_a_dead_ghost_item_is_never_listed(self, instance):
        # A dead unit whose item has no corpus file is lint's ghost-item
        # finding — never work the backstop can ask anyone to digest.
        run_mod.run(self._dead_ctx(instance))
        (instance.corpus_dir / "2026" / f"{ITEM}.md").unlink()
        assert run_mod.digest_orphans(instance) == []


PHOTO_ITEM = "2026-08-19-photo-abc123"


class TestUndescribedMedia:
    """Media on disk owes a description — counted per item, never slot-matched."""

    def _item_dir(self, instance, item_id: str = ITEM) -> Path:
        item_dir = instance.enrichment_dir / item_id
        item_dir.mkdir(parents=True, exist_ok=True)
        return item_dir

    def _stated(self, instance, *repo_paths: str) -> list[str]:
        """The stated paths, materialized — only media on disk is countable."""
        for repo_path in repo_paths:
            path = instance.root / repo_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"bytes")
        return list(repo_paths)

    def test_stated_binaries_count_with_no_enrichment_directory(self, instance):
        media = self._stated(instance, "media/x/a.jpg", "media/x/b.pdf")
        row = run_mod.undescribed_media(instance, ITEM, media)
        assert row == {"item": ITEM, "binaries": 2, "described": 0}

    def test_a_download_in_the_enrichment_directory_counts_too(self, instance):
        # A media capture states its path; a downloaded one never reaches
        # frontmatter, so the directory is the only place it shows.
        (self._item_dir(instance) / "media-0.jpg").write_bytes(PNG_BYTES)
        assert run_mod.undescribed_media(instance, ITEM, []) == {
            "item": ITEM,
            "binaries": 1,
            "described": 0,
        }

    def test_a_stated_markdown_path_owes_a_description_too(self, instance):
        # Nothing transcribes or extracts an attached document, so the row is
        # the only thing that ever sends a session to read it.
        media = self._stated(instance, "media/x/notes.md")
        assert run_mod.undescribed_media(instance, ITEM, media) == {
            "item": ITEM,
            "binaries": 1,
            "described": 0,
        }

    def test_a_summarized_markdown_path_clears(self, instance):
        (self._item_dir(instance) / "media-0.md").write_text("what it says\n", encoding="utf-8")
        media = self._stated(instance, "media/x/notes.md")
        assert run_mod.undescribed_media(instance, ITEM, media) is None

    def test_a_stated_path_naming_no_file_counts_nothing(self, instance):
        # The phantom: a stated path no file answers to. No description
        # could ever close a row for it — not one written for something
        # else in the directory either.
        assert run_mod.undescribed_media(instance, ITEM, ["media/x/gone.jpg"]) is None
        (self._item_dir(instance) / "media-0.md").write_text("what it says\n", encoding="utf-8")
        assert run_mod.undescribed_media(instance, ITEM, ["media/x/gone.jpg"]) is None

    def test_the_stated_paths_on_disk_are_counted_beside_a_phantom(self, instance):
        media = [*self._stated(instance, "media/x/a.jpg"), "media/x/gone.jpg"]
        assert run_mod.undescribed_media(instance, ITEM, media) == {
            "item": ITEM,
            "binaries": 1,
            "described": 0,
        }

    def test_a_path_escaping_the_root_counts_nothing(self, instance):
        # It resolves to a real file, which is exactly why it is not
        # countable: seeding parks it manual and never reads it.
        outside = instance.root.parent / "secret.pdf"
        outside.write_bytes(b"%PDF-1.4 not for the corpus")
        assert run_mod.undescribed_media(instance, ITEM, [str(outside), "../secret.pdf"]) is None

    def test_an_unpulled_lfs_pointer_counts_like_any_stated_file(self, instance):
        # What counts is presence on disk, never format: the pointer is a
        # short text file where the image will be, and the description a
        # session owes it is the same one either way.
        pointer = instance.root / "media" / "x" / "scan.png"
        pointer.parent.mkdir(parents=True)
        pointer.write_text("version https://git-lfs.github.com/spec/v1\n", encoding="utf-8")
        assert run_mod.undescribed_media(instance, ITEM, ["media/x/scan.png"]) == {
            "item": ITEM,
            "binaries": 1,
            "described": 0,
        }

    def test_extraction_assets_owe_no_description(self, instance):
        # A figure lifted out of an already-extracted document is not a
        # capture: charging a description for each invents work.
        (self._item_dir(instance) / "abc123-asset-0.png").write_bytes(PNG_BYTES)
        assert run_mod.undescribed_media(instance, ITEM, []) is None

    def test_a_half_written_download_is_neither(self, instance):
        # An interrupted download's temp counted as a binary opens a row no
        # description could ever close; counted as a description it retires
        # the row the file beside it is owed.
        item_dir = self._item_dir(instance)
        (item_dir / "media-0.jpg").write_bytes(PNG_BYTES)
        (item_dir / f"media-0.jpg.abc123{atomic.TEMP_SUFFIX}").write_bytes(b"half")
        assert run_mod.undescribed_media(instance, ITEM, []) == {
            "item": ITEM,
            "binaries": 1,
            "described": 0,
        }

    def test_the_rest_of_the_enrichment_directory_is_not_counted(self, instance):
        # Every item with a fetched URL and a media download holds both
        # kinds of file in one directory, in whatever order it lists them.
        item_dir = self._item_dir(instance)
        (item_dir / "web-abc123.md").write_text("the fetched page\n", encoding="utf-8")
        (item_dir / "media-0.jpg").write_bytes(PNG_BYTES)
        (item_dir / "media-0.md").write_text("what it depicts\n", encoding="utf-8")
        media = self._stated(instance, "media/x/a.jpg")
        assert run_mod.undescribed_media(instance, ITEM, media) == {
            "item": ITEM,
            "binaries": 2,
            "described": 1,
        }

    def test_descriptions_are_counted_until_they_cover_the_media(self, instance):
        item_dir = self._item_dir(instance)
        (item_dir / "media-0.jpg").write_bytes(PNG_BYTES)
        (item_dir / "media-1.jpg").write_bytes(PNG_BYTES)
        (item_dir / "media-0.md").write_text("what the first depicts\n", encoding="utf-8")
        assert run_mod.undescribed_media(instance, ITEM, []) == {
            "item": ITEM,
            "binaries": 2,
            "described": 1,
        }
        (item_dir / "media-1.md").write_text("and the second\n", encoding="utf-8")
        assert run_mod.undescribed_media(instance, ITEM, []) is None

    def _photo_item(self, instance, files: int = 1) -> str:
        """A media capture whose files are on disk, states them, describes none."""
        media = [f"media/{PHOTO_ITEM}/photo-{n}.jpg" for n in range(files)]
        write_item(instance, PHOTO_ITEM, urls=[], kinds=["image"], media=media)
        for repo_path in media:
            path = instance.root / repo_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG_BYTES)
        return PHOTO_ITEM

    def _describe(self, instance, slot: int, item_id: str = PHOTO_ITEM) -> None:
        item_dir = self._item_dir(instance, item_id)
        (item_dir / f"media-{slot}.md").write_text("what it depicts\n", encoding="utf-8")

    def test_the_run_report_names_the_item_and_its_counts(self, instance, monkeypatch):
        self._photo_item(instance, files=3)
        self._describe(instance, 0)
        captured: dict[str, dict] = {}
        rendered = run_mod.surfaces.render

        def spy(surface, payload):
            captured[surface] = dict(payload)
            return rendered(surface, payload)

        monkeypatch.setattr(run_mod.surfaces, "render", spy)
        report = run_mod.run(make_ctx(instance, FakeDriver()))
        assert captured["enrich-report"]["undescribed_media"] == [
            {"item": PHOTO_ITEM, "binaries": 3, "described": 1}
        ]
        # The section is the surface's, off that payload — never hand-drawn.
        assert "### Describe these — 1 item carries media nobody has described" in report
        assert f"- **{PHOTO_ITEM}**" in report
        assert "1 of 3 media files described" in report
        assert f"enrichment/{PHOTO_ITEM}/media-<n>.md" in report

    def test_listed_though_the_item_is_enriched_and_digested(self, instance):
        # The whole point: an item whose units have all landed leaves the
        # unit-driven report, which is where its undescribed media hid.
        path = write_item(
            instance,
            PHOTO_ITEM,
            urls=[],
            kinds=["image"],
            media=self._stated(instance, f"media/{PHOTO_ITEM}/a.jpg", f"media/{PHOTO_ITEM}/b.jpg"),
        )
        self._describe(instance, 0)
        instance.digests_dir.mkdir(parents=True, exist_ok=True)
        (instance.digests_dir / f"{PHOTO_ITEM}.md").write_text("digested\n", encoding="utf-8")
        report = run_mod.run(make_ctx(instance, FakeDriver()))
        assert corpus.read_item(path).status == "enriched"
        assert "1 of 2 media files described" in report

    def test_it_persists_across_runs_until_the_description_lands(self, instance):
        self._photo_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        assert "Describe these" in run_mod.run(ctx)
        assert "Describe these" in run_mod.run(ctx)
        self._describe(instance, 0)
        assert "Describe these" not in run_mod.run(ctx)

    def test_a_described_item_is_never_named(self, instance):
        self._photo_item(instance)
        self._describe(instance, 0)
        assert "Describe these" not in run_mod.run(make_ctx(instance, FakeDriver()))

    def test_an_item_with_no_media_is_never_named(self, instance):
        write_item(instance)
        assert "Describe these" not in run_mod.run(make_ctx(instance, FakeDriver()))

    def test_only_the_full_run_carries_the_queue(self, instance):
        # A transcribe drain or a promotion reports on the units it moved;
        # the standing queue belongs to the run and to `enrich status`.
        self._photo_item(instance)
        report = run_mod.run_transcribe(make_ctx(instance, FakeDriver()))
        assert "Describe these" not in report

    def test_the_standing_listing_appears_and_clears_on_the_same_rule(self, instance):
        self._photo_item(instance, files=2)
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.status_report(ctx)
        assert "### Describe these — 1 item carries media nobody has described" in report
        assert "0 of 2 media files described" in report
        self._describe(instance, 0)
        self._describe(instance, 1)
        assert "Describe these" not in run_mod.status_report(ctx)

    def test_an_unreadable_corpus_item_makes_no_row_and_stops_nothing(self, instance):
        # Naming it is seeding's job and lint's; this listing states no
        # media for a file it could not read — and keeps walking, or one
        # corrupt item hides every describe row sorting behind it.
        (instance.corpus_dir / "2026").mkdir(parents=True, exist_ok=True)
        (instance.corpus_dir / "2026" / "2026-08-19-aaa-000000.md").write_text("not an item\n")
        self._photo_item(instance)
        assert run_mod.items_owing_descriptions(instance) == [
            {"item": PHOTO_ITEM, "binaries": 1, "described": 0}
        ]


ALPHA = "2026-08-19-alpha-111111"
BRAVO = "2026-08-19-bravo-222222"
OLD_ITEM = "2026-08-19-old-slug-55ad7b"
NEW_ITEM = "2026-08-19-new-slug-55ad7b"


class TestOwnershipIsTheCorpusAnswer:
    """Who owns a unit is asked of the corpus, never of the stored string.

    A ledger line's ``item`` is the attribution as of the day it was
    written, and migration 1 carries a stated item verbatim — so an item
    renamed since then has every one of its lines naming an id no corpus
    file answers to, and an ``exclude`` that keeps a line another live item
    claims leaves it naming the purged one.

    Asked on the way in AND on the way out: every reader resolves the work
    to the live item, and so does every write, so the stored string is
    never healed. A stale ``item`` on an old line is history.
    """

    def _renamed(self, instance, **fields) -> Path:
        """One item, renamed since its only ledger line was written."""
        path = write_item(instance, NEW_ITEM)
        out = instance.enrichment_dir / NEW_ITEM / f"web-{work_hash(URL)[:6]}.md"
        out.parent.mkdir(parents=True)
        out.write_text(f"---\nurl: {URL}\nfetched: '2026-08-01'\n---\n\nthe body\n")
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(URL),
                url=URL,
                item=OLD_ITEM,
                kind=Kind.WEB,
                engine="0.2.0",
                date=datetime.date(2026, 8, 1),
                **fields,
            ),
        )
        return path

    def _waiting(self, instance) -> Path:
        return self._renamed(
            instance, status=Status.WAITING, needs=Need.TRANSCRIBE, reason="no captions"
        )

    def test_a_cognitive_units_pointer_names_the_live_id_on_every_surface(self, instance):
        # The cognitive row is the manual-work pointer. Run report and lint
        # spelled the stored string while `enrich status` resolved the
        # owner, so one unit carried two ids across the surfaces — and the
        # pointer sent the session into enrichment/<dead-id>/, a directory
        # the rename moved away.
        self._renamed(instance, status=Status.WAITING, needs=Need.OCR, reason="scanned pages")
        ctx = make_ctx(
            instance, FakeDriver(), capabilities=Capabilities(transcribers=(), extractors=())
        )
        run_report = run_mod.run(ctx)
        status = run_mod.status_report(ctx, item_id=NEW_ITEM)
        (instance.state_dir / "taxonomy.json").write_text('{"topics": {}, "entities": {}}')
        lint_report = run_lint(
            instance, is_cognitive=lambda _need, _fmt=None: True, today=lambda: TODAY, write=False
        ).report
        assert f"**{NEW_ITEM}** · `ocr`" in run_report
        assert f"**{NEW_ITEM}** · `ocr`" in lint_report
        assert URL in status  # enrich status: the same unit, under the same id
        for report in (run_report, lint_report):
            assert f"{OLD_ITEM}** · `ocr`" not in report

    def test_a_renamed_item_owing_work_is_never_written_enriched(self, instance):
        # The whole point of deriving status from the ledger: an item
        # holding a waiting unit is raw, whatever id the line spells.
        path = self._waiting(instance)
        run_mod.run(make_ctx(instance, FakeDriver()))
        assert corpus.read_item(path).status == "raw"

    def test_the_renamed_items_work_shows_on_its_own_status_view(self, instance):
        self._waiting(instance)
        report = run_mod.status_report(make_ctx(instance, FakeDriver()), item_id=NEW_ITEM)
        assert "No ledger work units" not in report  # it has one, under a dead id
        assert URL in report
        assert "no captions" in report

    def test_a_renamed_item_owing_work_is_not_a_digest_orphan(self, instance):
        # It derives raw, and a raw item is one the ingest procedure
        # forbids digesting — listing it names work nobody may do.
        self._waiting(instance)
        assert run_mod.digest_orphans(instance) == []

    def test_the_stale_line_is_left_exactly_as_it_was_written(self, instance):
        # Ownership is a question the readers and the write path both ask
        # of the corpus, so the stored string never has to be healed — and
        # a run that does no work on a unit writes no line for it. A stale
        # `item` is historical attribution, and history is not repaired.
        self._waiting(instance)
        before = instance.ledger_path.read_text(encoding="utf-8")
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        assert instance.ledger_path.read_text(encoding="utf-8") == before
        assert entry_for(ctx).item == OLD_ITEM

    def test_the_renamed_items_unit_drains_into_the_live_items_enrichment(self, instance):
        # The write path asks the same question the readers ask: without it
        # `record_outcome` carries the dead id forward and `_write_output`
        # lands the file in `enrichment/<dead-id>/`, where the item that
        # owes the work cannot list it.
        self._renamed(instance, status=Status.QUEUED)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.item == NEW_ITEM
        assert entry.path == f"enrichment/{NEW_ITEM}/web-{entry.hash[:6]}.md"
        assert not (instance.enrichment_dir / OLD_ITEM).exists()

    def test_a_child_promoted_under_a_stale_parent_lands_on_the_live_item(self, instance):
        # A promotion names its parent by hash, and that line still spells
        # the dead id — so the dead id would propagate onto units written
        # this run, not just be carried on old ones.
        self._renamed(instance, status=Status.QUEUED)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        child = "https://example.test/harvested"
        run_mod.fetch_urls(ctx, NEW_ITEM, [child], parent=work_hash(URL))
        entry = ledger.load(instance.ledger_path)[work_hash(child)]
        assert entry.item == NEW_ITEM
        assert entry.path == f"enrichment/{NEW_ITEM}/web-{entry.hash[:6]}.md"

    def test_fetch_into_a_renamed_item_finds_its_primary_unit(self, instance):
        # `enrich fetch <live-id> <url>` with no --parent scans for the
        # item's depth-0 unit, which spells the dead id. Scanned on the
        # stored string it aborted "no primary work unit — pass --parent
        # <hash> explicitly", both implied causes false, for every renamed
        # item forever.
        self._renamed(instance, status=Status.DONE, path=f"enrichment/{NEW_ITEM}/web-x.md")
        ctx = make_ctx(instance, FakeDriver())
        child = "https://example.test/docs"
        run_mod.fetch_urls(ctx, NEW_ITEM, [child])
        entry = ledger.load(instance.ledger_path)[work_hash(child)]
        assert entry.item == NEW_ITEM
        assert entry.parent == work_hash(URL)  # the item's own primary unit
        assert entry.depth == 1
        assert entry.status is Status.DONE

    def test_refetching_a_renamed_items_own_url_requeues_in_place(self, instance):
        # The held line spells the dead id, so comparing the stored string
        # refused the item's OWN unit as another item's ("already enriches
        # under item <dead-id>") — the requeue-in-place route unreachable
        # for every pre-rename unit.
        self._renamed(instance, status=Status.DONE, path=f"enrichment/{NEW_ITEM}/web-x.md")
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.fetch_urls(ctx, NEW_ITEM, [URL])
        assert "already enriches" not in report
        entry = entry_for(ctx)
        assert entry.item == NEW_ITEM
        assert entry.status is Status.DONE
        assert entry.rerun is True  # requeued in place, then drained
        assert entry.parent is None  # never re-parented under itself

    def test_the_requeued_line_itself_is_written_under_the_live_id(self, instance):
        # The transient QUEUED line is a write like any other. Carried from
        # the held line it spelled the dead id — superseded by the drain's
        # outcome in the same run, but standing alone if the run dies
        # between the requeue and the drain.
        self._renamed(instance, status=Status.DONE, path=f"enrichment/{NEW_ITEM}/web-x.md")
        ctx = make_ctx(instance, FakeDriver())
        run_mod.fetch_urls(ctx, NEW_ITEM, [URL])
        queued = [
            json.loads(line)
            for line in instance.ledger_path.read_text().split("\n")
            if line.strip() and json.loads(line).get("status") == "queued"
        ]
        assert [line["item"] for line in queued] == [NEW_ITEM]

    def _spent_budget(self, instance) -> RunContext:
        """The item's 12-URL budget, spent entirely under the old id."""
        self._renamed(instance, status=Status.DONE, path=f"enrichment/{NEW_ITEM}/web-x.md")
        for n in range(MAX_URLS_PER_ITEM - 1):
            url = f"https://example.test/deep{n}"
            ledger.append(
                instance.ledger_path,
                LedgerEntry(
                    hash=work_hash(url),
                    url=url,
                    item=OLD_ITEM,
                    kind=Kind.WEB,
                    status=Status.DONE,
                    engine="0.2.0",
                    date=datetime.date(2026, 8, 1),
                    via="harvest",
                    parent=work_hash(URL),
                    depth=1,
                ),
            )
        return make_ctx(instance, FakeDriver())

    def test_the_url_budget_spans_a_rename(self, instance):
        # The cap counts the entries the item OWNS. Counted on the stored
        # string the live id owns nothing, ever, so a renamed item's budget
        # restarted from zero — unbounded fetches, no cap fire, and the
        # health check's drift reading silenced with it.
        ctx = self._spent_budget(instance)
        extra = "https://example.test/extra"
        run_mod.fetch_urls(ctx, NEW_ITEM, [extra])
        refused = ledger.load(instance.ledger_path)[work_hash(extra)]
        assert refused.status is Status.SKIPPED
        assert refused.cap is Cap.URL_REQUESTED
        assert "url cap" in (refused.reason or "")

    def test_the_url_budget_spanning_a_rename_still_honors_force(self, instance):
        # --force is the deliberate route past the URL bound, rename or not.
        ctx = self._spent_budget(instance)
        extra = "https://example.test/extra"
        run_mod.fetch_urls(ctx, NEW_ITEM, [extra], force=True)
        entry = ledger.load(instance.ledger_path)[work_hash(extra)]
        assert entry.status is Status.DONE
        assert entry.item == NEW_ITEM

    def test_a_renamed_items_unit_reaches_its_driver_under_the_live_id(self, instance):
        # `WorkUnit.item` is what a driver has to reason about the item with
        # — the thread walk-up and the harvest subject rule both read it —
        # and a dead id answers no question a driver can ask.
        self._renamed(instance, status=Status.QUEUED)
        driver = FakeDriver()
        run_mod.run(make_ctx(instance, driver))
        assert [unit.item for unit in driver.fetched] == [NEW_ITEM]

    def test_a_renamed_items_redetected_unit_is_corrected_under_the_live_id(self, instance):
        # The superseding line a mid-fetch kind discovery writes is a line
        # like any other, and it is written before any outcome resolves the
        # attribution for it.
        self._renamed(instance, status=Status.QUEUED)
        driver = FakeDriver(fetch_fn=lambda _unit: Redetected(kind=Kind.PODCAST))
        run_mod.run(make_ctx(instance, driver))
        sniffed = [
            json.loads(line)
            for line in instance.ledger_path.read_text().split("\n")
            if line.strip() and json.loads(line).get("via") == "sniff"
        ]
        assert sniffed[0]["status"] == "queued"  # the correction, ahead of its outcome
        assert sniffed[0]["item"] == NEW_ITEM

    def test_a_unit_two_live_items_share_keeps_the_id_its_line_carries(self, instance):
        # Both claim the work, so ownership has two answers and the line's
        # own is one of them. Taking the first would move the output between
        # two live items run to run, on nothing but id order.
        write_item(instance, ALPHA)
        write_item(instance, BRAVO)
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(URL),
                url=URL,
                item=BRAVO,  # the later id: alpha would win an ordered pick
                kind=Kind.WEB,
                status=Status.QUEUED,
                engine="0.2.0",
                date=datetime.date(2026, 8, 1),
            ),
        )
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.item == BRAVO
        assert entry.path == f"enrichment/{BRAVO}/web-{entry.hash[:6]}.md"

    def _media_unit(self, instance, url: str, item_id: str, parent: str) -> None:
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(url),
                url=url,
                item=item_id,
                kind=Kind.WEB,
                status=Status.QUEUED,
                engine="0.2.0",
                date=datetime.date(2026, 8, 1),
                job=Job.MEDIA,
                parent=parent,
                depth=1,
            ),
        )

    def test_a_renamed_items_media_downloads_into_the_live_items_directory(self, instance):
        # A media unit redrained from a previous run comes off the ledger
        # carrying whatever id its line was written under, so this is the
        # one download path the parent's corrected line does not cover.
        self._renamed(instance, status=Status.DONE, path=f"enrichment/{NEW_ITEM}/web-x.md")
        hero = "https://example.test/hero.png"
        self._media_unit(instance, hero, OLD_ITEM, work_hash(URL))
        transport = FakeTransport(
            {hero: HttpResponse(status=200, content_type="image/png", body=b"png")}
        )
        run_mod.run(make_ctx(instance, FakeDriver(), transport=transport))
        entry = ledger.load(instance.ledger_path)[work_hash(hero)]
        assert entry.item == NEW_ITEM
        assert entry.path == f"enrichment/{NEW_ITEM}/media-0.png"
        assert (instance.root / entry.path).read_bytes() == b"png"

    def _landed_media(self, instance, url: str, item_id: str, slot: int, body: bytes) -> None:
        """A media unit that landed before the rename, its file moved with it."""
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(url),
                url=url,
                item=item_id,
                kind=Kind.WEB,
                status=Status.DONE,
                engine="0.2.0",
                date=datetime.date(2026, 8, 1),
                job=Job.MEDIA,
                parent=work_hash(URL),
                depth=1,
                path=f"enrichment/{item_id}/media-{slot}.png",
            ),
        )
        out = instance.enrichment_dir / NEW_ITEM / f"media-{slot}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)

    def test_a_new_media_unit_never_takes_a_landed_siblings_slot(self, instance):
        # The slot is the unit's position among the item's media units, and
        # the item is the OWNING one on both sides. Counted off the stored
        # string, a sibling that landed before the rename is invisible to
        # a unit ledgered after it, which then claims slot 0 and overwrites
        # the sibling's file in place.
        self._renamed(instance, status=Status.DONE, path=f"enrichment/{NEW_ITEM}/web-x.md")
        landed, fresh = "https://example.test/a.png", "https://example.test/b.png"
        self._landed_media(instance, landed, OLD_ITEM, 0, b"first")
        self._media_unit(instance, fresh, NEW_ITEM, work_hash(URL))
        transport = FakeTransport(
            {fresh: HttpResponse(status=200, content_type="image/png", body=b"second")}
        )
        run_mod.run(make_ctx(instance, FakeDriver(), transport=transport))
        item_dir = instance.enrichment_dir / NEW_ITEM
        assert (item_dir / "media-0.png").read_bytes() == b"first"
        assert (item_dir / "media-1.png").read_bytes() == b"second"

    def test_the_media_cap_counts_the_live_items_files(self, instance):
        # The cap counts media-family files that EXIST, in the directory the
        # download would land in. Counted in the dead id's directory it
        # finds nothing there, ever, and a renamed item's media budget
        # becomes unbounded.
        self._renamed(instance, status=Status.DONE, path=f"enrichment/{NEW_ITEM}/web-x.md")
        item_dir = instance.enrichment_dir / NEW_ITEM
        for slot in range(4):  # MEDIA_MAX_FILES, all landed before the rename
            self._landed_media(instance, f"https://example.test/{slot}.png", OLD_ITEM, slot, b"png")
        fifth = "https://example.test/fifth.png"
        self._media_unit(instance, fifth, OLD_ITEM, work_hash(URL))
        transport = FakeTransport(
            {fifth: HttpResponse(status=200, content_type="image/png", body=b"png")}
        )
        run_mod.run(make_ctx(instance, FakeDriver(), transport=transport))
        entry = ledger.load(instance.ledger_path)[work_hash(fifth)]
        assert entry.status is Status.SKIPPED
        assert "media cap" in str(entry.reason)
        assert sorted(p.name for p in item_dir.glob("media-*")) == [
            f"media-{slot}.png" for slot in range(4)
        ]

    def test_mark_drops_a_superseded_output_beside_its_replacement(self, instance):
        # The stale file leaves from the directory the replacement is in.
        # Read off the line's stored item instead, the search runs in a
        # directory a rename left empty and the item keeps two files for
        # one unit, serving the pre-correction view to the digest forever.
        self._renamed(instance, status=Status.DONE, path=f"enrichment/{NEW_ITEM}/web-x.md")
        unit_hash = work_hash(URL)
        item_dir = instance.enrichment_dir / NEW_ITEM
        stale = item_dir / f"web-{unit_hash[:6]}.md"
        stale.write_text(f"---\nurl: {URL}\nfetched: '2026-08-01'\n---\n\nold\n")
        fresh = item_dir / f"podcast-{unit_hash[:6]}.md"
        fresh.write_text(f"---\nurl: {URL}\nfetched: '2026-08-02'\n---\n\nnew\n")
        run_mod.mark(
            make_ctx(instance, FakeDriver()),
            URL,
            Status.DONE,
            path=f"enrichment/{NEW_ITEM}/podcast-{unit_hash[:6]}.md",
        )
        assert not stale.exists()
        assert fresh.exists()

    def _drained_then_renamed(
        self, instance, *, assets=(), media=(), children=(), move_enrichment: bool = True
    ) -> FakeTransport:
        """A real drain under the old id, then the rename that moves it all.

        The state a run meets after a rename is whatever the drain left —
        so it is the drain that builds it here, not a hand-written line.
        ``children`` are promoted with ``enrich fetch`` before the rename,
        exactly as a session deepens an item — harvested depth-1 units
        whose hashes no frontmatter ever lists.
        ``move_enrichment=False`` is the interrupted rename: a rename is
        judgment work a session does in steps, and one interrupted between
        the corpus file and the enrichment directory leaves exactly that.
        """
        write_item(instance, OLD_ITEM)
        transport = FakeTransport(
            {url: HttpResponse(status=200, content_type="image/png", body=b"png") for url in media}
        )

        def fetch(_unit):
            return Content(
                meta={"title": "t"}, body="b" * 400, media=list(media), assets=list(assets)
            )

        run_mod.run(make_ctx(instance, FakeDriver(fetch_fn=fetch), transport=transport))
        if children:
            run_mod.fetch_urls(
                make_ctx(instance, FakeDriver(fetch_fn=fetch), transport=transport),
                OLD_ITEM,
                list(children),
            )
        old = instance.corpus_dir / "2026" / f"{OLD_ITEM}.md"
        item = corpus.read_item(old)
        corpus.write_item(
            instance.corpus_dir / "2026" / f"{NEW_ITEM}.md", dataclasses.replace(item, id=NEW_ITEM)
        )
        old.unlink()
        if move_enrichment:
            (instance.enrichment_dir / OLD_ITEM).rename(instance.enrichment_dir / NEW_ITEM)
        return transport

    def test_a_rename_carried_through_leaves_nothing_to_repair(self, instance):
        # Corpus file and enrichment directory both moved. Every line still
        # names the old id and every one of them is history: the outputs are
        # under the live item, its listing and status derive from them, and
        # neither output check has anything to say. Only the ghost row
        # shows, saying what is true — the id on the line was renamed away.
        self._drained_then_renamed(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.item == OLD_ITEM
        assert entry.path == f"enrichment/{OLD_ITEM}/web-{entry.hash[:6]}.md"
        item = corpus.read_item(instance.corpus_dir / "2026" / f"{NEW_ITEM}.md")
        assert item.status == "enriched"
        assert item.enrichment == [f"web-{entry.hash[:6]}.md"]
        report = self._lint(instance)
        assert f"**{OLD_ITEM}** — 1 entry (renamed — {NEW_ITEM} lists this work)" in report
        assert "done entries whose output file is gone from disk — none" in report
        assert "done entries whose output sits under another item's directory — none" in report

    def test_an_interrupted_rename_does_not_become_invisible(self, instance):
        # The corpus file moved and the enrichment directory did not. The
        # item owes nothing, so it derives `enriched` and is not stuck; but
        # its own directory is empty and its listing says so, and no run
        # surface says why. Moving files is not the drain's business; naming
        # the state is lint's, and the state must stay named — against the
        # live id, which is the one with an empty directory.
        self._drained_then_renamed(instance, move_enrichment=False)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.item == OLD_ITEM
        assert entry.status is Status.DONE
        assert entry.path == f"enrichment/{OLD_ITEM}/web-{entry.hash[:6]}.md"
        item = corpus.read_item(instance.corpus_dir / "2026" / f"{NEW_ITEM}.md")
        assert item.enrichment == []  # the file is not in its directory
        report = self._lint(instance)
        assert f"**{OLD_ITEM}** — 1 entry (renamed — {NEW_ITEM} lists this work)" in report
        assert "done entries whose output sits under another item's directory — **1**" in report
        assert f"**{NEW_ITEM}** → `enrichment/{OLD_ITEM}/web-{entry.hash[:6]}.md`" in report

    def _lint(self, instance) -> str:
        (instance.state_dir / "taxonomy.json").write_text('{"topics": {}, "entities": {}}')
        return run_lint(
            instance, is_cognitive=lambda _need, _fmt=None: False, today=lambda: TODAY, write=False
        ).report

    CHILD = "https://example.test/harvested-doc"
    HERO = "https://example.test/hero.png"

    def test_a_completed_rename_resolves_the_whole_family_to_the_live_id(self, instance):
        # The driven scenario, whole: an item with a harvested child
        # (enrich fetch, depth 1) and a media unit, renamed per the skills
        # — corpus file moved with its id field updated, enrichment
        # directory moved, ledger untouched. The children's hashes appear
        # in no frontmatter, so their only corpus answer travels through
        # the parent chain; read off `urls:` hashing alone they were
        # "unclaimed work" whose outputs were "gone from disk" — advice
        # that pointed at deleting live state, standing forever.
        self._drained_then_renamed(instance, media=[self.HERO], children=[self.CHILD])
        report = self._lint(instance)
        assert "ledger items with no corpus file — **1**" in report
        assert f"**{OLD_ITEM}** — 3 entries (renamed — {NEW_ITEM} lists this work)" in report
        assert "no exclusions.tsv record" not in report
        assert "done entries whose output file is gone from disk — none" in report
        assert "done entries whose output sits under another item's directory — none" in report

    def test_a_completed_renames_status_view_carries_the_whole_family(self, instance):
        self._drained_then_renamed(instance, media=[self.HERO], children=[self.CHILD])
        status = run_mod.status_report(make_ctx(instance, FakeDriver()), item_id=NEW_ITEM)
        for url in (URL, self.CHILD, self.HERO):
            assert url in status

    def test_the_digest_backstop_attributes_the_renamed_family_once(self, instance):
        self._drained_then_renamed(instance, media=[self.HERO], children=[self.CHILD])
        assert run_mod.digest_orphans(instance) == [NEW_ITEM]  # once, under the live id

    def test_a_pre_rename_digest_still_covers_the_live_item(self, instance):
        # The skills' rename moves the corpus file and the enrichment
        # directory; state/digests/<old-id>.md stays where it was written.
        # Resolved by shortid it still covers the item — asking for the
        # live id's filename re-listed the item as owing a digest already
        # recorded, and the re-digest that answered it left a ninth digest
        # beside eight items, named on no surface, forever.
        self._drained_then_renamed(instance)
        instance.digests_dir.mkdir(parents=True, exist_ok=True)
        (instance.digests_dir / f"{OLD_ITEM}.md").write_text("digested\n", encoding="utf-8")
        run_mod.record_pass(make_ctx(instance, FakeDriver()), OLD_ITEM, "digest")
        assert run_mod.digest_orphans(instance) == []

    def test_a_pre_rename_digest_gone_stale_is_still_stale(self, instance):
        # The other direction: enrichment lands AFTER the rename, so the
        # old digest is genuinely stale. The pass record names the old id,
        # and unresolved it dated nothing — the staleness reading went
        # silent exactly when it had something to say.
        self._drained_then_renamed(instance)
        instance.digests_dir.mkdir(parents=True, exist_ok=True)
        (instance.digests_dir / f"{OLD_ITEM}.md").write_text("digested\n", encoding="utf-8")
        run_mod.record_pass(make_ctx(instance, FakeDriver()), OLD_ITEM, "digest")
        later = datetime.date(2026, 8, 22)
        run_mod.fetch_urls(make_ctx(instance, FakeDriver(), today=lambda: later), NEW_ITEM, [URL])
        assert run_mod.digest_orphans(instance) == [NEW_ITEM]

    def test_write_time_marks_land_under_the_live_owner(self, instance):
        # `mark` is a write like any other: healed on the stored string it
        # re-records a renamed item's unit under the dead id, and the
        # superseded-output drop searches a directory the rename moved.
        self._drained_then_renamed(instance, children=[self.CHILD])
        run_mod.mark(
            make_ctx(instance, FakeDriver()),
            self.CHILD,
            Status.MANUAL,
            reason="superseded by the live copy",
        )
        entry = ledger.load(instance.ledger_path)[work_hash(self.CHILD)]
        assert entry.item == NEW_ITEM
        assert entry.status is Status.MANUAL

    def test_an_extraction_asset_that_moved_with_the_item_is_never_re_fetched(self, instance):
        # A `job: asset` unit's work key is a repo path, not a URL,
        # so anything that queues one for a fetch can only ever raise: the
        # unit sat in `error`, holding the item `raw` out of digest and wiki
        # forever, with the asset on disk under the new name the whole time.
        # Nothing re-derives an output name now, so nothing judges it lost.
        self._drained_then_renamed(instance, assets=[Asset(data=b"png-bytes", suggested_ext="png")])
        driver = FakeDriver()
        run_mod.run(make_ctx(instance, driver))
        asset = next(e for e in ledger.load(instance.ledger_path).values() if e.job is Job.ASSET)
        assert asset.status is Status.DONE
        assert (
            instance.enrichment_dir / NEW_ITEM / f"{work_hash(URL)[:6]}-asset-0.png"
        ).read_bytes() == b"png-bytes"
        assert driver.fetched == []  # nothing was requeued, so nothing re-fetched
        item = corpus.read_item(instance.corpus_dir / "2026" / f"{NEW_ITEM}.md")
        assert item.status == "enriched"

    def test_a_media_download_that_moved_with_the_item_is_not_re_downloaded(self, instance):
        # The milder half of the same case: a file that moved with the item
        # was re-fetched and reported as new material.
        hero = "https://example.test/hero.png"
        transport = self._drained_then_renamed(instance, media=[hero])
        transport.calls.clear()
        run_mod.run(make_ctx(instance, FakeDriver(), transport=transport))
        entry = ledger.load(instance.ledger_path)[work_hash(hero)]
        assert entry.status is Status.DONE
        assert (instance.enrichment_dir / NEW_ITEM / "media-0.png").is_file()
        assert transport.calls == []

    def test_a_renamed_items_error_line_keeps_its_retry_on_a_newer_engine(self, instance):
        # `is_drainable` retries an `error` once per newer engine. Nothing
        # re-stamps the line before the drain builds its queue, so the run
        # that meets the rename is the run that rescues the unit — sharpest
        # right after a sync, where migration 1 stamps old lines `0.0.1`.
        self._renamed(instance, status=Status.ERROR, error="ValueError: boom")
        driver = FakeDriver()
        ctx = make_ctx(instance, driver, engine_version="0.3.0")
        run_mod.run(ctx)
        assert [unit.url for unit in driver.fetched] == [URL]  # the rescue run rescued it
        entry = entry_for(ctx)
        assert entry.item == NEW_ITEM
        assert entry.status is Status.DONE

    def test_a_unit_no_live_item_claims_is_written_under_the_id_it_carries(self, instance):
        # Ownership resolves to a live corpus item or it does not resolve;
        # nobody gets clever rescuing a dead item's work, so the write path
        # falls back exactly as the read side does.
        self._renamed(instance, status=Status.QUEUED)
        (instance.corpus_dir / "2026" / f"{NEW_ITEM}.md").unlink()
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.item == OLD_ITEM
        assert entry.path == f"enrichment/{OLD_ITEM}/web-{entry.hash[:6]}.md"

    def _shared_then_excluded(self, instance) -> RunContext:
        """Two items on one URL; the one the ledger line names is purged."""
        write_item(instance, ALPHA)
        write_item(instance, BRAVO)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)  # alpha wins the dedupe, so the line names alpha
        run_exclude(
            instance,
            [{"id": ALPHA, "reason": "out of scope"}],
            today=lambda: TODAY,
            now=lambda: NOW,
            version=lambda: "0.2.0",
        )
        return ctx

    def test_the_survivors_url_is_fetched_after_its_twin_is_excluded(self, instance):
        # `exclude` rightly keeps the line — bravo still claims the work —
        # but deletes the enrichment with alpha, leaving a `done` hash whose
        # product is gone and which seeding's short-circuit never revisits.
        ctx = self._shared_then_excluded(instance)
        assert not (instance.enrichment_dir / BRAVO).exists()
        run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.item == BRAVO
        assert entry.status is Status.DONE
        assert entry.path == f"enrichment/{BRAVO}/web-{entry.hash[:6]}.md"
        assert (instance.root / entry.path).is_file()

    def test_the_survivor_is_not_reported_as_a_no_source_item(self, instance):
        # bravo lists a URL; "no-source item — awaiting description" says
        # the opposite of what its own frontmatter says.
        ctx = self._shared_then_excluded(instance)
        assert "no-source item" not in run_mod.run(ctx)

    def test_a_live_twin_sharing_the_url_is_not_a_no_source_item(self, instance):
        # The same rule with no exclusion anywhere — the case the survivor
        # test cannot pin, because there `exclude` re-queues the stranded
        # landing and the run rewrites it under bravo. Here alpha is alive
        # and keeps the line, so "has this item any units" can only be
        # answered by the corpus.
        write_item(instance, ALPHA)
        write_item(instance, BRAVO)
        report = run_mod.run(make_ctx(instance, FakeDriver()))
        assert "no-source item" not in report

    def test_the_shape_of_what_is_missing_counts_a_shared_unit_too(self, instance):
        # bravo's two units are one line naming bravo and one naming alpha,
        # and the report's shape has to be read off the same map its `raw`
        # is — "0 of 1 landed" for an item that owes two says the ledger
        # holds less of bravo's work than it does.
        second = "https://example.test/second"
        write_item(instance, ALPHA)
        write_item(instance, BRAVO, urls=[URL, second])

        def fetch(unit):
            if unit.url == second:
                return Content(meta={"title": "t"}, body="body " * 30)
            return NeedsCapability(need=Need.TRANSCRIBE, reason="no captions")

        report = run_mod.run(make_ctx(instance, FakeDriver(fetch_fn=fetch)))
        flat = " ".join(report.split())
        assert f"**{BRAVO}** ↳ 1 of 2 units landed — 1 waiting on transcription" in flat

    def test_the_staleness_backstop_reads_a_renamed_items_landing_date(self, instance):
        # `_last_enriched` keyed on the stored string files the landing date
        # under a dead id, so the renamed item has no date to compare and
        # the interrupted-session backstop — the one surface that catches
        # "enriched, never digested" — quietly stops naming it.
        self._renamed(
            instance,
            status=Status.DONE,
            path=f"enrichment/{NEW_ITEM}/web-{work_hash(URL)[:6]}.md",
        )
        instance.digests_dir.mkdir(parents=True, exist_ok=True)
        (instance.digests_dir / f"{NEW_ITEM}.md").write_text("digested\n", encoding="utf-8")
        instance.passes_path.write_text(
            json.dumps({"stage": "digest", "item": NEW_ITEM, "date": "2026-07-01"}) + "\n",
            encoding="utf-8",
        )
        assert run_mod.digest_orphans(instance) == [NEW_ITEM]

    def test_mark_refreshes_the_item_the_corpus_says_owns_the_unit(self, instance):
        # `mark` copies the prior line's item, so a heal on a renamed item's
        # unit refreshed an id with no corpus file — a silent no-op — and
        # left the live item reading `raw` with its enrichment on disk.
        path = self._waiting(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.mark(
            ctx, URL, Status.DONE, path=f"enrichment/{NEW_ITEM}/web-{work_hash(URL)[:6]}.md"
        )
        item = corpus.read_item(path)
        assert item.status == "enriched"
        assert item.enrichment == [f"web-{work_hash(URL)[:6]}.md"]

    def test_a_done_unit_whose_output_stands_is_never_re_fetched(self, instance):
        # The dedupe is the design, not a fault: alpha holds the one file
        # for the shared URL and bravo has none, on 80 hashes in one real
        # instance. Only a landing with nothing on disk is re-fetched.
        write_item(instance, ALPHA)
        write_item(instance, BRAVO)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        run_mod.run(ctx)
        assert not (instance.enrichment_dir / BRAVO).exists()
        assert entry_for(ctx).item == ALPHA

    def test_a_twin_is_enriched_by_the_landing_under_its_co_claimant(self, instance):
        # Seeding dedupes by work hash, so bravo's only URL is ledgered and
        # enriched under alpha. Reading bravo's own empty directory as "not
        # enriched" left it `raw` permanently, on no surface, with nothing
        # that could move it: `fetch` refuses a URL another item enriches,
        # `mark` heals the unit — which was never the thing that was wrong —
        # and a digest pass cannot lift a status it does not derive.
        write_item(instance, ALPHA)
        bravo = write_item(instance, BRAVO)
        run_mod.run(make_ctx(instance, FakeDriver()))
        item = corpus.read_item(bravo)
        assert item.status == "enriched"
        # And its listing stays empty, deliberately: the field is the item's
        # OWN directory, its entries are bare filenames every reader
        # resolves against `enrichment/<id>/`, and one file for one unit is
        # named by one item. `enrich status <item>` is where the shared unit
        # shows, and it does.
        assert item.enrichment == []
        assert URL in run_mod.status_report(make_ctx(instance, FakeDriver()), item_id=BRAVO)

    def test_a_twin_reaches_the_digest_backstop_with_no_directory_of_its_own(self, instance):
        # The interrupted-session case the backstop exists for: the run
        # landed the shared URL and the session stopped before digesting
        # bravo. bravo owes nothing, so it is digestible — and it has no
        # enrichment directory for a disk walk to find it by.
        write_item(instance, ALPHA)
        write_item(instance, BRAVO)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        assert not (instance.enrichment_dir / BRAVO).exists()
        assert run_mod.digest_orphans(instance) == [ALPHA, BRAVO]
        assert BRAVO in run_mod.status_report(ctx)

    def test_a_digested_twin_leaves_the_backstop(self, instance):
        # The backstop names an interrupted session, not a standing state:
        # once the digest pass is recorded, bravo stops being listed.
        write_item(instance, ALPHA)
        write_item(instance, BRAVO)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        instance.digests_dir.mkdir(parents=True, exist_ok=True)
        (instance.digests_dir / f"{BRAVO}.md").write_text("digested\n", encoding="utf-8")
        run_mod.record_pass(ctx, BRAVO, "digest")
        assert run_mod.digest_orphans(instance) == [ALPHA]

    def test_a_dead_id_holding_a_landing_is_not_offered_as_digestible(self, instance):
        # The candidate set gained "the ledger records a landing for it", so
        # it has to keep the live-item bound the disk walk gave it for free:
        # a done line naming an id no corpus file answers to is lint's
        # ghost-item finding, not work anyone can be asked to do.
        write_item(instance, ALPHA)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        (instance.corpus_dir / "2026" / f"{ALPHA}.md").unlink()
        shutil.rmtree(instance.enrichment_dir / ALPHA)  # what `exclude` leaves behind
        assert run_mod.digest_orphans(instance) == []

    def test_an_outstanding_shared_unit_holds_every_item_listing_it_raw(self, instance):
        # The line names alpha, but bravo lists the URL too, so bravo owes
        # the work — and an item owing work is out of digest and wiki.
        second = "https://example.test/second"
        write_item(instance, ALPHA)
        bravo_path = write_item(instance, BRAVO, urls=[URL, second])

        def fetch(unit):
            if unit.url == second:
                return Content(meta={"title": "t"}, body="body " * 30)
            return NeedsCapability(need=Need.TRANSCRIBE, reason="no captions")

        run_mod.run(make_ctx(instance, FakeDriver(fetch_fn=fetch)))
        assert corpus.read_item(bravo_path).status == "raw"


class TestRenameAwareDigestBackstop:
    """An orphaned enrichment directory resolves to the live item by shortid."""

    def _mid_rename(self, instance) -> None:
        """File + id renamed; the enrichment directory left under the old name."""
        write_item(instance, NEW_ITEM)
        out = instance.enrichment_dir / OLD_ITEM / f"web-{work_hash(URL)[:6]}.md"
        out.parent.mkdir(parents=True)
        out.write_text(f"---\nurl: {URL}\nfetched: '2026-08-01'\n---\n\nthe body\n")
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(URL),
                url=URL,
                item=OLD_ITEM,
                kind=Kind.WEB,
                status=Status.DONE,
                engine="0.2.0",
                date=datetime.date(2026, 8, 1),
                path=f"enrichment/{OLD_ITEM}/web-{work_hash(URL)[:6]}.md",
            ),
        )

    def test_only_the_live_id_is_listed(self, instance):
        # Not the dead directory name the digest verb would refuse, and
        # not both — once, under the live id.
        self._mid_rename(instance)
        assert run_mod.digest_orphans(instance) == [NEW_ITEM]

    def test_digesting_the_listed_id_succeeds_and_clears_the_listing(self, instance):
        self._mid_rename(instance)
        ctx = make_ctx(instance, FakeDriver())
        payload = instance.cache_dir / "digest.json"
        payload.write_text(
            json.dumps(
                {
                    "id": NEW_ITEM,
                    "signal": "medium",
                    "topics": ["uncategorized-shares"],
                    "facts": ["one fact"],
                }
            ),
            encoding="utf-8",
        )
        confirmation = item_digest(payload, ctx=ctx)
        assert "digest pass recorded" in confirmation
        assert run_mod.digest_orphans(instance) == []

    def test_a_directory_matching_no_live_shortid_keeps_its_name(self, instance):
        # Nothing to attribute to — the name stands, as it always did.
        stray = instance.enrichment_dir / "2026-01-01-gone-abcdef"
        stray.mkdir(parents=True)
        (stray / "web-abc123.md").write_text("enriched", encoding="utf-8")
        assert run_mod.digest_orphans(instance) == ["2026-01-01-gone-abcdef"]


class TestRerunPacing:
    """Reruns pace themselves: fresh work first, then at most 50 per run."""

    def _seed_reruns(self, ctx, count, start=0):
        for i in range(start, start + count):
            url = f"https://example.test/rerun-{i}"
            ledger.append(
                ctx.instance.ledger_path,
                LedgerEntry(
                    hash=work_hash(url),
                    url=url,
                    item=ITEM,
                    kind=Kind.WEB,
                    status=Status.QUEUED,
                    engine="0.0.1",
                    date=TODAY,
                    via="migration-2",
                    rerun=True,
                ),
            )

    def test_fresh_work_drains_before_reruns(self, instance):
        driver = FakeDriver()
        ctx = make_ctx(instance, driver)
        write_item(instance)  # one fresh URL
        self._seed_reruns(ctx, 1)
        run_mod.run(ctx, limit=1)  # room for exactly one unit
        assert [unit.url for unit in driver.fetched] == [URL]  # the fresh one
        rerun_entry = entry_for(ctx, "https://example.test/rerun-0")
        assert rerun_entry.status is Status.QUEUED  # untouched, queues on

    def test_rerun_cap_holds_and_remainder_queues_across_runs(self, instance):
        driver = FakeDriver()
        ctx = make_ctx(instance, driver)
        self._seed_reruns(ctx, run_mod.RERUN_DRAIN_CAP + 5)
        report = run_mod.run(ctx)
        done = [
            e for e in ledger.load(ctx.instance.ledger_path).values() if e.status is Status.DONE
        ]
        queued = [
            e for e in ledger.load(ctx.instance.ledger_path).values() if e.status is Status.QUEUED
        ]
        assert len(done) == run_mod.RERUN_DRAIN_CAP
        assert len(queued) == 5
        assert (
            f"rerun cohort: {run_mod.RERUN_DRAIN_CAP} of "
            f"{run_mod.RERUN_DRAIN_CAP + 5} drained; 5 queue for the next run"
        ) in report
        # The next run drains the remainder and says so.
        second = run_mod.run(make_ctx(instance, FakeDriver()))
        remaining = [
            e for e in ledger.load(ctx.instance.ledger_path).values() if e.status is Status.QUEUED
        ]
        assert remaining == []
        assert "rerun cohort: 5 of 5 drained" in second
        assert "queue for the next run" not in second

    def test_limit_binds_the_total_cap_binds_the_slice(self, instance):
        ctx = make_ctx(instance, FakeDriver())
        self._seed_reruns(ctx, 10)
        report = run_mod.run(ctx, limit=3)
        assert "rerun cohort: 3 of 10 drained; 7 queue for the next run" in report

    def test_no_reruns_no_cohort_note(self, instance):
        write_item(instance)
        report = run_mod.run(make_ctx(instance, FakeDriver()))
        assert "rerun cohort" not in report


class TestRedetection:
    """Mid-fetch kind discovery: same URL, same hash, corrected identity."""

    PDF_URL = "https://example.test/whitepaper"
    HTML = b"<!DOCTYPE html>\n<html><body>a page pretending to be a paper</body></html>"

    def _drivers(self, instance, transport):
        capabilities = Capabilities(
            transcribers=(), extractors=(FakeExtractor(markdown="extracted pdf text " * 30),)
        )
        return [
            FileDriver(capabilities=capabilities, root=instance.root, transport=transport),
            WebDriver(transport=transport),  # the catch-all stays last
        ]

    def _lineage(self, ctx, url):
        lines = [
            json.loads(line)
            for line in ctx.instance.ledger_path.read_text().split("\n")
            if line.strip()
        ]
        return [
            (line["kind"], line["status"], line.get("via"))
            for line in lines
            if line["hash"] == work_hash(url)
        ]

    def test_lying_head_reroutes_web_to_file_in_run(self, instance):
        # The server reports text/html on HEAD and GET alike; the body is a
        # real PDF. Detection trusts the HEAD (web); the web driver's magic
        # sniff catches the lie mid-fetch.
        transport = FakeTransport(
            {
                self.PDF_URL: HttpResponse(
                    status=200, content_type="text/html", body=fixture_bytes("paper.pdf")
                )
            }
        )
        write_item(instance, urls=[self.PDF_URL])
        ctx = make_ctx(instance, FakeDriver(), drivers=self._drivers(instance, transport))
        report = run_mod.run(ctx)
        entry = entry_for(ctx, self.PDF_URL)
        assert entry.status is Status.DONE
        assert entry.kind is Kind.FILE
        assert entry.path is not None
        assert (instance.root / entry.path).exists()  # extraction output landed
        assert entry.path.endswith(f"file-{entry.hash[:6]}.md")
        assert self._lineage(ctx, self.PDF_URL) == [
            ("web", "queued", None),  # seeded as detection said
            ("file", "queued", "sniff"),  # corrected mid-fetch
            ("file", "done", "sniff"),  # drained through the file driver, same run
        ]
        assert f"re-detected: {self.PDF_URL} — web → file/pdf" in report

    EPISODE_URL = "https://engineering-distilled.test/episodes/ledgers-as-work-queues"
    EPISODE_FEED = "https://feeds.pods.test/engineering-distilled.rss"

    def test_an_indie_episode_page_reroutes_web_to_podcast_in_run(self, instance):
        # The podcast detection's third route, end to end: no URL pattern claims this page, so
        # the catch-all fetches it, sees the audio it carries, and hands the
        # unit to the podcast driver — which resolves the feed's enclosure.
        transport = FakeTransport(
            {
                self.EPISODE_URL: HttpResponse(
                    status=200,
                    content_type="text/html",
                    body=fixture_text("podcast", "indie-episode-page.html").encode("utf-8"),
                ),
                self.EPISODE_FEED: HttpResponse(
                    status=200,
                    content_type="application/rss+xml",
                    body=fixture_text("podcast", "feed.xml").encode("utf-8"),
                ),
            }
        )
        write_item(instance, urls=[self.EPISODE_URL])
        drivers = [PodcastDriver(transport=transport), WebDriver(transport=transport)]
        ctx = make_ctx(instance, FakeDriver(), drivers=drivers)
        report = run_mod.run(ctx)
        entry = entry_for(ctx, self.EPISODE_URL)
        assert entry.kind is Kind.PODCAST
        assert entry.status is Status.WAITING
        assert entry.needs is Need.TRANSCRIBE
        assert self._lineage(ctx, self.EPISODE_URL) == [
            ("web", "queued", None),  # nothing but the catch-all claimed it
            ("podcast", "queued", "sniff"),  # corrected on the page's own audio
            ("podcast", "waiting", "sniff"),  # resolved, parked for the drain
        ]
        park = instance.enrichment_dir / ITEM / f"podcast-{entry.hash[:6]}.md"
        content = park.read_text()
        assert '"https://cdn.pods.test/ed/ep42.mp3?sig=abc123"' in content  # the drain's pointer
        assert "Ada Guest" in content  # the feed's show notes, richer than the page
        assert f"re-detected: {self.EPISODE_URL} — web → podcast" in " ".join(report.split())

    BLOB_URL = "https://github.com/acme/pipeline-kit/blob/main/docs/whitepaper.pdf"
    BLOB_CONTENTS = ("api", "repos/acme/pipeline-kit/contents/docs/whitepaper.pdf?ref=main")

    def test_a_github_blob_pdf_reroutes_to_file_and_extracts(self, instance):
        # The blob URL serves an HTML viewer, so the file driver reads its
        # bytes through the same gh seam the github driver uses — which is
        # why the refusal that used to park this manual no longer holds.
        gh = FakeGhApi({self.BLOB_CONTENTS: gh_contents(fixture_bytes("paper.pdf"))})
        capabilities = Capabilities(
            transcribers=(), extractors=(FakeExtractor(markdown="extracted pdf text " * 30),)
        )
        drivers = [
            GitHubDriver(gh=gh),
            FileDriver(capabilities=capabilities, root=instance.root, gh=gh),
            WebDriver(transport=FakeTransport({})),  # the catch-all stays last
        ]
        write_item(instance, urls=[self.BLOB_URL])
        ctx = make_ctx(instance, FakeDriver(), drivers=drivers)
        report = run_mod.run(ctx)
        entry = entry_for(ctx, self.BLOB_URL)
        assert entry.status is Status.DONE
        assert entry.kind is Kind.FILE
        assert entry.format is Format.PDF
        assert entry.path is not None
        assert (instance.root / entry.path).exists()
        assert self._lineage(ctx, self.BLOB_URL) == [
            ("github", "queued", None),  # seeded as detection said
            ("file", "queued", "sniff"),  # the blob's bytes are a document
            ("file", "done", "sniff"),  # extracted in the same run
        ]
        assert f"re-detected: {self.BLOB_URL} — github → file/pdf" in " ".join(report.split())

    def test_blocked_head_still_corrects_on_the_get(self, instance):
        pdf = HttpResponse(
            status=200, content_type="application/octet-stream", body=fixture_bytes("paper.pdf")
        )
        blocked = HttpResponse(status=403, content_type="text/html", body=b"")

        class MethodAware:
            def __call__(self, url, *, method="GET", limit=None):  # noqa: ARG002 — routes on method alone
                return blocked if method == "HEAD" else pdf

        write_item(instance, urls=[self.PDF_URL])
        transport = MethodAware()
        ctx = make_ctx(instance, FakeDriver(), drivers=self._drivers(instance, transport))
        run_mod.run(ctx)
        entry = entry_for(ctx, self.PDF_URL)
        assert entry.status is Status.DONE
        assert entry.kind is Kind.FILE

    def test_mutual_redetection_parks_manual_never_loops(self, instance):
        # content-type says PDF, the body is HTML: web re-routes to file on
        # the content type, file re-routes back on the bytes — the second
        # correction hits the once-only guard.
        transport = FakeTransport(
            {self.PDF_URL: HttpResponse(status=200, content_type="application/pdf", body=self.HTML)}
        )
        write_item(instance, urls=[self.PDF_URL])
        ctx = make_ctx(instance, FakeDriver(), drivers=self._drivers(instance, transport))
        report = run_mod.run(ctx)
        entry = entry_for(ctx, self.PDF_URL)
        assert entry.status is Status.MANUAL
        assert "re-detection loop" in (entry.reason or "")
        assert self._lineage(ctx, self.PDF_URL) == [
            ("web", "queued", None),
            ("file", "queued", "sniff"),
            ("file", "manual", "sniff"),
        ]
        assert "re-detected" in report  # the first correction is still noted

    def test_thin_html_never_false_redetects_end_to_end(self, instance):
        transport = FakeTransport(
            {URL: HttpResponse(status=200, content_type="text/html", body=self.HTML)}
        )
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver(), drivers=self._drivers(instance, transport))
        report = run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.status is Status.MANUAL
        assert entry.reason == "thin-extraction"
        assert entry.kind is Kind.WEB
        assert "re-detected" not in report

    def _requeue(self, ctx, url, **overrides):
        entry = entry_for(ctx, url)
        requeued = dataclasses.replace(
            entry, status=Status.QUEUED, path=None, title=None, rerun=True, **overrides
        )
        ledger.append(ctx.instance.ledger_path, requeued)

    def test_cross_run_redetect_unlinks_the_stale_old_kind_output(self, instance):
        # Run 1: an ordinary web page, extracted and listed. The world then
        # changes — the URL serves a PDF — and a reseeded rerun corrects the
        # kind. The superseded web output must leave the disk AND the item's
        # enrichment: listing; the ledger audit trail is the history.
        article = fixture_text("web", "article.html")
        responses = {
            self.PDF_URL: HttpResponse(status=200, content_type="text/html", body=article.encode())
        }
        transport = FakeTransport(responses)
        item_path = write_item(instance, urls=[self.PDF_URL])
        quiet = Config(media_fetch=MediaFetch.NONE)  # the fixture's og:image is not under test
        ctx = make_ctx(
            instance, FakeDriver(), drivers=self._drivers(instance, transport), config=quiet
        )
        run_mod.run(ctx)
        first = entry_for(ctx, self.PDF_URL)
        assert first.kind is Kind.WEB
        assert first.status is Status.DONE
        web_out = instance.root / str(first.path)
        assert web_out.exists()
        assert corpus.read_item(item_path).enrichment == [web_out.name]

        responses[self.PDF_URL] = HttpResponse(
            status=200, content_type="text/html", body=fixture_bytes("paper.pdf")
        )
        self._requeue(ctx, self.PDF_URL, via="migration-2")
        run_mod.run(
            make_ctx(
                instance, FakeDriver(), drivers=self._drivers(instance, transport), config=quiet
            )
        )
        entry = entry_for(ctx, self.PDF_URL)
        assert entry.kind is Kind.FILE
        assert entry.status is Status.DONE
        assert not web_out.exists()  # the correction superseded it
        assert corpus.read_item(item_path).enrichment == [f"file-{entry.hash[:6]}.md"]

    def test_cross_run_re_correction_succeeds_after_a_requeue(self, instance):
        # The guard is per-run state, not ledger history: a unit corrected
        # to file months ago (via sniff on its lines) that now serves HTML
        # corrects BACK to web on a later requeue — never a false
        # "re-detection loop".
        responses = {
            self.PDF_URL: HttpResponse(
                status=200, content_type="text/html", body=fixture_bytes("paper.pdf")
            )
        }
        transport = FakeTransport(responses)
        write_item(instance, urls=[self.PDF_URL])
        ctx = make_ctx(instance, FakeDriver(), drivers=self._drivers(instance, transport))
        run_mod.run(ctx)
        corrected = entry_for(ctx, self.PDF_URL)
        assert (corrected.kind, corrected.via) == (Kind.FILE, "sniff")

        article = fixture_text("web", "article.html")
        responses[self.PDF_URL] = HttpResponse(
            status=200, content_type="text/html", body=article.encode()
        )
        self._requeue(ctx, self.PDF_URL)  # keeps via: sniff — provenance, not a lock
        report = run_mod.run(
            make_ctx(
                instance,
                FakeDriver(),
                drivers=self._drivers(instance, transport),
                config=Config(media_fetch=MediaFetch.NONE),
            )
        )
        entry = entry_for(ctx, self.PDF_URL)
        assert entry.kind is Kind.WEB
        assert entry.status is Status.DONE
        assert "re-detection loop" not in report
        assert not (instance.enrichment_dir / ITEM / f"file-{entry.hash[:6]}.md").exists()

    def test_a_parked_correction_keeps_the_old_output_until_one_replaces_it(self, instance):
        # The wild rerun path: an item enriched as web re-detects to file,
        # and the corrected fetch parks. Unlinking the web output at
        # redetect time left the item at `raw` with an orphaned digest and
        # nothing on disk to re-derive from.
        item_path = write_item(instance)
        mode = {"redetect": False, "extract": False}

        def web_fetch(_unit):
            if mode["redetect"]:
                return Redetected(kind=Kind.FILE, format=Format.PDF)
            return Content(meta={"title": "t"}, body="b" * 400)

        def file_fetch(_unit):
            if mode["extract"]:
                return Content(meta={"title": "t"}, body="extracted " * 40)
            return Unusable(evidence="no extractor for pdf here")

        web = FakeDriver(kind=Kind.WEB, fetch_fn=web_fetch)
        files = FakeDriver(kind=Kind.FILE, fetch_fn=file_fetch)
        ctx = make_ctx(instance, web, drivers=[web, files])
        run_mod.run(ctx)
        web_out = instance.enrichment_dir / ITEM / f"web-{work_hash(URL)[:6]}.md"
        assert web_out.exists()

        mode["redetect"] = True
        self._requeue(ctx, URL)
        run_mod.run(make_ctx(instance, web, drivers=[web, files]))
        parked = entry_for(ctx)
        assert parked.kind is Kind.FILE
        assert parked.status is Status.MANUAL
        assert web_out.exists()  # the enrichment the item already had stands
        assert corpus.read_item(item_path).enrichment == [web_out.name]
        assert corpus.read_item(item_path).status == "raw"  # the parked unit is still owed

        # The success that replaces it is what drops it.
        mode["extract"] = True
        self._requeue(ctx, URL, reason=None)
        run_mod.run(make_ctx(instance, web, drivers=[web, files]))
        done = entry_for(ctx)
        assert done.status is Status.DONE
        assert not web_out.exists()
        assert corpus.read_item(item_path).enrichment == [f"file-{done.hash[:6]}.md"]
        assert corpus.read_item(item_path).status == "enriched"

    # These two URLs share a sha1[:6] (6f0d01) and detect as different
    # kinds, so under one item they want output names that differ only by
    # the kind prefix — the shape a name-pattern drop cannot tell apart.
    COLLIDING_WEB = "https://example.test/post-26969"
    COLLIDING_PDF = "https://example.test/doc-2.pdf"

    def test_a_hash6_collision_never_drops_the_neighbours_output(self, instance):
        assert work_hash(self.COLLIDING_WEB)[:6] == work_hash(self.COLLIDING_PDF)[:6]
        article = fixture_text("web", "article.html")
        transport = FakeTransport(
            {
                self.COLLIDING_WEB: HttpResponse(
                    status=200, content_type="text/html", body=article.encode()
                ),
                self.COLLIDING_PDF: HttpResponse(
                    status=200, content_type="application/pdf", body=fixture_bytes("paper.pdf")
                ),
            }
        )
        item_path = write_item(instance, urls=[self.COLLIDING_WEB, self.COLLIDING_PDF])
        quiet = Config(media_fetch=MediaFetch.NONE)
        ctx = make_ctx(
            instance, FakeDriver(), drivers=self._drivers(instance, transport), config=quiet
        )
        run_mod.run(ctx)
        entries = [entry_for(ctx, self.COLLIDING_WEB), entry_for(ctx, self.COLLIDING_PDF)]
        assert [e.status for e in entries] == [Status.DONE, Status.DONE]
        # A done line pointing at nothing is the failure this pins.
        for entry in entries:
            assert entry.path is not None
            assert (instance.root / entry.path).is_file()
        shared = work_hash(self.COLLIDING_WEB)[:6]
        assert corpus.read_item(item_path).enrichment == [
            f"file-{shared}.md",
            f"web-{shared}.md",
        ]

    def test_a_mark_landed_output_drops_the_superseded_one(self, instance):
        # The prescribed recovery for a web→file correction whose corrected
        # fetch parks: the session writes the enrichment by hand and closes
        # it with `enrich mark ... done --path`. The stale web view of the
        # PDF must leave with it — the drain's route is not the only one.
        item_path = write_item(instance)
        mode = {"redetect": False}

        def web_fetch(_unit):
            if mode["redetect"]:
                return Redetected(kind=Kind.FILE, format=Format.PDF)
            return Content(meta={"title": "t"}, body="b" * 400)

        def file_fetch(_unit):
            return Unusable(evidence="scanned pdf — no extractor")

        web = FakeDriver(kind=Kind.WEB, fetch_fn=web_fetch)
        files = FakeDriver(kind=Kind.FILE, fetch_fn=file_fetch)
        ctx = make_ctx(instance, web, drivers=[web, files])
        run_mod.run(ctx)
        web_out = instance.enrichment_dir / ITEM / f"web-{work_hash(URL)[:6]}.md"
        assert web_out.exists()

        mode["redetect"] = True
        self._requeue(ctx, URL)
        run_mod.run(make_ctx(instance, web, drivers=[web, files]))
        assert entry_for(ctx).status is Status.MANUAL
        assert web_out.exists()  # the park leaves the item as enriched as it found it

        # The session reads the PDF with its eyes and closes the unit.
        hand_written = instance.enrichment_dir / ITEM / f"file-{work_hash(URL)[:6]}.md"
        hand_written.write_text(
            render_enrichment(URL, TODAY, {"title": "t"}, "read by hand " * 40),
            encoding="utf-8",
        )
        run_mod.mark(ctx, URL, Status.DONE, path=f"enrichment/{ITEM}/{hand_written.name}")
        assert not web_out.exists()
        assert corpus.read_item(item_path).enrichment == [hand_written.name]

    def test_redetected_rerun_spends_one_cohort_slot(self, instance):
        url = self.PDF_URL
        transport = FakeTransport(
            {
                url: HttpResponse(
                    status=200, content_type="text/html", body=fixture_bytes("paper.pdf")
                )
            }
        )
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(url),
                url=url,
                item=ITEM,
                kind=Kind.WEB,
                status=Status.QUEUED,
                engine="0.0.1",
                date=TODAY,
                via="migration-2",
                rerun=True,
            ),
        )
        ctx = make_ctx(instance, FakeDriver(), drivers=self._drivers(instance, transport))
        report = run_mod.run(ctx)
        assert entry_for(ctx, url).status is Status.DONE  # popped twice, completed
        assert "rerun cohort: 1 of 1 drained" in report
        assert "2 of" not in report

    def test_limit_defers_the_corrected_unit_to_the_next_run(self, instance):
        transport = FakeTransport(
            {
                self.PDF_URL: HttpResponse(
                    status=200, content_type="text/html", body=fixture_bytes("paper.pdf")
                )
            }
        )
        write_item(instance, urls=[self.PDF_URL])
        ctx = make_ctx(instance, FakeDriver(), drivers=self._drivers(instance, transport))
        report = run_mod.run(ctx, limit=1)  # the web fetch spends the whole budget
        deferred = entry_for(ctx, self.PDF_URL)
        assert (deferred.kind, deferred.status, deferred.via) == (
            Kind.FILE,
            Status.QUEUED,
            "sniff",
        )
        assert "re-detected" in report
        assert not (instance.enrichment_dir / ITEM / f"file-{deferred.hash[:6]}.md").exists()
        # The next run drains the corrected unit.
        run_mod.run(make_ctx(instance, FakeDriver(), drivers=self._drivers(instance, transport)))
        assert entry_for(ctx, self.PDF_URL).status is Status.DONE


class TestYamlValue:
    # Values a YAML 1.1 reader would retype (booleans, nulls, numbers in
    # every 1.1 spelling, dates/timestamps) or that break/restructure the
    # line (leading indicators, padding, interior tabs — a tab in a plain
    # scalar invalidates the whole frontmatter block): each must survive
    # the frontmatter round trip as the intended string.
    RETYPED = (
        "col1\tcol2",
        "- 10 lessons",
        "? maybe",
        ", and more",
        "yes",
        "No",
        "TRUE",
        "false",
        "on",
        "Off",
        "null",
        "~",
        "42",
        "-42",
        "+42",
        "3.14",
        ".5",
        "1e5",
        "1_000",
        "0x1A",
        "0o17",
        "0b101",
        "05",
        ".inf",
        "-.Inf",
        ".NaN",
        "20260810",
        "2026-08-21",
        "2026-08-99",  # shape-matching but calendar-invalid: 1.1 readers RAISE on it bare
        "2026-8-21T10:00:00",
        "2026-08-21 10:00:00",
        "2001-12-14 21:59:43.10 -5",
        " padded ",
    )

    def test_retyping_and_indicator_values_round_trip(self, tmp_path):
        for value in self.RETYPED:
            text = render_enrichment("https://example.test/post", TODAY, {"title": value}, "body")
            record = tmp_path / "web-abc123.md"
            record.write_text(text)
            fields, _ = read_enrichment(record)
            assert fields["title"] == value, value

    def test_retyping_shapes_are_emitted_quoted(self):
        for value in self.RETYPED:
            assert _yaml_value(value) == json.dumps(value, ensure_ascii=False), value

    def test_plain_prose_stays_unquoted(self):
        for value in ("Ledgers at Scale", "10 lessons from prod", "a-b (c)", "v1.2 notes"):
            assert _yaml_value(value) == value

    def test_timestamp_near_misses_stay_bare(self):
        # Colon-free shapes the 1.1 timestamp resolver rejects — these
        # load as strings already, so they need no quotes. (Anything with
        # a time part carries ':' and is quoted by the unsafe-char rule.)
        for value in ("2026-8-15", "2026-08", "21-08-2026"):
            assert _yaml_value(value) == value

    def test_ints_are_emitted_bare(self):
        assert _yaml_value(42) == "42"

    @pytest.mark.parametrize(
        "url",
        [
            "file:media/plan: v2.pdf",  # wild filenames are space-rich
            "file:media/2026-08-21/notes: draft.docx",
            "https://example.test/post",
        ],
    )
    def test_the_url_line_is_a_value_like_any_other(self, url, tmp_path):
        # A work key carrying a colon-space made the whole block unparseable
        # — and the url line is the one field that used to bypass quoting.
        text = render_enrichment(url, TODAY, {"title": "t"}, "body")
        record = tmp_path / "file-abc123.md"
        record.write_text(text)
        head = text[4:].partition("\n---\n")[0]
        assert yaml.safe_load(head)["url"] == url
        fields, _ = read_enrichment(record)
        assert fields["url"] == url  # and the engine's own reader round-trips it

    def test_tabbed_value_leaves_the_block_parseable_by_a_real_yaml_reader(self, tmp_path):
        value = "col1\tcol2"
        text = render_enrichment("https://example.test/post", TODAY, {"title": value}, "body")
        record = tmp_path / "web-abc123.md"
        record.write_text(text)
        fields, _ = read_enrichment(record)
        assert fields["title"] == value
        head = text[4:].partition("\n---\n")[0]
        assert yaml.safe_load(head)["title"] == value


class TestMediaSniff:
    def docx_bytes(self, padding: int = 20_000) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "x" * padding)
        return buf.getvalue()

    def test_sniff_bytes_is_bounded_for_non_zip_files(self, tmp_path):
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100_000)
        assert len(_sniff_bytes(clip)) == _SNIFF_PREFIX_BYTES

    def test_zip_containers_read_fully_for_their_package_identity(self, tmp_path):
        # A ZIP's package identity lives in the central directory at the
        # END of the file — a prefix would misdetect every large OOXML/ODF.
        data = self.docx_bytes()
        assert len(data) > _SNIFF_PREFIX_BYTES
        doc = tmp_path / "big.docx"
        doc.write_bytes(data)
        assert _sniff_bytes(doc) == data

    def test_small_and_missing_files_read_as_before(self, tmp_path):
        small = tmp_path / "pointer.pdf"
        small.write_bytes(b"%PDF-1.7 tiny")
        assert _sniff_bytes(small) == b"%PDF-1.7 tiny"
        assert _sniff_bytes(tmp_path / "absent.pdf") == b""

    def test_seeding_outcomes_identical_for_large_files(self, instance):
        media_dir = instance.root / "media" / "aaa111"
        media_dir.mkdir(parents=True)
        (media_dir / "paper.pdf").write_bytes(b"%PDF-1.7 " + b"x" * 50_000)
        (media_dir / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 50_000)
        (media_dir / "big.docx").write_bytes(self.docx_bytes())
        write_item(
            instance,
            urls=[],
            media=[
                "media/aaa111/paper.pdf",
                "media/aaa111/clip.mp4",
                "media/aaa111/big.docx",
            ],
        )
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        assert entries[work_hash("file:media/aaa111/paper.pdf")].format is Format.PDF
        assert entries[work_hash("file:media/aaa111/big.docx")].format is Format.DOCX
        # Images/video still never become file work — and no longer cost a
        # full read every run to say so.
        assert work_hash("file:media/aaa111/clip.mp4") not in entries
