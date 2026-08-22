"""Transcribe-drain tests: acquisition, priming, lifecycle, caps."""

import dataclasses
import os
import re
from pathlib import Path

import pytest

from dex_engine import corpus
from dex_engine.capabilities import Capabilities
from dex_engine.drivers.podcast import PodcastDriver
from dex_engine.drivers.transport import HttpResponse
from dex_engine.drivers.youtube import ProbeError, _video_meta
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.classify import ProviderInputError, ProviderUnavailableError
from dex_engine.pipeline.registry import build_drivers
from dex_engine.pipeline.run import _Drain
from dex_engine.pipeline.transcribe import (
    TRANSCRIBE_RUN_CAP,
    Acquired,
    YoutubeAudio,
    _cached_audio,
    _download_enclosure,
    acquire_youtube_audio,
    read_enrichment,
)
from dex_engine.pipeline.types import (
    Availability,
    Config,
    Format,
    Instance,
    Kind,
    LedgerEntry,
    Need,
    Status,
)
from dex_engine.pipeline.urls import work_hash
from tests.capabilities.conftest import FakeTranscriber, fixture_bytes
from tests.conftest import FakeDriver
from tests.drivers.conftest import FakeTransport, fixture_text, html_response
from tests.pipeline.test_run import ITEM, TODAY, entry_for, make_ctx, write_item

VIDEO_URL = "https://youtube.com/watch?v=abc123"


class FakeDownload:
    """A scriptable yt-dlp seam: writes fake audio into the cache."""

    def __init__(self, *, raise_: Exception | None = None) -> None:
        self.raise_ = raise_
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url, cache_dir, stem) -> YoutubeAudio:
        self.calls.append((url, stem))
        if self.raise_ is not None:
            raise self.raise_
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{stem}.m4a"
        path.write_bytes(b"fake-audio")
        return YoutubeAudio(
            path=path,
            title="Ledgers at Scale",
            channel="Engineering Distilled",
            description="A talk about anydoc, JSONL and dex.",
            duration_min=42,
            upload_date="20260810",
        )


def seed_waiting(
    instance: Instance, url: str = VIDEO_URL, *, kind: Kind = Kind.YOUTUBE
) -> LedgerEntry:
    entry = LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=ITEM,
        kind=kind,
        status=Status.WAITING,
        needs=Need.TRANSCRIBE,
        reason="no captions available",
        engine="0.2.0",
        date=TODAY,
    )
    ledger.append(instance.ledger_path, entry)
    return entry


def transcribe_ctx(instance, *, transcriber=None, download=None, transport=None):
    caps = Capabilities(
        transcribers=(transcriber if transcriber is not None else FakeTranscriber(),),
        extractors=(),
    )
    return make_ctx(
        instance,
        FakeDriver(),
        capabilities=caps,
        provider_available=caps.available,
        download_audio=download if download is not None else FakeDownload(),
        transport=transport if transport is not None else FakeTransport({}),
    )


def audio_files(instance: Instance) -> list[str]:
    audio_dir = instance.cache_dir / "audio"
    return sorted(p.name for p in audio_dir.glob("*")) if audio_dir.is_dir() else []


class TestYoutubeDrain:
    def test_end_to_end_acquire_prime_write_delete(self, instance):
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        transcriber = FakeTranscriber("whisper-local", text="The transcript text.", model="medium")
        download = FakeDownload()
        ctx = transcribe_ctx(instance, transcriber=transcriber, download=download)
        report = run_mod.run_transcribe(ctx)

        # Acquisition went through the drain's seam, keyed by the unit hash.
        assert download.calls == [(VIDEO_URL, work_hash(VIDEO_URL))]
        # Priming carried the item's known vocabulary.
        audio, prompt = transcriber.calls[0]
        assert audio.name == f"{work_hash(VIDEO_URL)}.m4a"
        assert "Ledgers at Scale" in prompt
        assert "anydoc" in prompt
        # The transcript wrote the youtube description+transcript pattern,
        # stamped via/model, and the ledger closed with the path.
        entry = entry_for(ctx, VIDEO_URL)
        assert entry.status is Status.DONE
        assert entry.title == "Ledgers at Scale"
        content = (instance.root / str(entry.path)).read_text()
        assert "via: whisper-local" in content
        assert "model: medium" in content
        assert "duration_min: 42" in content
        # Captions-path parity; quoted — bare it would retype as a YAML int.
        assert 'upload_date: "20260810"' in content
        assert "## Description" in content
        assert "## Transcript\n\nThe transcript text." in content
        # Audio lifecycle: deleted on successful transcription.
        assert audio_files(instance) == []
        assert ITEM in report  # the item lands on the cognitive work list

    def test_drained_meta_shape_matches_the_captions_path(self, instance, tmp_path):
        # One frontmatter shape per kind, whichever route produced the
        # transcript: the drain's meta keys are exactly
        # the captions path's.
        entry = seed_waiting(instance)
        acquired = acquire_youtube_audio(entry, tmp_path, FakeDownload())
        assert isinstance(acquired, Acquired)
        assert set(acquired.meta) == set(_video_meta({}))

    def test_bad_audio_is_manual_and_the_audio_is_kept(self, instance):
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        angry = FakeTranscriber(raise_=ProviderInputError("could not decode the audio"))
        ctx = transcribe_ctx(instance, transcriber=angry)
        run_mod.run_transcribe(ctx)
        entry = entry_for(ctx, VIDEO_URL)
        assert entry.status is Status.MANUAL
        assert "could not decode" in (entry.reason or "")
        assert audio_files(instance) == [f"{work_hash(VIDEO_URL)}.m4a"]  # kept for the human

    def test_call_time_capability_failure_stays_waiting(self, instance):
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        flaky = FakeTranscriber(raise_=ProviderUnavailableError("whisper-api returned HTTP 429"))
        ctx = transcribe_ctx(instance, transcriber=flaky)
        run_mod.run_transcribe(ctx)
        entry = entry_for(ctx, VIDEO_URL)
        assert entry.status is Status.WAITING
        assert entry.needs is Need.TRANSCRIBE
        assert "HTTP 429" in (entry.reason or "")
        assert audio_files(instance) == [f"{work_hash(VIDEO_URL)}.m4a"]  # retries reuse it

    def test_transient_acquisition_failure_takes_the_blocked_lifecycle(self, instance):
        # Acquisition failures are NOT provider failures — a failed audio
        # download is blocked with attempts, never no-clock waiting.
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        download = FakeDownload(raise_=ProbeError("HTTP Error 429: Too Many Requests"))
        ctx = transcribe_ctx(instance, download=download)
        run_mod.run_transcribe(ctx)
        entry = entry_for(ctx, VIDEO_URL)
        assert entry.status is Status.BLOCKED
        assert entry.attempts == 1
        assert entry.needs is Need.TRANSCRIBE  # the typed routing signal
        assert (entry.reason or "").startswith("audio acquisition failed")

    def test_acquisition_failures_escalate_manual_at_five(self, instance):
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        failing = FakeDownload(raise_=ProbeError("HTTP Error 429: Too Many Requests"))
        for expected_attempts in range(1, run_mod.MAX_BLOCKED_ATTEMPTS):
            run_mod.run_transcribe(transcribe_ctx(instance, download=failing))
            entry = ledger.load(instance.ledger_path)[work_hash(VIDEO_URL)]
            assert entry.status is Status.BLOCKED
            assert entry.attempts == expected_attempts
        run_mod.run_transcribe(transcribe_ctx(instance, download=failing))
        entry = ledger.load(instance.ledger_path)[work_hash(VIDEO_URL)]
        assert entry.status is Status.MANUAL
        assert f"still blocked after {run_mod.MAX_BLOCKED_ATTEMPTS} attempts" in (
            entry.reason or ""
        )
        assert "audio acquisition failed" in (entry.reason or "")
        # Manual is terminal for the mechanical drain — no further attempts.
        run_mod.run_transcribe(transcribe_ctx(instance, download=failing))
        assert ledger.load(instance.ledger_path)[work_hash(VIDEO_URL)].status is Status.MANUAL

    def test_blocked_acquisition_retry_routes_back_to_the_drain_and_completes(self, instance):
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        failing = FakeDownload(raise_=ProbeError("HTTP Error 429: Too Many Requests"))
        run_mod.run_transcribe(transcribe_ctx(instance, download=failing))
        assert ledger.load(instance.ledger_path)[work_hash(VIDEO_URL)].status is Status.BLOCKED
        # The world recovers: the blocked retry goes straight back through
        # the transcribe path (never the driver) and completes.
        ctx = transcribe_ctx(instance)
        run_mod.run_transcribe(ctx)
        assert entry_for(ctx, VIDEO_URL).status is Status.DONE

    def test_blocked_retry_routes_by_the_typed_field_not_the_reason_wording(self, instance):
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        failing = FakeDownload(raise_=ProbeError("HTTP Error 429: Too Many Requests"))
        run_mod.run_transcribe(transcribe_ctx(instance, download=failing))
        blocked = ledger.load(instance.ledger_path)[work_hash(VIDEO_URL)]
        assert blocked.needs is Need.TRANSCRIBE
        # Reword the parked prose entirely — routing must survive it.
        ledger.append(
            instance.ledger_path,
            dataclasses.replace(blocked, reason="the tube said no; try again later"),
        )
        ctx = transcribe_ctx(instance)
        run_mod.run_transcribe(ctx)
        assert entry_for(ctx, VIDEO_URL).status is Status.DONE

    def test_prose_mimicking_the_old_acquisition_wording_never_routes(self, instance):
        # A session-authored reason lives in the same namespace as the
        # engine's park wording; without `needs` it must not become a
        # transcribe job.
        page = "https://example.test/an-article"
        write_item(instance, urls=[page])
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=work_hash(page),
                url=page,
                item=ITEM,
                kind=Kind.WEB,
                status=Status.BLOCKED,
                attempts=1,
                reason="audio acquisition failed: session note, not a signal",
                engine="0.2.0",
                date=TODAY,
            ),
        )
        ctx = transcribe_ctx(instance)
        run_mod.run_transcribe(ctx)
        after = ledger.load(instance.ledger_path)[work_hash(page)]
        assert after.status is Status.BLOCKED  # untouched — not transcribe work
        assert after.attempts == 1

    def test_confirmed_gone_video_is_dead(self, instance):
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        download = FakeDownload(raise_=ProbeError("Video unavailable. This video was removed"))
        ctx = transcribe_ctx(instance, download=download)
        run_mod.run_transcribe(ctx)
        assert entry_for(ctx, VIDEO_URL).status is Status.DEAD

    def test_no_provider_notes_and_drains_nothing(self, instance):
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        broken = FakeTranscriber(ok=False, reason="faster-whisper not importable")
        ctx = transcribe_ctx(instance, transcriber=broken)
        report = run_mod.run_transcribe(ctx)
        assert "no transcription provider available" in report
        assert "faster-whisper not importable" in report
        assert entry_for(ctx, VIDEO_URL).status is Status.WAITING

    def test_first_run_model_download_is_noted(self, instance):
        # The report surfaces the slow first run.
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        fresh = FakeTranscriber(
            "whisper-local", reason="model 'medium' is not cached yet — downloads on first use"
        )
        ctx = transcribe_ctx(instance, transcriber=fresh)
        report = run_mod.run_transcribe(ctx)
        assert "not cached yet" in report

    def test_default_limit_is_ten_per_run(self, instance):
        urls = [f"https://youtube.com/watch?v=v{n:02d}" for n in range(12)]
        write_item(instance, urls=urls)
        for url in urls:
            seed_waiting(instance, url)
        ctx = transcribe_ctx(instance)
        run_mod.run_transcribe(ctx)  # default limit: TRANSCRIBE_RUN_CAP
        entries = ledger.load(instance.ledger_path)
        done = [e for e in entries.values() if e.status is Status.DONE]
        waiting = [e for e in entries.values() if e.status is Status.WAITING]
        assert len(done) == TRANSCRIBE_RUN_CAP
        assert len(waiting) == 2
        run_mod.run_transcribe(transcribe_ctx(instance))
        entries = ledger.load(instance.ledger_path)
        assert all(e.status is Status.DONE for e in entries.values() if e.kind is Kind.YOUTUBE)

    def test_explicit_limit_overrides(self, instance):
        urls = [f"https://youtube.com/watch?v=v{n:02d}" for n in range(3)]
        write_item(instance, urls=urls)
        for url in urls:
            seed_waiting(instance, url)
        run_mod.run_transcribe(transcribe_ctx(instance), limit=1)
        statuses = sorted(e.status.value for e in ledger.load(instance.ledger_path).values())
        assert statuses == ["done", "waiting", "waiting"]

    def test_failed_acquisitions_do_not_burn_the_limit_budget(self, instance):
        # The cap bounds attempts that REACH a provider — a failed
        # download must not starve the drainable rest of the cohort.
        urls = [f"https://youtube.com/watch?v=v{n:02d}" for n in range(3)]
        write_item(instance, urls=urls)
        for url in urls:
            seed_waiting(instance, url)
        good = FakeDownload()

        def download(url, cache_dir, stem):
            if url == urls[0]:
                raise ProbeError("HTTP Error 429: Too Many Requests")
            return good(url, cache_dir, stem)

        run_mod.run_transcribe(transcribe_ctx(instance, download=download), limit=1)
        statuses = sorted(e.status.value for e in ledger.load(instance.ledger_path).values())
        # The first unit's failed acquisition spent nothing: the second
        # reached the provider (the whole budget), the third deferred.
        assert statuses == ["blocked", "done", "waiting"]

    def test_provider_missing_passes_do_not_burn_the_run_cap(self, instance):
        urls = [f"https://youtube.com/watch?v=v{n:02d}" for n in range(TRANSCRIBE_RUN_CAP + 2)]
        write_item(instance, urls=urls)
        for url in urls:
            seed_waiting(instance, url)
        # The drain seam reports available but no capability registry is
        # wired: every unit re-parks waiting without reaching a provider —
        # none of those passes may count against the per-run cap.
        ctx = make_ctx(
            instance,
            FakeDriver(),
            provider_available=lambda _need, _fmt: Availability(ok=True),
        )
        report = run_mod.run(ctx)
        assert "transcription capped" not in report
        entries = ledger.load(instance.ledger_path)
        assert all(e.status is Status.WAITING for e in entries.values() if e.kind is Kind.YOUTUBE)


class TestPodcastDrain:
    APPLE_URL = "https://podcasts.apple.com/us/podcast/engineering/id9990001?i=8880042"
    # Keyed by the show id (9990001), not the ?i= episode id — the lookup
    # API resolves show ids only.
    LOOKUP_URL = "https://itunes.apple.com/lookup?id=9990001&entity=podcastEpisode&limit=200"
    FEED_URL = "https://feeds.pods.test/engineering-distilled.rss"
    ENCLOSURE = "https://cdn.pods.test/ed/ep42.mp3?sig=abc123"

    def park_via_driver(self, instance, *, feed: str | None = None) -> run_mod.RunContext:
        """Run the real podcast driver so the park writes its enclosure record."""
        write_item(instance, urls=[self.APPLE_URL])
        transport = FakeTransport(
            {
                self.LOOKUP_URL: html_response(fixture_text("podcast", "itunes-lookup.json")),
                self.FEED_URL: html_response(
                    feed if feed is not None else fixture_text("podcast", "feed.xml")
                ),
            }
        )
        ctx = make_ctx(
            instance,
            FakeDriver(),
            drivers=[PodcastDriver(transport=transport)],
            transport=transport,
        )
        run_mod.run(ctx)
        return ctx

    def entry(self, instance) -> LedgerEntry:
        driver = PodcastDriver()
        canonical = driver.canonical(self.APPLE_URL)
        return ledger.load(instance.ledger_path)[work_hash(canonical)]

    def noteless_feed(self) -> str:
        """The fixture feed with the matched episode's show notes removed."""
        feed = fixture_text("podcast", "feed.xml")
        return re.sub(r"\s*<description>.*?</description>", "", feed, count=1, flags=re.DOTALL)

    def drain(self, instance, *, text: str = "Episode words.") -> run_mod.RunContext:
        """One transcribe drain against a canned enclosure download."""
        transcriber = FakeTranscriber("whisper-local", text=text, model="medium")
        transport = FakeTransport({self.ENCLOSURE: html_response("AUDIO-BYTES")})
        ctx = transcribe_ctx(instance, transcriber=transcriber, transport=transport)
        run_mod.run_transcribe(ctx)
        return ctx

    def test_park_writes_show_notes_with_the_enclosure_pointer(self, instance):
        self.park_via_driver(instance)
        entry = self.entry(instance)
        assert entry.status is Status.WAITING
        assert entry.needs is Need.TRANSCRIBE
        assert entry.path is None  # path is success-only
        record = instance.enrichment_dir / ITEM / f"podcast-{entry.hash[:6]}.md"
        fields, body = read_enrichment(record)
        assert fields["enclosure"] == self.ENCLOSURE  # the drain's re-fetch pointer
        assert fields["title"] == "Ledgers as Work Queues"
        assert "[Ada Guest](https://example.test/guest)" in body

    def test_park_is_not_cognitive_work_yet(self, instance):
        ctx = self.park_via_driver(instance)
        report = run_mod.run(ctx)  # a second run: nothing new
        assert "cognitive work — none" in report

    def test_park_file_appears_in_the_items_enrichment_listing(self, instance):
        # The frontmatter refresh keys on every write, not only counted
        # outcomes — a waiting park's file must show in `enrichment:`
        # deterministically.
        self.park_via_driver(instance)
        entry = self.entry(instance)
        assert entry.status is Status.WAITING
        item = corpus.read_item(instance.corpus_dir / "2026" / f"{ITEM}.md")
        assert f"podcast-{entry.hash[:6]}.md" in item.enrichment

    def test_no_show_notes_park_still_writes_the_enclosure_pointer(self, instance):
        # Blocker regression: an episode without <description> parks
        # with body=None — the park file must exist anyway, because the
        # drain re-fetches audio from ITS frontmatter; without it the unit
        # would loop manual ("re-resolve") forever.
        self.park_via_driver(instance, feed=self.noteless_feed())
        entry = self.entry(instance)
        assert entry.status is Status.WAITING
        record = instance.enrichment_dir / ITEM / f"podcast-{entry.hash[:6]}.md"
        fields, body = read_enrichment(record)
        assert fields["enclosure"] == self.ENCLOSURE
        assert fields["title"] == "Ledgers as Work Queues"
        assert body == ""

    def test_no_show_notes_drain_succeeds_and_redrain_never_duplicates(self, instance):
        self.park_via_driver(instance, feed=self.noteless_feed())
        entry = self.entry(instance)
        self.drain(instance)
        drained = ledger.load(instance.ledger_path)[entry.hash]
        assert drained.status is Status.DONE
        _, body = read_enrichment(instance.root / str(drained.path))
        assert body == "## Transcript\n\nEpisode words."
        # Re-drain (requeue/heal): the stored transcript opens the body, so
        # a naive notes split would hand it back as "show notes" and
        # duplicate it in front of the fresh transcript.
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash=drained.hash,
                url=drained.url,
                item=drained.item,
                kind=Kind.PODCAST,
                status=Status.WAITING,
                needs=Need.TRANSCRIBE,
                reason="requeued for a re-drain",
                engine="0.2.0",
                date=TODAY,
            ),
        )
        self.drain(instance)
        final = ledger.load(instance.ledger_path)[entry.hash]
        assert final.status is Status.DONE
        _, body = read_enrichment(instance.root / str(final.path))
        assert body.count("## Transcript") == 1
        assert body.count("Episode words.") == 1

    def test_partial_download_is_deleted_and_refetched_never_reused(self, instance):
        # A crash mid-download leaves yt-dlp-style partials in the cache;
        # transcribing one would ledger truncated audio as done.
        self.park_via_driver(instance)
        entry = self.entry(instance)
        audio_dir = instance.cache_dir / "audio"
        audio_dir.mkdir(parents=True)
        (audio_dir / f"{entry.hash}.mp3.part").write_bytes(b"TRUNCATED")
        transcriber = FakeTranscriber("whisper-local", text="Episode words.", model="medium")
        transport = FakeTransport({self.ENCLOSURE: html_response("AUDIO-BYTES")})
        ctx = transcribe_ctx(instance, transcriber=transcriber, transport=transport)
        run_mod.run_transcribe(ctx)
        assert ledger.load(instance.ledger_path)[entry.hash].status is Status.DONE
        # The enclosure was re-fetched — the partial never reached whisper.
        assert ("GET", self.ENCLOSURE) in transport.calls
        assert transcriber.calls[0][0].name == f"{entry.hash}.mp3"
        assert audio_files(instance) == []  # partial deleted, audio deleted on success

    def test_drain_gets_enclosure_appends_transcript_deletes_audio(self, instance):
        self.park_via_driver(instance)
        entry = self.entry(instance)
        transcriber = FakeTranscriber("whisper-local", text="Episode words.", model="medium")
        transport = FakeTransport(
            {
                self.ENCLOSURE: html_response("AUDIO-BYTES"),
            }
        )
        ctx = transcribe_ctx(instance, transcriber=transcriber, transport=transport)
        run_mod.run_transcribe(ctx)

        drained = ledger.load(instance.ledger_path)[entry.hash]
        assert drained.status is Status.DONE
        record = instance.root / str(drained.path)
        fields, body = read_enrichment(record)
        assert fields["via"] == "whisper-local"
        assert fields["model"] == "medium"
        assert fields["enclosure"] == self.ENCLOSURE  # the re-fetch pointer survives
        assert body.index("Ada Guest") < body.index("## Transcript")  # notes, then transcript
        assert body.rstrip().endswith("Episode words.")
        # Priming used title + show notes vocabulary.
        prompt = transcriber.calls[0][1]
        assert "Ledgers as Work Queues" in prompt
        assert audio_files(instance) == []  # deleted on success

    def test_enclosure_fetch_failure_takes_the_blocked_lifecycle(self, instance):
        self.park_via_driver(instance)
        transport = FakeTransport({self.ENCLOSURE: OSError("connection reset")})
        ctx = transcribe_ctx(instance, transport=transport)
        run_mod.run_transcribe(ctx)
        entry = ledger.load(instance.ledger_path)[self.entry(instance).hash]
        assert entry.status is Status.BLOCKED
        assert entry.attempts == 1
        assert entry.needs is Need.TRANSCRIBE  # the typed routing signal
        assert (entry.reason or "").startswith("audio acquisition failed")

    def test_gone_enclosure_is_manual_with_the_reresolve_route(self, instance):
        # A 404ing enclosure is often an expired signed URL — the episode is
        # NOT confirmed gone; manual, with the requeue route stated.
        self.park_via_driver(instance)
        transport = FakeTransport(
            {self.ENCLOSURE: HttpResponse(status=404, content_type="text/html", body=b"")}
        )
        ctx = transcribe_ctx(instance, transport=transport)
        run_mod.run_transcribe(ctx)
        entry = ledger.load(instance.ledger_path)[self.entry(instance).hash]
        assert entry.status is Status.MANUAL
        assert "re-resolves" in (entry.reason or "")

    def test_missing_enrichment_record_is_manual(self, instance):
        write_item(instance, urls=["https://feeds.pods.test/x.rss"])
        seed_waiting(instance, "https://feeds.pods.test/x.rss", kind=Kind.PODCAST)
        ctx = transcribe_ctx(instance)
        run_mod.run_transcribe(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash("https://feeds.pods.test/x.rss")]
        assert entry.status is Status.MANUAL
        assert "no enrichment record" in (entry.reason or "")


class TestRunAutoDrain:
    def test_enrich_run_drains_waiting_transcribe_mechanically(self, instance):
        # Waiting means no mechanical provider — the moment one exists,
        # the ordinary run drains the cohort through the capability.
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        ctx = transcribe_ctx(instance)
        run_mod.run(ctx)
        assert entry_for(ctx, VIDEO_URL).status is Status.DONE

    def test_full_run_caps_transcription_and_notes_the_deferral(self, instance):
        urls = [f"https://youtube.com/watch?v=v{n:02d}" for n in range(TRANSCRIBE_RUN_CAP + 2)]
        write_item(instance, urls=urls)
        for url in urls:
            seed_waiting(instance, url)
        ctx = transcribe_ctx(instance)
        report = run_mod.run(ctx)
        entries = ledger.load(instance.ledger_path)
        done = [e for e in entries.values() if e.status is Status.DONE]
        assert len(done) == TRANSCRIBE_RUN_CAP  # never monopolize a machine
        assert f"transcription capped at {TRANSCRIBE_RUN_CAP}" in report

    def test_cap_deferrals_do_not_burn_the_run_limit(self, instance):
        # A waiting-transcribe backlog ahead of fresh captures: units the
        # cap defers are no-ops and must not spend `--limit`, or the fresh
        # work behind them never gets fetched.
        waiting = [f"https://youtube.com/watch?v=v{n:02d}" for n in range(TRANSCRIBE_RUN_CAP + 4)]
        fresh = [f"https://example.test/post-{n}" for n in range(3)]
        write_item(instance, urls=waiting)
        write_item(instance, "2026-08-19-fresh-99aa00", urls=fresh)
        for url in waiting:
            seed_waiting(instance, url)
        ctx = transcribe_ctx(instance)
        run_mod.run(ctx, limit=TRANSCRIBE_RUN_CAP + 5)
        entries = ledger.load(instance.ledger_path)
        transcribed = [
            e for e in entries.values() if e.kind is Kind.YOUTUBE and e.status is Status.DONE
        ]
        assert len(transcribed) == TRANSCRIBE_RUN_CAP
        fetched = [e for e in entries.values() if e.kind is Kind.WEB and e.status is Status.DONE]
        assert len(fetched) == len(fresh)  # deferrals spent none of the limit

    def test_unknown_kind_body_dispatch_is_loud_never_the_podcast_body(self, instance, monkeypatch):
        # The body pick must be total over the same kinds _acquire_audio
        # admits — a kind added to acquisition alone fails loudly instead of
        # silently taking the podcast body.
        url = "https://example.test/audio-page"
        write_item(instance, urls=[url])
        seed_waiting(instance, url, kind=Kind.WEB)
        audio = instance.cache_dir / "audio" / "a.mp3"
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"fake-audio")
        monkeypatch.setattr(
            _Drain,
            "_acquire_audio",
            lambda _self, _entry: Acquired(audio=audio, meta={}, prompt="", prefix=""),
        )
        ctx = transcribe_ctx(instance)
        run_mod.run_transcribe(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash(url)]
        assert entry.status is Status.ERROR
        assert "no transcript body for kind" in (entry.error or "")

    def test_cognitive_jobs_surface_on_the_report_never_drain(self, instance):
        # Jobs resolving to the cognitive floor are listed for the
        # session; the run never drains them. Transcribe waits silently —
        # it has no floor.
        write_item(instance)
        ocr_url = "https://example.test/scan.pdf"
        ocr_entry = LedgerEntry(
            hash=work_hash(ocr_url),
            url=ocr_url,
            item=ITEM,
            kind=Kind.FILE,
            format=Format.PDF,
            status=Status.WAITING,
            needs=Need.OCR,
            reason="scanned document",
            engine="0.2.0",
            date=TODAY,
        )
        ledger.append(instance.ledger_path, ocr_entry)
        seed_waiting(instance, VIDEO_URL)
        caps = Capabilities(
            transcribers=(FakeTranscriber(ok=False, reason="no key"),), extractors=()
        )
        ctx = make_ctx(instance, FakeDriver(), capabilities=caps, provider_available=caps.available)
        report = run_mod.run(ctx)
        assert "cognitive jobs — the session completes these with eyes" in report
        assert ocr_url in report
        assert ledger.load(instance.ledger_path)[work_hash(ocr_url)].status is Status.WAITING
        # The transcribe job is parked, not listed as cognitive:
        lines = report.split("cognitive jobs")[1]
        assert VIDEO_URL not in lines


class TestMediaFileSeeding:
    def test_materialized_document_seeds_and_extracts(self, instance):
        # Materialized files feed the pipeline — format detect →
        # extract queue → real anydoc extraction, end to end.
        media_dir = instance.root / "media" / "abc123"
        media_dir.mkdir(parents=True)
        (media_dir / "report.docx").write_bytes(fixture_bytes("report.docx"))
        write_item(instance, urls=[], media=["media/abc123/report.docx"])
        caps = Capabilities.build(Config())
        ctx = make_ctx(
            instance,
            FakeDriver(),
            drivers=build_drivers(capabilities=caps, root=instance.root),
            capabilities=caps,
            provider_available=caps.available,
        )
        run_mod.run(ctx)
        entry = ledger.load(instance.ledger_path)[work_hash("file:media/abc123/report.docx")]
        assert entry.kind is Kind.FILE
        assert str(entry.format) == "docx"
        assert entry.status is Status.DONE
        content = (instance.root / str(entry.path)).read_text()
        assert "Intro text before the figure." in content
        # The embedded image landed as an extract-asset under the media caps.
        assets = [e for e in ledger.load(instance.ledger_path).values() if e.via == "extract-asset"]
        assert len(assets) == 1
        assert assets[0].status is Status.DONE
        assert (instance.root / str(assets[0].path)).read_bytes().startswith(b"\x89PNG")

    def test_images_never_become_file_work(self, instance):
        media_dir = instance.root / "media" / "def456"
        media_dir.mkdir(parents=True)
        (media_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0 jpeg")
        write_item(instance, urls=[], media=["media/def456/photo.jpg"])
        ctx = make_ctx(instance, FakeDriver())
        run_mod.run(ctx)
        assert work_hash("file:media/def456/photo.jpg") not in ledger.load(instance.ledger_path)


class TestStatusIncludesCapabilities:
    def test_status_report_appends_the_capability_report(self, instance):
        caps = Capabilities(
            transcribers=(FakeTranscriber("whisper-local"),),
            extractors=(),
        )
        ctx = make_ctx(instance, FakeDriver(), capabilities=caps)
        report = run_mod.status_report(ctx)
        assert "ledger — 0 entries" in report
        assert "capabilities" in report
        assert "whisper-local (active)" in report

    def test_without_a_registry_the_status_stands_alone(self, instance):
        ctx = make_ctx(instance, FakeDriver())
        report = run_mod.status_report(ctx)
        assert "capabilities" not in report


class TestEnclosureDownloadAtomicity:
    ENCLOSURE = "https://cdn.pods.test/ed/ep42.mp3?sig=abc123"

    def test_crashed_download_leaves_nothing_under_the_final_name(self, tmp_path, monkeypatch):
        # A truncated <hash>.mp3 has no `.part` marker — the next run would
        # transcribe truncated audio and ledger it done. The write must be
        # atomic: temp in the cache dir, replace only when complete.
        real = os.fdopen

        def failing(fd, *args, **kwargs):
            real(fd, *args, **kwargs).close()
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("dex_engine.atomic.os.fdopen", failing)
        transport = FakeTransport({self.ENCLOSURE: html_response("AUDIO-BYTES")})
        with pytest.raises(OSError, match="No space left"):
            _download_enclosure(self.ENCLOSURE, tmp_path, "abc", transport)
        assert _cached_audio(tmp_path, "abc") is None
        assert list(tmp_path.iterdir()) == []

    def test_completed_download_lands_under_the_final_name(self, tmp_path):
        transport = FakeTransport({self.ENCLOSURE: html_response("AUDIO-BYTES")})
        path = _download_enclosure(self.ENCLOSURE, tmp_path, "abc", transport)
        assert isinstance(path, Path)
        assert path == tmp_path / "abc.mp3"
        assert b"AUDIO-BYTES" in path.read_bytes()
        assert [p.name for p in tmp_path.iterdir()] == ["abc.mp3"]


class TestCachedAudio:
    def test_hard_crash_atomic_temp_is_a_partial_never_audio(self, tmp_path):
        # A kill mid-atomic-write can orphan the temp file itself; it must
        # be cleared like any partial, never picked up as completed audio.
        (tmp_path / "abc.mp3.k3j2f9.tmp").write_bytes(b"TRUNCATED")
        assert _cached_audio(tmp_path, "abc") is None
        assert list(tmp_path.iterdir()) == []

    def test_partials_are_deleted_and_never_reused(self, tmp_path):
        (tmp_path / "abc.m4a.part").write_bytes(b"half")
        (tmp_path / "abc.ytdl").write_bytes(b"state")
        assert _cached_audio(tmp_path, "abc") is None
        assert list(tmp_path.iterdir()) == []  # crash leftovers cleared for the re-download

    def test_complete_file_wins_and_partial_leftovers_are_cleared(self, tmp_path):
        (tmp_path / "abc.m4a").write_bytes(b"full")
        (tmp_path / "abc.m4a.part").write_bytes(b"half")
        path = _cached_audio(tmp_path, "abc")
        assert path is not None
        assert path.name == "abc.m4a"
        assert [p.name for p in tmp_path.iterdir()] == ["abc.m4a"]

    def test_fragment_partials_count_as_partials(self, tmp_path):
        (tmp_path / "abc.mp4.part-Frag001").write_bytes(b"frag")
        assert _cached_audio(tmp_path, "abc") is None

    def test_missing_cache_dir_is_simply_empty(self, tmp_path):
        assert _cached_audio(tmp_path / "audio", "abc") is None


class TestReadEnrichment:
    def test_round_trips_quoted_values(self, tmp_path):
        record = tmp_path / "podcast-abc123.md"
        record.write_text(
            '---\nurl: https://x.test\nfetched: 2026-08-20\ntitle: "Ep: one"\n'
            'enclosure: "https://cdn.test/a.mp3?sig=1"\n---\n\nnotes body\n'
        )
        fields, body = read_enrichment(record)
        assert fields["title"] == "Ep: one"
        assert fields["enclosure"] == "https://cdn.test/a.mp3?sig=1"
        assert body == "notes body"

    def test_frontmatterless_file_is_all_body(self, tmp_path):
        record = tmp_path / "x.md"
        record.write_text("just text\n")
        assert read_enrichment(record) == ({}, "just text")
