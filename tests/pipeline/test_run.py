"""Tests for pipeline/run.py: the orchestrator, with fake drivers (§15)."""

import dataclasses
import datetime
import json
import os
from pathlib import Path

import pytest

from dex_engine import corpus
from dex_engine.drivers.transport import HttpResponse
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.classify import ProviderInputError
from dex_engine.pipeline.run import (
    MAX_BLOCKED_ATTEMPTS,
    MAX_DEPTH,
    MAX_URLS_PER_ITEM,
    MEDIA_MAX_FILES,
    RunContext,
    is_drainable,
    no_providers,
)
from dex_engine.pipeline.types import (
    Config,
    Instance,
    Kind,
    LedgerEntry,
    MediaFetch,
    Need,
    Result,
    Status,
)
from dex_engine.pipeline.urls import work_hash
from tests.conftest import FakeDriver
from tests.drivers.conftest import FakeTransport

TODAY = datetime.date(2026, 8, 20)
ITEM = "2026-08-19-example-55ad7b"
URL = "https://example.test/post"


def write_item(instance: Instance, item_id: str = ITEM, urls: list[str] | None = None) -> Path:
    item = corpus.CorpusItem(
        id=item_id,
        source="discord",
        channel="general",
        shared_by="Lee",
        date=datetime.date(2026, 8, 19),
        urls=urls if urls is not None else [URL],
        kinds=["web"],
        body="the owner's note\n",
    )
    path = instance.corpus_dir / "2026" / f"{item_id}.md"
    corpus.write_item(path, item)
    return path


def make_ctx(instance: Instance, driver: FakeDriver, **overrides) -> RunContext:
    defaults = {
        "instance": instance,
        "config": Config(),
        "drivers": [driver],
        "today": lambda: TODAY,
        "engine_version": "0.2.0",
        "transport": FakeTransport({}),
        "provider_available": no_providers,
        "sleep": lambda _seconds: None,
    }
    defaults.update(overrides)
    return RunContext(**defaults)


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
        assert statuses == ["done", "done", "queued"]  # the cohort drains across runs (§12)
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
        assert capped.reason == f"depth cap ({MAX_DEPTH}) reached"
        assert capped.depth == MAX_DEPTH + 1
        fetched = [e for e in entries.values() if e.status is Status.DONE]
        assert len(fetched) == MAX_DEPTH + 1  # depths 0..4 fetched, 5 refused
        assert "depth cap" not in report  # judgment-drift signal, not user-facing (§1)

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
        assert all(e.reason == f"url cap ({MAX_URLS_PER_ITEM} per item) reached" for e in capped)
        assert "url cap" not in report


class TestParking:
    def test_waiting_parks_with_reason_until_a_provider_appears(self, instance, flippable_provider):
        write_item(instance)
        calls = {"n": 0}

        def fetch(_unit):
            calls["n"] += 1
            if calls["n"] == 1:
                return Result(
                    status=Status.WAITING,
                    meta={},
                    needs=Need.TRANSCRIBE,
                    reason="no captions available",
                )
            return Result(status=Status.DONE, meta={}, body="b" * 400)

        driver = FakeDriver(kind=Kind.YOUTUBE, fetch_fn=fetch)
        ctx = make_ctx(instance, driver, provider_available=flippable_provider)
        report = run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.status is Status.WAITING
        assert entry.needs is Need.TRANSCRIBE
        assert entry.reason == "no captions available"
        assert "no captions available" in report  # parked rows are printed (§1)

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
            raise RuntimeError("exploded at /Users/lee/base fetching https://secret.test/a")

        driver = FakeDriver(fetch_fn=fetch)
        ctx = make_ctx(instance, driver)
        report = run_mod.run(ctx)
        entry = entry_for(ctx)
        assert entry.status is Status.ERROR
        assert "RuntimeError" in (entry.error or "")
        assert "/Users/lee" not in (entry.error or "")
        assert "secret.test" not in (entry.error or "")
        assert "secret.test" not in report

        run_mod.run(ctx)  # same engine — retrying a deterministic bug is waste
        assert len(driver.fetched) == 1
        newer = make_ctx(instance, driver, engine_version="0.2.1")
        run_mod.run(newer)
        assert len(driver.fetched) == 2

    def test_engine_bug_after_the_fetch_ledgers_error_never_aborts(self, instance):
        # The per-unit try covers ALL per-unit processing (§5): here the
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

    def test_driverless_kinds_park_manual_never_crash(self, instance):
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
        assert entry.reason == "no driver for kind 'podcast' yet (ships phase 3)"

    def test_file_detection_parks_waiting_extract(self, instance):
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
        assert entry.status is Status.WAITING
        assert entry.needs is Need.EXTRACT
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
        assert media.kind is Kind.X  # the parent's kind (§7)
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
        # §1: entries from birth — a crash mid-download must leave a queued
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

    def test_fetch_urls_unknown_item_is_loud(self, instance):
        ctx = make_ctx(instance, FakeDriver())
        with pytest.raises(ValueError, match="unknown corpus item"):
            run_mod.fetch_urls(ctx, "2026-01-01-nope-000000", ["https://example.test/x"])

    def test_fetch_urls_respects_the_cap_and_force_exceeds_it(self, instance):
        urls = [f"https://example.test/p{n}" for n in range(MAX_URLS_PER_ITEM)]
        write_item(instance, urls=urls)
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        extra = "https://example.test/extra"
        run_mod.fetch_urls(ctx, ITEM, [extra])
        refused = ledger.load(instance.ledger_path)[work_hash(extra)]
        assert refused.status is Status.SKIPPED
        assert "url cap" in (refused.reason or "")

        run_mod.fetch_urls(ctx, ITEM, [extra], force=True)
        forced = ledger.load(instance.ledger_path)[work_hash(extra)]
        assert forced.status is Status.DONE
        # The cap fire stayed in the audit trail:
        lines = instance.ledger_path.read_text().split("\n")
        assert any("exceeded by --force" in line for line in lines)

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
