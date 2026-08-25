"""Transcribe-drain tests: acquisition, priming, lifecycle, caps."""

import dataclasses
import json
import os
import re
from pathlib import Path

import pytest

from dex_engine import corpus
from dex_engine.capabilities import Capabilities
from dex_engine.drivers.podcast import PodcastDriver
from dex_engine.drivers.transport import HttpResponse, urllib_transport
from dex_engine.drivers.web import WebDriver
from dex_engine.drivers.youtube import YouTubeDriver, _video_meta
from dex_engine.drivers.ytdlp import ProbeError, YoutubeAudio, cached_audio
from dex_engine.pipeline import ledger
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.classify import (
    Classification,
    ProviderInputError,
    ProviderUnavailableError,
)
from dex_engine.pipeline.enrichment import read_enrichment
from dex_engine.pipeline.registry import build_drivers
from dex_engine.pipeline.run import _Drain
from dex_engine.pipeline.transcribe import (
    HEAD_MAX_TOKENS,
    TRANSCRIBE_RUN_CAP,
    Acquired,
    _download_enclosure,
    _prompt,
    acquire_youtube_audio,
    estimated_tokens,
    keep_last_tokens,
)
from dex_engine.pipeline.types import (
    Availability,
    Config,
    Format,
    Instance,
    Kind,
    LedgerEntry,
    MediaFetch,
    Need,
    Status,
)
from dex_engine.pipeline.urls import work_hash
from tests.capabilities.conftest import FakeTranscriber, fixture_bytes
from tests.conftest import FakeDriver
from tests.drivers.conftest import (
    FakeTransport,
    fixture_text,
    html_response,
    truncating_server,
)
from tests.pipeline.test_run import ITEM, TODAY, entry_for, make_ctx, write_item

VIDEO_URL = "https://youtube.com/watch?v=abc123"


class FakeDownload:
    """A scriptable yt-dlp seam: writes fake audio into the cache."""

    def __init__(
        self,
        *,
        raise_: Exception | None = None,
        description: str = "A talk about anydoc, JSONL and dex.",
    ) -> None:
        self.raise_ = raise_
        self.description = description
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
            description=self.description,
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

    def test_a_renamed_items_park_file_is_read_from_the_live_items_directory(self, instance):
        # The transcribe drain composes onto the file the park wrote, and
        # it finds that file at `enrichment/<item>/<kind>-<hash6>.md`. The
        # unit comes off the ledger carrying whatever id its line was
        # written under, so a rename since leaves the drain reading a
        # directory that is gone — and it would write a transcript with the
        # description silently dropped, over the park that held it.
        renamed = "2026-08-19-example-renamed-55ad7b"
        write_item(instance, renamed, urls=[VIDEO_URL])
        park = instance.enrichment_dir / renamed / f"youtube-{work_hash(VIDEO_URL)[:6]}.md"
        park.parent.mkdir(parents=True)
        park.write_text(
            f"---\nurl: {VIDEO_URL}\nfetched: '2026-08-01'\n---\n\n"
            "## Description\n\nRecorded on a phone.\n",
            encoding="utf-8",
        )
        entry = seed_waiting(instance)  # the line still names the old id
        assert entry.item == ITEM
        run_mod.run_transcribe(
            transcribe_ctx(instance, transcriber=FakeTranscriber("whisper-local", text="Words."))
        )
        content = park.read_text()
        assert "Recorded on a phone." in content  # the park's own description stands
        assert "## Transcript\n\nWords." in content

    def _park_through_the_driver(self, instance) -> Path:
        """Park the video the way the real driver does, and return its file."""
        write_item(instance, urls=[VIDEO_URL])
        info = json.loads(fixture_text("youtube", "info-without-captions.json"))
        driver = YouTubeDriver(probe=lambda _url: info, transport=FakeTransport({}))
        run_mod.run(make_ctx(instance, FakeDriver(), drivers=[driver]))
        return instance.enrichment_dir / ITEM / f"youtube-{work_hash(VIDEO_URL)[:6]}.md"

    def test_the_transcript_joins_the_parks_description_in_one_file(self, instance):
        # The driver's park wrote the description; the drain must append to
        # it, never write over it — and the two modules must agree on the
        # section headings they compose around.
        park = self._park_through_the_driver(instance)
        assert "Recorded on a phone" in park.read_text()
        transcriber = FakeTranscriber("whisper-local", text="The transcript text.", model="medium")
        run_mod.run_transcribe(transcribe_ctx(instance, transcriber=transcriber))
        content = park.read_text()
        assert "Recorded on a phone" in content  # the park's own description stands
        assert "anydoc" not in content  # not the re-probe's, which differs
        assert "## Transcript\n\nThe transcript text." in content

    def test_a_re_drain_never_duplicates_the_transcript(self, instance):
        park = self._park_through_the_driver(instance)
        first = FakeTranscriber("whisper-local", text="First pass words.")
        run_mod.run_transcribe(transcribe_ctx(instance, transcriber=first))
        ledger.append(
            instance.ledger_path,
            dataclasses.replace(
                ledger.load(instance.ledger_path)[work_hash(VIDEO_URL)],
                status=Status.WAITING,
                needs=Need.TRANSCRIBE,
                path=None,
                title=None,
            ),
        )
        second = FakeTranscriber("whisper-local", text="Second pass words.")
        run_mod.run_transcribe(transcribe_ctx(instance, transcriber=second))
        content = park.read_text()
        assert content.count("## Transcript") == 1
        assert content.count("## Description") == 1
        assert "Recorded on a phone" in content
        assert "Second pass words." in content
        assert "First pass words." not in content  # superseded, not stacked

    def _park_naming_the_transcript_heading(self, instance) -> Path:
        """Park a video whose own description contains a ``## Transcript`` line."""
        write_item(instance, urls=[VIDEO_URL])
        info = json.loads(fixture_text("youtube", "info-without-captions.json"))
        info["description"] = (
            "Chapters below.\n\n## Transcript\n\nauto-generated, see the pinned "
            "comment\n\nNotes: https://example.test/ep-notes"
        )
        driver = YouTubeDriver(probe=lambda _url: info, transport=FakeTransport({}))
        run_mod.run(make_ctx(instance, FakeDriver(), drivers=[driver]))
        park = instance.enrichment_dir / ITEM / f"youtube-{work_hash(VIDEO_URL)[:6]}.md"
        assert "https://example.test/ep-notes" in park.read_text()  # the park wrote it whole
        return park

    def _requeue_for_a_re_drain(self, instance) -> None:
        ledger.append(
            instance.ledger_path,
            dataclasses.replace(
                ledger.load(instance.ledger_path)[work_hash(VIDEO_URL)],
                status=Status.WAITING,
                needs=Need.TRANSCRIBE,
                path=None,
                title=None,
            ),
        )

    def test_a_description_naming_the_transcript_heading_survives_the_drain(self, instance):
        # A description is arbitrary author text, headings included. Reading
        # the park's description back by splitting on the heading truncated
        # it at the author's own line, and the drain then wrote the
        # truncation back — the tail gone from disk, unrecoverable.
        park = self._park_naming_the_transcript_heading(instance)
        transcriber = FakeTranscriber("whisper-local", text="The transcript text.")
        run_mod.run_transcribe(transcribe_ctx(instance, transcriber=transcriber))
        content = park.read_text()
        assert "https://example.test/ep-notes" in content  # and the drain kept it whole
        assert "auto-generated, see the pinned comment" in content
        assert "## Transcript\n\nThe transcript text." in content

    def test_such_a_description_survives_a_re_drain_too(self, instance):
        # Once the file HAS a transcript section, the heading is the split
        # again — and the section the drain appended is the last one.
        park = self._park_naming_the_transcript_heading(instance)
        run_mod.run_transcribe(
            transcribe_ctx(instance, transcriber=FakeTranscriber("whisper-local", text="First."))
        )
        self._requeue_for_a_re_drain(instance)
        run_mod.run_transcribe(
            transcribe_ctx(instance, transcriber=FakeTranscriber("whisper-local", text="Second."))
        )
        content = park.read_text()
        assert "https://example.test/ep-notes" in content
        assert content.endswith("## Transcript\n\nSecond.\n")
        assert "First." not in content
        assert content.count("## Description") == 1

    def test_a_transcript_with_no_description_is_still_its_own_section(self, instance):
        # No description on either side — park or probe. The transcript
        # keeps its heading anyway: without it a re-drain reads the stored
        # transcript back as "description" and stacks itself under it.
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        ctx = transcribe_ctx(
            instance,
            transcriber=FakeTranscriber("whisper-local", text="First pass words."),
            download=FakeDownload(description=""),
        )
        run_mod.run_transcribe(ctx)
        park = instance.root / str(entry_for(ctx, VIDEO_URL).path)
        _, body = read_enrichment(park)
        assert body == "## Transcript\n\nFirst pass words."
        self._requeue_for_a_re_drain(instance)
        run_mod.run_transcribe(
            transcribe_ctx(
                instance,
                transcriber=FakeTranscriber("whisper-local", text="Second pass words."),
                download=FakeDownload(description=""),
            )
        )
        _, body = read_enrichment(park)
        assert body == "## Transcript\n\nSecond pass words."

    def test_drained_meta_shape_matches_the_captions_path(self, instance, tmp_path):
        # One frontmatter shape per kind, whichever route produced the
        # transcript: the drain's meta keys are exactly
        # the captions path's.
        entry = seed_waiting(instance)
        acquired = acquire_youtube_audio(entry, tmp_path / "park.md", tmp_path, FakeDownload())
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
        assert "Nothing to report from this run" in report

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

    def test_show_notes_naming_the_transcript_heading_survive_the_drain(self, instance):
        # Show notes are the publisher's prose, and one that publishes its
        # own transcript writes exactly this heading.
        feed = fixture_text("podcast", "feed.xml").replace(
            "<p>We discuss append-only ledgers with "
            '<a href="https://example.test/guest">Ada Guest</a>.</p>',
            "<p>We discuss append-only ledgers.</p><p>## Transcript</p>"
            "<p>Full text at https://engineering-distilled.test/ep42</p>",
        )
        self.park_via_driver(instance, feed=feed)
        entry = self.entry(instance)
        record = instance.enrichment_dir / ITEM / f"podcast-{entry.hash[:6]}.md"
        assert "https://engineering-distilled.test/ep42" in record.read_text()
        self.drain(instance)
        _, body = read_enrichment(
            instance.root / str(ledger.load(instance.ledger_path)[entry.hash].path)
        )
        assert "https://engineering-distilled.test/ep42" in body
        assert body.endswith("## Transcript\n\nEpisode words.")

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

    def test_truncated_enclosure_is_blocked_never_an_engine_error(self, instance):
        # A server hanging up mid-body is the routine failure for a 100MB
        # enclosure; the IncompleteRead it raises is not an OSError, so
        # unnormalized it landed `error` + a filed issue, frozen until the
        # next engine release.
        self.park_via_driver(instance)
        entry = self.entry(instance)
        record = instance.enrichment_dir / ITEM / f"podcast-{entry.hash[:6]}.md"
        with truncating_server() as url:
            record.write_text(
                record.read_text(encoding="utf-8").replace(self.ENCLOSURE, url), encoding="utf-8"
            )
            ctx = transcribe_ctx(instance, transport=urllib_transport)
            run_mod.run_transcribe(ctx)
        drained = ledger.load(instance.ledger_path)[entry.hash]
        assert drained.status is Status.BLOCKED
        assert drained.attempts == 1
        assert "truncated response body" in (drained.reason or "")
        # The issue filer fires on `error` outcomes only — none here.
        assert drained.error is None

    def test_cdn_error_page_takes_the_blocked_lifecycle_not_manual(self, instance):
        # A 200 carrying an error page cached as <hash>.mp3 decoded as
        # garbage and parked the episode manual forever; nothing about the
        # EPISODE was learned, so it is blocked and retried.
        self.park_via_driver(instance)
        page = html_response("<!DOCTYPE html>\n<html><body>Access denied</body></html>")
        ctx = transcribe_ctx(instance, transport=FakeTransport({self.ENCLOSURE: page}))
        run_mod.run_transcribe(ctx)
        entry = ledger.load(instance.ledger_path)[self.entry(instance).hash]
        assert entry.status is Status.BLOCKED
        assert entry.attempts == 1
        assert entry.needs is Need.TRANSCRIBE
        assert "HTML page" in (entry.reason or "")
        assert audio_files(instance) == []  # the page never became cached "audio"

    def test_real_audio_that_will_not_decode_still_parks_manual(self, instance):
        # The guards must not swallow the genuine bad-audio case: bytes
        # that are not markup reach the provider, and its verdict stands.
        self.park_via_driver(instance)
        angry = FakeTranscriber(raise_=ProviderInputError("could not decode the audio"))
        transport = FakeTransport({self.ENCLOSURE: html_response("AUDIO-BYTES")})
        ctx = transcribe_ctx(instance, transcriber=angry, transport=transport)
        run_mod.run_transcribe(ctx)
        entry = ledger.load(instance.ledger_path)[self.entry(instance).hash]
        assert entry.status is Status.MANUAL
        assert "could not decode" in (entry.reason or "")

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


class TestCorrectedUnitLandsItsTranscript:
    """A web → podcast correction whose transcript lands drops the web view."""

    EPISODE_URL = "https://engineering-distilled.test/episodes/ledgers-as-work-queues"
    FEED_URL = "https://feeds.pods.test/engineering-distilled.rss"
    ENCLOSURE = "https://cdn.pods.test/ed/ep42.mp3?sig=abc123"

    def _requeue(self, ctx, url):
        entry = entry_for(ctx, url)
        ledger.append(
            ctx.instance.ledger_path,
            dataclasses.replace(
                entry, status=Status.QUEUED, path=None, title=None, via="migration-2", rerun=True
            ),
        )

    def test_three_runs_leave_one_enrichment_file(self, instance):
        # Run 1 extracts the episode page as an ordinary article; the world
        # then serves the page's audio and a rerun corrects the kind, which
        # parks waiting (the web output rightly stands through a park). The
        # transcript lands through run_transcribe — the OTHER landing site —
        # and the stale web view of the same unit must leave with it, or the
        # item lists two files for one work unit forever.
        responses = {
            self.EPISODE_URL: html_response(fixture_text("web", "article.html")),
            self.FEED_URL: html_response(fixture_text("podcast", "feed.xml")),
        }
        transport = FakeTransport(responses)
        item_path = write_item(instance, urls=[self.EPISODE_URL])
        quiet = Config(media_fetch=MediaFetch.NONE)  # the article's og:image is not under test

        def fetch_ctx():
            return make_ctx(
                instance,
                FakeDriver(),
                drivers=[PodcastDriver(transport=transport), WebDriver(transport=transport)],
                transport=transport,
                config=quiet,
            )

        ctx = fetch_ctx()
        run_mod.run(ctx)
        first = entry_for(ctx, self.EPISODE_URL)
        assert first.kind is Kind.WEB
        assert first.status is Status.DONE
        web_out = instance.enrichment_dir / ITEM / f"web-{first.hash[:6]}.md"
        assert corpus.read_item(item_path).enrichment == [web_out.name]

        responses[self.EPISODE_URL] = html_response(
            fixture_text("podcast", "indie-episode-page.html")
        )
        self._requeue(ctx, self.EPISODE_URL)
        run_mod.run(fetch_ctx())
        parked = entry_for(ctx, self.EPISODE_URL)
        assert parked.kind is Kind.PODCAST
        assert parked.status is Status.WAITING
        podcast_out = instance.enrichment_dir / ITEM / f"podcast-{parked.hash[:6]}.md"
        assert web_out.exists()  # the park leaves the item as enriched as it found it
        assert corpus.read_item(item_path).enrichment == [podcast_out.name, web_out.name]

        transcriber = FakeTranscriber("whisper-local", text="Episode words.", model="medium")
        drained_ctx = transcribe_ctx(
            instance,
            transcriber=transcriber,
            transport=FakeTransport({self.ENCLOSURE: html_response("AUDIO-BYTES")}),
        )
        run_mod.run_transcribe(drained_ctx)
        done = entry_for(ctx, self.EPISODE_URL)
        assert done.status is Status.DONE
        assert done.path == f"enrichment/{ITEM}/{podcast_out.name}"
        assert "Episode words." in podcast_out.read_text()
        assert not web_out.exists()  # the transcript superseded the pre-correction view
        assert corpus.read_item(item_path).enrichment == [podcast_out.name]


class TestMalformedModelName:
    """A model name HuggingFace rejects must cost one provider, not the verb."""

    MODEL = "Systran/faster-whisper/large-v3"  # the real repo id has no third slash

    @pytest.fixture(autouse=True)
    def _no_api_key(self, monkeypatch):
        # Both transcribers must be unavailable for the drain to park —
        # a developer's exported key would otherwise reach a real endpoint.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    def ctx(self, instance, *, from_config: bool):
        # The config shape and the `--model` shape reach the same provider.
        caps = (
            Capabilities.build(Config(transcribe_model=self.MODEL))
            if from_config
            else Capabilities.build(Config(), model=self.MODEL)
        )
        return make_ctx(
            instance, FakeDriver(), capabilities=caps, provider_available=caps.available
        )

    @pytest.mark.parametrize("from_config", [True, False])
    def test_status_reports_the_provider_unavailable(self, instance, from_config):
        ctx = self.ctx(instance, from_config=from_config)
        report = " ".join(run_mod.status_report(ctx).split())  # the surface wraps
        assert "unknown whisper model" in report

    @pytest.mark.parametrize("from_config", [True, False])
    def test_run_and_transcribe_park_the_job_instead_of_crashing(self, instance, from_config):
        write_item(instance, urls=[VIDEO_URL])
        seed_waiting(instance)
        ctx = self.ctx(instance, from_config=from_config)
        run_mod.run(ctx)
        assert entry_for(ctx, VIDEO_URL).status is Status.WAITING
        report = " ".join(run_mod.run_transcribe(ctx).split())  # the surface wraps
        assert "no transcription provider available" in report
        assert "unknown whisper model" in report
        entry = entry_for(ctx, VIDEO_URL)
        assert entry.status is Status.WAITING  # no clock — it waits for a provider
        assert entry.needs is Need.TRANSCRIBE


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
        assert "### Read these yourself — 1 job the engine cannot do" in report
        assert ocr_url in report
        assert ledger.load(instance.ledger_path)[work_hash(ocr_url)].status is Status.WAITING
        # The transcribe job is parked, not listed as cognitive:
        lines = report.split("Read these yourself")[1]
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
        assert "## Ledger — 0 entries" in report
        assert "## Capabilities" in report
        assert "- **whisper-local** · `active`" in report

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
        assert cached_audio(tmp_path, "abc") is None
        assert list(tmp_path.iterdir()) == []

    def test_completed_download_lands_under_the_final_name(self, tmp_path):
        transport = FakeTransport({self.ENCLOSURE: html_response("AUDIO-BYTES")})
        path = _download_enclosure(self.ENCLOSURE, tmp_path, "abc", transport)
        assert isinstance(path, Path)
        assert path == tmp_path / "abc.mp3"
        assert b"AUDIO-BYTES" in path.read_bytes()
        assert [p.name for p in tmp_path.iterdir()] == ["abc.mp3"]


class TestEnclosureBodyGuards:
    """A 200 that isn't audio is the CDN's failure, not the episode's."""

    ENCLOSURE = "https://cdn.pods.test/ed/ep42.mp3?sig=abc123"

    def download(self, tmp_path, response) -> object:
        transport = FakeTransport({self.ENCLOSURE: response})
        return _download_enclosure(self.ENCLOSURE, tmp_path, "abc", transport)

    def audio(self, body: bytes, **kwargs) -> HttpResponse:
        return HttpResponse(status=200, content_type="audio/mpeg", body=body, **kwargs)

    def test_an_empty_body_is_blocked(self, tmp_path):
        outcome = self.download(tmp_path, self.audio(b""))
        assert isinstance(outcome, Classification)
        assert outcome.status is Status.BLOCKED
        assert "empty response body" in outcome.reason
        assert list(tmp_path.iterdir()) == []  # nothing cached to poison the retry

    def test_a_body_short_of_its_declared_length_is_blocked(self, tmp_path):
        outcome = self.download(tmp_path, self.audio(b"ID3" + b"x" * 17, content_length=5_000_000))
        assert isinstance(outcome, Classification)
        assert outcome.status is Status.BLOCKED
        assert "truncated" in outcome.reason
        assert list(tmp_path.iterdir()) == []

    def test_an_html_error_page_is_blocked_not_manual(self, tmp_path):
        # Cached as <hash>.mp3 this decodes as garbage and parks the
        # episode manual forever; blocked is the honest reading.
        page = HttpResponse(
            status=200,
            content_type="text/html",
            body=b"<!DOCTYPE html>\n<html><body>Access denied</body></html>",
        )
        outcome = self.download(tmp_path, page)
        assert isinstance(outcome, Classification)
        assert outcome.status is Status.BLOCKED
        assert "HTML page" in outcome.reason
        assert list(tmp_path.iterdir()) == []

    def test_a_complete_body_still_downloads(self, tmp_path):
        body = b"ID3\x04\x00\x00audio bytes"
        outcome = self.download(tmp_path, self.audio(body, content_length=len(body)))
        assert isinstance(outcome, Path)
        assert outcome.read_bytes() == body


class TestCachedAudio:
    def test_hard_crash_atomic_temp_is_a_partial_never_audio(self, tmp_path):
        # A kill mid-atomic-write can orphan the temp file itself; it must
        # be cleared like any partial, never picked up as completed audio.
        (tmp_path / "abc.mp3.k3j2f9.tmp").write_bytes(b"TRUNCATED")
        assert cached_audio(tmp_path, "abc") is None
        assert list(tmp_path.iterdir()) == []

    def test_partials_are_deleted_and_never_reused(self, tmp_path):
        (tmp_path / "abc.m4a.part").write_bytes(b"half")
        (tmp_path / "abc.ytdl").write_bytes(b"state")
        assert cached_audio(tmp_path, "abc") is None
        assert list(tmp_path.iterdir()) == []  # crash leftovers cleared for the re-download

    def test_complete_file_wins_and_partial_leftovers_are_cleared(self, tmp_path):
        (tmp_path / "abc.m4a").write_bytes(b"full")
        (tmp_path / "abc.m4a.part").write_bytes(b"half")
        path = cached_audio(tmp_path, "abc")
        assert path is not None
        assert path.name == "abc.m4a"
        assert [p.name for p in tmp_path.iterdir()] == ["abc.m4a"]

    def test_fragment_partials_count_as_partials(self, tmp_path):
        (tmp_path / "abc.mp4.part-Frag001").write_bytes(b"frag")
        assert cached_audio(tmp_path, "abc") is None

    def test_missing_cache_dir_is_simply_empty(self, tmp_path):
        assert cached_audio(tmp_path / "audio", "abc") is None


# Whisper's real prompt window: `previous_tokens[-(448 // 2 - 1):]` in
# faster_whisper/transcribe.py, and the same model server-side. A literal,
# deliberately — the budget is only honest if it is checked against the
# window rather than against itself.
WHISPER_WINDOW_TOKENS = 223

# Enough of each script to blow any budget, so the trim is always exercised.
CJK_NOTES = "这是一个关于软件工程的播客节目，今天我们讨论账本与工作队列的设计。" * 40
CYRILLIC_NOTES = "Это подкаст о разработке программного обеспечения и практиках. " * 40


class TestPromptBudget:
    """Whisper reads a prompt from its END, and counts the window in TOKENS."""

    TITLE = "Ledgers as Work Queues"
    SHOW = "Engineering Distilled"
    HEAD = f"{TITLE} — {SHOW}"

    def test_the_names_are_the_last_thing_in_the_prompt(self):
        # Not merely present: LAST. Whatever whisper drops for being over
        # the window, it drops from the front, so the front is where the
        # expendable vocabulary belongs.
        prompt = _prompt(self.TITLE, self.SHOW, "jargon " * 400)
        assert prompt.endswith(self.HEAD)
        assert prompt.startswith("jargon")

    def test_the_vocabulary_is_trimmed_from_its_tail(self):
        prompt = _prompt(self.TITLE, self.SHOW, "anydoc CTranslate2 " + "x" * 2000)
        assert prompt.startswith("anydoc CTranslate2 ")
        assert prompt.endswith(self.HEAD)
        assert "x" * 2000 not in prompt

    def test_a_short_prompt_is_untouched(self):
        assert _prompt(self.TITLE, self.SHOW, "notes") == f"notes — {self.HEAD}"
        assert _prompt(self.TITLE, None, "") == self.TITLE

    def test_a_latin_prompt_fits_whispers_window(self):
        prompt = _prompt(self.TITLE, self.SHOW, "jargon " * 400)
        assert estimated_tokens(prompt) <= WHISPER_WINDOW_TOKENS
        # And uses most of it: a budget that fits by being tiny is no budget.
        assert estimated_tokens(prompt) > WHISPER_WINDOW_TOKENS * 0.7

    def test_a_non_latin_prompt_fits_whispers_window_too(self):
        # The character cap this replaced was ~180 tokens of Cyrillic but
        # ~690 of Chinese — three windows' worth, and whisper takes the
        # overflow off the FRONT, where the title used to sit.
        for notes in (CJK_NOTES, CYRILLIC_NOTES):
            prompt = _prompt(self.TITLE, self.SHOW, notes)
            assert estimated_tokens(prompt) <= WHISPER_WINDOW_TOKENS
            assert prompt.endswith(self.HEAD)

    def test_the_budget_is_tokens_not_characters(self):
        latin = _prompt(self.TITLE, self.SHOW, "jargon " * 400).removesuffix(self.HEAD)
        cjk = _prompt(self.TITLE, self.SHOW, CJK_NOTES).removesuffix(self.HEAD)
        # Same token allowance spends very different numbers of characters —
        # a character cap would make these two lengths equal.
        assert len(latin) > 3 * len(cjk)
        assert abs(estimated_tokens(latin) - estimated_tokens(cjk)) < 5

    def test_emoji_dense_notes_are_charged_for_what_they_cost(self):
        # A show-notes header of emoji tokenizes at ~2 tokens per character.
        prompt = _prompt(self.TITLE, self.SHOW, "🚀🎧📚✨🔥" * 200)
        assert estimated_tokens(prompt) <= WHISPER_WINDOW_TOKENS
        assert prompt.endswith(self.HEAD)

    def test_vocabulary_alone_is_still_bounded(self):
        assert estimated_tokens(_prompt(None, None, "y " * 1000)) <= WHISPER_WINDOW_TOKENS
        assert estimated_tokens(_prompt(None, None, CJK_NOTES)) <= WHISPER_WINDOW_TOKENS

    def test_an_overlong_head_is_capped_rather_than_eating_the_prompt(self):
        prompt = _prompt("T" * 900, self.SHOW, "notes")
        assert estimated_tokens(prompt) <= WHISPER_WINDOW_TOKENS
        assert prompt.startswith("notes — ")
        assert estimated_tokens(prompt.removeprefix("notes — ")) <= HEAD_MAX_TOKENS

    def test_the_acquisition_path_composes_within_the_budget(self, instance, tmp_path):
        # The real composition site, not just the helper: a talkative
        # video description must not cost the title and channel.
        entry = seed_waiting(instance)

        def download(_url, cache_dir, stem) -> YoutubeAudio:
            cache_dir.mkdir(parents=True, exist_ok=True)
            path = cache_dir / f"{stem}.m4a"
            path.write_bytes(b"fake-audio")
            return YoutubeAudio(
                path=path,
                title="Ledgers at Scale",
                channel="Engineering Distilled",
                description="sponsors and links " * 200,
            )

        acquired = acquire_youtube_audio(entry, tmp_path / "park.md", tmp_path, download)
        assert isinstance(acquired, Acquired)
        assert acquired.prompt.endswith("Ledgers at Scale — Engineering Distilled")
        assert estimated_tokens(acquired.prompt) <= WHISPER_WINDOW_TOKENS


class TestTokenEstimate:
    def test_keeping_the_tail_keeps_the_end(self):
        assert keep_last_tokens("abcdefghij", 1) == "ij"
        assert keep_last_tokens("abc", 100) == "abc"
        assert keep_last_tokens("", 10) == ""

    def test_non_latin_characters_cost_more_than_ascii(self):
        assert estimated_tokens("这是一个") > estimated_tokens("abcd")
        assert estimated_tokens("абвг") > estimated_tokens("abcd")

    @pytest.mark.live
    @pytest.mark.ci_hostile  # a cold runner pulls whisper-tiny's ~2.4MB
    # tokenizer.json from HuggingFace, which throttles anonymous datacenter
    # traffic; a red morning reports HF's mood, not a drifted cost table.
    def test_the_estimate_covers_what_whispers_tokenizer_actually_does(self):
        # Opt-in: needs openai/whisper-tiny's tokenizer.json from HuggingFace.
        # Drift check on the cost table — a prompt this estimator passes must
        # really fit whisper's window, in every script, or the head it puts
        # last is the only thing that survives the overflow.
        from huggingface_hub import hf_hub_download  # noqa: PLC0415 — live-only dep
        from tokenizers import Tokenizer  # noqa: PLC0415 — live-only dep

        tokenizer = Tokenizer.from_file(hf_hub_download("openai/whisper-tiny", "tokenizer.json"))
        samples = {
            "english": "anydoc CTranslate2 ledger idempotent frontmatter yt-dlp " * 40,
            "chinese": CJK_NOTES,
            "cyrillic": CYRILLIC_NOTES,
            "japanese": "これはソフトウェアエンジニアリングに関するポッドキャストです。" * 40,
            "korean": "이것은 소프트웨어 엔지니어링에 관한 팟캐스트입니다 그리고 " * 40,
            "hindi": "यह सॉफ्टवेयर इंजीनियरिंग के बारे में एक पॉडकास्ट है। " * 40,
            "thai": "นี่คือพอดแคสต์เกี่ยวกับวิศวกรรมซอฟต์แวร์และแนวปฏิบัติ " * 40,
            "arabic": "هذه حلقة بودكاست عن هندسة البرمجيات وممارسات الهندسة اليومية. " * 40,
            "emoji": "🚀🎧📚✨🔥" * 200,
        }
        for script, notes in samples.items():
            prompt = _prompt("Ledgers as Work Queues", "Engineering Distilled", notes)
            real = len(tokenizer.encode(prompt, add_special_tokens=False).ids)
            assert real <= WHISPER_WINDOW_TOKENS, f"{script}: {real} tokens"
