"""Tests for pipeline/run.py: the orchestrator, with fake drivers."""

import dataclasses
import datetime
import io
import json
import os
import zipfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dex_engine import corpus
from dex_engine.capabilities import Capabilities
from dex_engine.drivers.file import FileDriver
from dex_engine.drivers.transport import HttpResponse
from dex_engine.drivers.web import WebDriver
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.classify import ProviderInputError
from dex_engine.pipeline.run import (
    _SNIFF_PREFIX_BYTES,
    MAX_BLOCKED_ATTEMPTS,
    MAX_DEPTH,
    MAX_URLS_PER_ITEM,
    MEDIA_MAX_FILES,
    RunContext,
    _render_enrichment,
    _sniff_bytes,
    _yaml_value,
    is_drainable,
    no_providers,
)
from dex_engine.pipeline.transcribe import read_enrichment
from dex_engine.pipeline.types import (
    Asset,
    Child,
    Config,
    Format,
    Instance,
    Kind,
    LedgerEntry,
    MediaFetch,
    Need,
    Result,
    Status,
)
from dex_engine.pipeline.urls import work_hash
from tests.capabilities.conftest import FakeExtractor, fixture_bytes
from tests.conftest import FakeDriver
from tests.drivers.conftest import FakeTransport, fixture_text
from tests.pipeline.test_issues import FakeGh

TODAY = datetime.date(2026, 8, 20)
ITEM = "2026-08-19-example-55ad7b"
URL = "https://example.test/post"


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
        assert report.startswith("enrich run — 1 unit processed")
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

    def test_enrichment_frontmatter_quotes_unsafe_values(self, instance):
        write_item(instance)
        fetch = lambda _unit: Result(  # noqa: E731
            status=Status.DONE, meta={"title": "Ledgers: a Field Report"}, body="b" * 400
        )
        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        content = (instance.root / entry_for(ctx).path).read_text()
        assert 'title: "Ledgers: a Field Report"' in content
        assert content.startswith(f"---\nurl: {URL}\nfetched: 2026-08-20\n")


class TestFrontmatterRefresh:
    """The derived-frontmatter contract: status/enrichment converge with disk."""

    def test_media_only_item_converges_on_the_next_run(self, instance):
        # A kinds:[image] capture seeds no drainable unit; the session
        # hand-writes the media description — the run still reconciles it.
        item_id = "2026-08-19-photo-abc123"
        path = write_item(
            instance, item_id, urls=[], kinds=["image"], media=[f"media/{item_id}/photo.jpg"]
        )
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
        fetch = lambda _unit: Result(status=Status.MANUAL, meta={}, reason="paywalled")  # noqa: E731
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

    def test_mark_for_an_excluded_items_unit_still_heals(self, instance):
        path = write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        path.unlink()  # the item was excluded; its ledger unit remains
        confirmation = run_mod.mark(ctx, URL, Status.SKIPPED, reason="item excluded")
        assert confirmation.endswith("skipped")
        assert entry_for(ctx).status is Status.SKIPPED


class TestChildren:
    def test_children_re_enter_with_provenance_and_drain_same_run(self, instance):
        write_item(instance)
        child_url = "https://example.test/docs"

        def fetch(unit):
            children = []
            if unit.depth == 0:
                children = [run_mod.Child(url=child_url, via="harvest")]
            return Result(status=Status.DONE, meta={}, body="b" * 400, children=children)

        driver = FakeDriver(fetch_fn=fetch)
        ctx = make_ctx(instance, driver)
        run_mod.run(ctx)
        child = ledger.load(instance.ledger_path)[work_hash(child_url)]
        assert child.status is Status.DONE  # drained in the same run
        assert child.via == "harvest"
        assert child.parent == work_hash(URL)
        assert child.depth == 1
        assert child.item == ITEM  # inherited — the driver never assigns it
        assert [unit.depth for unit in driver.fetched] == [0, 1]

    def test_depth_cap_fires_and_is_ledger_recorded_never_surfaced(self, instance):
        write_item(instance, urls=["https://chain.test/0"])

        def fetch(unit):
            deeper = run_mod.Child(url=f"https://chain.test/{unit.depth + 1}", via="harvest")
            return Result(status=Status.DONE, meta={}, body="b" * 400, children=[deeper])

        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        report = run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        capped = entries[work_hash(f"https://chain.test/{MAX_DEPTH + 1}")]
        assert capped.status is Status.SKIPPED
        assert capped.capped is True
        assert capped.reason == f"depth cap ({MAX_DEPTH}) reached"
        assert capped.depth == MAX_DEPTH + 1
        fetched = [e for e in entries.values() if e.status is Status.DONE]
        assert len(fetched) == MAX_DEPTH + 1  # depths 0..4 fetched, 5 refused
        assert "depth cap" not in report  # judgment-drift signal, not user-facing

    def test_url_cap_fires_per_item_and_is_recorded(self, instance):
        write_item(instance, urls=["https://hub.test/root"])
        flood = [f"https://hub.test/page-{n}" for n in range(MAX_URLS_PER_ITEM + 3)]

        def fetch(unit):
            children = (
                [run_mod.Child(url=url, via="harvest") for url in flood] if unit.depth == 0 else []
            )
            return Result(status=Status.DONE, meta={}, body="b" * 400, children=children)

        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        report = run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        capped = [e for e in entries.values() if e.status is Status.SKIPPED]
        admitted = [e for e in entries.values() if e.status is Status.DONE]
        assert len(admitted) == MAX_URLS_PER_ITEM  # the root + 11 children
        assert len(capped) == len(flood) - (MAX_URLS_PER_ITEM - 1)
        assert all(e.capped for e in capped)
        assert all(e.reason == f"url cap ({MAX_URLS_PER_ITEM} per item) reached" for e in capped)
        assert "url cap" not in report


class TestParking:
    def test_waiting_parks_with_reason_until_a_provider_appears(self, instance, flippable_provider):
        # The FlippableProvider auto-drain: a waiting cohort ignores
        # runs until available() flips, then drains through its driver.
        write_item(instance)
        calls = {"n": 0}

        def fetch(_unit):
            calls["n"] += 1
            if calls["n"] == 1:
                return Result(
                    status=Status.WAITING,
                    meta={},
                    needs=Need.EXTRACT,
                    reason="no extractor for this format",
                )
            return Result(status=Status.DONE, meta={}, body="b" * 400)

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
        fetch = lambda _unit: Result(status=Status.BLOCKED, meta={}, reason="HTTP 403")  # noqa: E731
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
        assert "enrich run" in report

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


class TestRerun:
    def seed_rerun(self, ctx: RunContext) -> None:
        entry = entry_for(ctx)
        requeued = dataclasses.replace(
            entry, status=Status.QUEUED, path=None, title=None, rerun=True, via="migration-2"
        )
        ledger.append(ctx.instance.ledger_path, requeued)

    def test_unchanged_rerun_does_not_rewrite_or_report(self, instance):
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        out = instance.root / entry_for(ctx).path
        before = (out.read_text(), out.stat().st_mtime_ns)

        self.seed_rerun(ctx)
        report = run_mod.run(ctx)
        assert (out.read_text(), out.stat().st_mtime_ns) == before  # byte-compare held
        assert "cognitive work — none" in report
        assert entry_for(ctx).status is Status.DONE

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
        assert "cognitive work — none" in report

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
            return Result(status=Status.DONE, meta={"title": "t"}, body=urls[unit.url])

        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        item_dir = instance.enrichment_dir / ITEM
        snapshot = {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in item_dir.iterdir()
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
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in item_dir.iterdir()
        } == snapshot
        assert set(after) == set(before)  # no new units minted, audit lines only
        assert all(entry.status is Status.DONE for entry in after.values())

    def test_changed_rerun_overwrites_in_place_never_duplicates(self, instance):
        write_item(instance)
        body = {"text": "first body " * 40}
        fetch = lambda _unit: Result(status=Status.DONE, meta={}, body=body["text"])  # noqa: E731
        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)

        body["text"] = "rewritten body " * 40
        self.seed_rerun(ctx)
        report = run_mod.run(ctx)
        item_dir = instance.enrichment_dir / ITEM
        files = sorted(p.name for p in item_dir.glob("*.md"))
        assert files == [f"web-{work_hash(URL)[:6]}.md"]  # overwrite, never a second file
        assert "rewritten body" in (item_dir / files[0]).read_text()
        assert "1 changed" in report


class TestMediaStage:
    IMG1 = "https://cdn.example.test/a.png"
    IMG2 = "https://cdn.example.test/b.jpg"

    def media_fetch(self, urls):
        def fetch(_unit):
            return Result(status=Status.DONE, meta={}, body="b" * 400, media=list(urls))

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
        assert media.via == "media"
        assert media.kind is Kind.X  # the parent's kind
        assert media.parent == work_hash(URL)
        assert media.status is Status.DONE
        assert media.path == f"enrichment/{ITEM}/media-0.png"
        assert (instance.root / media.path).read_bytes() == b"png"
        assert entries[work_hash(self.IMG2)].path == f"enrichment/{ITEM}/media-1.jpg"

    def test_media_config_none_gates_the_stage_off(self, instance):
        write_item(instance)
        driver = FakeDriver(fetch_fn=self.media_fetch([self.IMG1]))
        ctx = make_ctx(
            instance,
            driver,
            config=Config(media_fetch=MediaFetch.NONE),
            transport=FakeTransport({}),
        )
        run_mod.run(ctx)
        assert work_hash(self.IMG1) not in ledger.load(instance.ledger_path)

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
        assert all(line["via"] == "media" for line in audit)

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
        assert "enrich run" in report

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

    def test_media_does_not_count_toward_the_url_cap(self, instance):
        write_item(instance)
        transport = FakeTransport(
            {self.IMG1: HttpResponse(status=200, content_type="image/png", body=b"p")}
        )
        child_url = "https://example.test/docs"

        def fetch(unit):
            if unit.depth == 0:
                return Result(
                    status=Status.DONE,
                    meta={},
                    body="b" * 400,
                    media=[self.IMG1],
                    children=[run_mod.Child(url=child_url, via="harvest")],
                )
            return Result(status=Status.DONE, meta={}, body="b" * 400)

        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch), transport=transport)
        run_mod.run(ctx)
        drain = run_mod._Drain(ctx=ctx)  # noqa: SLF001 — asserting the counting rule directly
        assert drain.fetched_count(ITEM) == 2  # seed + child; media excluded


class TestExtractAssets:
    def asset_fetch(self, assets):
        def fetch(_unit):
            return Result(status=Status.DONE, meta={}, body="b" * 400, assets=list(assets))

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
        assert sum(1 for e in ledgered.values() if e.via == "extract-asset") == 2

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
        rows = sorted(
            (e for e in entries.values() if e.via == "extract-asset"), key=lambda e: e.url
        )
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
        row = next(
            e for e in ledger.load(instance.ledger_path).values() if e.via == "extract-asset"
        )
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
            (e for e in ledger.load(instance.ledger_path).values() if e.via == "extract-asset"),
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
            if line.strip() and json.loads(line).get("via") == "extract-asset"
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

    def test_fetch_urls_refuses_to_steal_another_items_unit(self, instance):
        write_item(instance, "2026-08-19-first-aaaaaa", urls=[URL])
        write_item(instance, "2026-08-19-second-bbbbbb", urls=["https://example.test/other"])
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        with pytest.raises(ValueError, match="2026-08-19-first-aaaaaa"):
            run_mod.fetch_urls(ctx, "2026-08-19-second-bbbbbb", [URL])
        # The unit stayed with its owner, provenance intact:
        entry = ledger.load(instance.ledger_path)[work_hash(URL)]
        assert entry.item == "2026-08-19-first-aaaaaa"
        assert entry.status is Status.DONE

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
        assert refused.capped is True
        assert "url cap" in (refused.reason or "")

        # A repeat WITHOUT --force is refused again — the refusal marker is
        # not an admitted unit and must never sneak in as a "rerun":
        run_mod.fetch_urls(ctx, ITEM, [extra])
        assert ledger.load(instance.ledger_path)[work_hash(extra)].status is Status.SKIPPED

        run_mod.fetch_urls(ctx, ITEM, [extra], force=True)
        forced = ledger.load(instance.ledger_path)[work_hash(extra)]
        assert forced.status is Status.DONE
        # The cap fire stayed in the audit trail:
        lines = instance.ledger_path.read_text().split("\n")
        assert any("exceeded by --force" in line for line in lines)

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
        assert again.capped is True

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
        fetch = lambda _unit: Result(status=Status.BLOCKED, meta={}, reason="HTTP 403")  # noqa: E731
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

    def test_mark_error_is_refused(self, instance):
        ctx = make_ctx(instance, FakeDriver())
        with pytest.raises(ValueError, match="engine outcomes"):
            run_mod.mark(ctx, URL, Status.ERROR)

    def test_record_pass_appends_with_harvest_rules_version(self, instance):
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

    def test_compact_reports_removed_lines(self, instance):
        write_item(instance)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)  # queued + done = 2 lines, 1 hash
        assert run_mod.compact(ctx) == "compacted: 1 superseded line removed"


class TestStatusReport:
    def test_counts_waiting_cohorts_and_orphans(self, instance):
        write_item(instance)
        fetch = lambda _unit: Result(  # noqa: E731
            status=Status.WAITING, meta={}, needs=Need.TRANSCRIBE, reason="no captions"
        )
        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        orphan_dir = instance.enrichment_dir / "2026-08-19-orphan-abcdef"
        orphan_dir.mkdir()
        (orphan_dir / "web-abc123.md").write_text("enriched, never digested")
        report = run_mod.status_report(ctx)
        assert "waiting" in report
        assert "transcribe" in report
        assert "2026-08-19-orphan-abcdef" in report
        assert "interrupted-session backstop" in report

    def test_item_view_lists_every_unit_with_provenance(self, instance):
        write_item(instance)

        def fetch(unit):
            if unit.depth == 0:
                return Result(
                    status=Status.DONE,
                    meta={"title": "t"},
                    body="substantial body " * 30,
                    children=[Child(url="https://example.test/child", via="harvest")],
                )
            return Result(
                status=Status.WAITING, meta={}, needs=Need.TRANSCRIBE, reason="no captions"
            )

        ctx = make_ctx(instance, FakeDriver(fetch_fn=fetch))
        run_mod.run(ctx)
        report = run_mod.status_report(ctx, item_id=ITEM)
        assert report.startswith(f"item {ITEM} — 2 units")
        assert "done" in report
        assert f"→ enrichment/{ITEM}/web-" in report
        assert "waiting" in report
        assert "needs transcribe" in report
        assert "(via harvest, depth" in report
        assert "capabilities" not in report  # a ledger query, not the summary

    def test_item_view_without_units_is_honest(self, instance):
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.status_report(ctx, item_id="2026-08-19-note-aaaaaa")
        assert "no ledger work units" in report

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
        assert "reported upstream: 1 issue" in report
        assert "filed engine issue" in report
        assert (instance.state_dir / "issue-reports.jsonl").exists()

    def test_world_failures_never_file(self, instance):
        write_item(instance)
        gh = FakeGh()
        fetch = lambda _unit: Result(status=Status.BLOCKED, meta={})  # noqa: E731
        run_mod.run(make_ctx(instance, FakeDriver(fetch_fn=fetch), gh=gh))
        assert gh.calls == []  # blocked/dead/manual/waiting are not engine bugs

    def test_report_issues_false_files_nothing(self, instance):
        gh = FakeGh()
        ctx = dataclasses.replace(
            self._crashing_ctx(instance, gh), config=Config(report_issues=False)
        )
        report = run_mod.run(ctx)
        assert gh.calls == []
        assert "reported upstream" not in report

    def test_filer_failure_is_a_note_and_the_run_completes(self, instance):
        report = run_mod.run(self._crashing_ctx(instance, refuse_gh))
        assert "issue filing failed" in report
        assert "error" in report  # the unit outcome is still ledgered and reported

    def test_torn_memory_never_loses_the_run_report(self, instance):
        (instance.state_dir / "issue-reports.jsonl").write_text('{"note": "torn"}\n')
        report = run_mod.run(self._crashing_ctx(instance, FakeGh()))
        assert "issue filing failed" in report
        assert "enrich run" in report  # the report itself survived


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

    def test_only_the_full_run_derives_the_listing(self, instance):
        self._text_item(instance)
        report = run_mod.run_transcribe(make_ctx(instance, FakeDriver()))
        assert "no-source item" not in report


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
        done = [e for e in ledger.load(ctx.instance.ledger_path).values()
                if e.status is Status.DONE]
        queued = [e for e in ledger.load(ctx.instance.ledger_path).values()
                  if e.status is Status.QUEUED]
        assert len(done) == run_mod.RERUN_DRAIN_CAP
        assert len(queued) == 5
        assert (
            f"rerun cohort: {run_mod.RERUN_DRAIN_CAP} of "
            f"{run_mod.RERUN_DRAIN_CAP + 5} drained; 5 queue for the next run"
        ) in report
        # The next run drains the remainder and says so.
        second = run_mod.run(make_ctx(instance, FakeDriver()))
        remaining = [e for e in ledger.load(ctx.instance.ledger_path).values()
                     if e.status is Status.QUEUED]
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

    def test_blocked_head_still_corrects_on_the_get(self, instance):
        pdf = HttpResponse(
            status=200, content_type="application/octet-stream", body=fixture_bytes("paper.pdf")
        )
        blocked = HttpResponse(status=403, content_type="text/html", body=b"")

        class MethodAware:
            def __call__(self, url, *, method="GET"):  # noqa: ARG002 — routes on method alone
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
            {
                self.PDF_URL: HttpResponse(
                    status=200, content_type="application/pdf", body=self.HTML
                )
            }
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
            self.PDF_URL: HttpResponse(
                status=200, content_type="text/html", body=article.encode()
            )
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
    # line (leading indicators, padding): each must survive the
    # frontmatter round trip as the intended string.
    RETYPED = (
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
            text = _render_enrichment("https://example.test/post", TODAY, {"title": value}, "body")
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
