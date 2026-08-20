"""Tests for the capability registries: resolution order, floors, the report."""

import pytest

from dex_engine.capabilities import DEFAULT_PROVIDER_ORDER, Capabilities
from dex_engine.capabilities.transcribe.whisper_api import DEFAULT_API_MODEL
from dex_engine.pipeline.types import Config, Format, Need
from dex_engine.render import surfaces
from tests.capabilities.conftest import FakeExtractor, FakeTranscriber


def caps(*, transcribers=(), extractors=(), ocr_providers=()) -> Capabilities:
    return Capabilities(
        transcribers=transcribers, extractors=extractors, ocr_providers=ocr_providers
    )


def provider_rows(payload) -> dict:
    rows = payload["capabilities"]
    assert isinstance(rows, list)
    return {row["name"]: row["providers"] for row in rows}


class TestBuild:
    def test_default_order_is_the_design_order(self):
        built = Capabilities.build(Config())
        assert [p.name for p in built.transcribers] == ["whisper-local", "whisper-api"]
        assert [p.name for p in built.extractors] == ["anydoc", "csv-builtin"]
        assert built.ocr_providers == ()
        assert built.extract_floor.name == "cognitive"
        assert built.ocr_floor.name == "cognitive"

    def test_config_order_promotes_without_delisting(self):
        config = Config(providers={"transcribe": ["whisper-api"]})
        built = Capabilities.build(config)
        assert [p.name for p in built.transcribers] == ["whisper-api", "whisper-local"]

    def test_unknown_provider_name_is_loud(self):
        with pytest.raises(ValueError, match="unknown provider"):
            Capabilities.build(Config(providers={"extract": ["textract"]}))

    def test_cognitive_is_not_configurable(self):
        # The floor is ALWAYS last in resolution order — config cannot
        # move it, so naming it is refused rather than silently reordered.
        with pytest.raises(ValueError, match="always last"):
            Capabilities.build(Config(providers={"extract": ["cognitive"]}))

    def test_model_override_reaches_the_local_provider(self):
        built = Capabilities.build(Config(transcribe_model="small"))
        assert built.transcribers[0].model == "small"
        overridden = Capabilities.build(Config(transcribe_model="small"), model="large-v3")
        assert overridden.transcribers[0].model == "large-v3"

    def test_api_model_config_reaches_the_api_provider(self):
        # transcribe_api_model is its own key: local size names never
        # leak into the API-side name, and --model still overrides per call.
        built = Capabilities.build(Config(transcribe_api_model="distil-whisper-large-v3-en"))
        assert built.transcribers[1].model == "distil-whisper-large-v3-en"
        assert built.transcribers[0].model == "medium"  # local stays local
        overridden = Capabilities.build(
            Config(transcribe_api_model="distil-whisper-large-v3-en"), model="whisper-1"
        )
        assert overridden.transcribers[1].model == "whisper-1"

    def test_api_model_defaults_without_config(self):
        built = Capabilities.build(Config(transcribe_model="small"))
        assert built.transcribers[1].model == DEFAULT_API_MODEL  # size name never leaks

    def test_default_order_covers_every_need(self):
        assert set(DEFAULT_PROVIDER_ORDER) == set(Need)


class TestResolution:
    def test_first_available_transcriber_wins(self):
        dormant = FakeTranscriber("first", ok=False, reason="no key")
        active = FakeTranscriber("second")
        c = caps(transcribers=(dormant, active))
        assert c.transcriber() is active
        assert c.available(Need.TRANSCRIBE).ok is True

    def test_no_available_transcriber_reports_every_reason(self):
        c = caps(
            transcribers=(
                FakeTranscriber("a", ok=False, reason="broken install"),
                FakeTranscriber("b", ok=False, reason="no key"),
            )
        )
        assert c.transcriber() is None
        availability = c.available(Need.TRANSCRIBE)
        assert availability.ok is False
        assert availability.reason == "a: broken install; b: no key"

    def test_extractor_resolution_is_per_format(self):
        # The Format is the contract, not the tool: each format falls
        # back independently when the all-formats provider is unavailable.
        anydoc_down = FakeExtractor("anydoc", ok=False, reason="wheel broken")
        csv_only = FakeExtractor("csv-builtin", formats=frozenset({Format.CSV}))
        c = caps(extractors=(anydoc_down, csv_only))
        assert c.extractor(Format.CSV) is csv_only
        assert c.extractor(Format.PDF) is None
        assert c.available(Need.EXTRACT, Format.CSV).ok is True
        assert c.available(Need.EXTRACT, Format.PDF).ok is False
        assert "wheel broken" in c.available(Need.EXTRACT, Format.PDF).reason

    def test_unsupported_format_parks_with_the_cognitive_truth(self):
        c = caps(extractors=(FakeExtractor("csv-builtin", formats=frozenset({Format.CSV})),))
        availability = c.available(Need.EXTRACT, Format.EPUB)
        assert availability.ok is False
        assert "cognitive floor" in availability.reason

    def test_ocr_is_never_mechanically_available(self):
        c = caps()
        availability = c.available(Need.OCR, Format.PDF)
        assert availability.ok is False
        assert "cognitive floor" in availability.reason

    def test_cognitive_jobs_are_extract_and_ocr_without_mechanical_providers(self):
        c = caps(extractors=(FakeExtractor("csv-builtin", formats=frozenset({Format.CSV})),))
        assert c.is_cognitive(Need.OCR, Format.PDF) is True
        assert c.is_cognitive(Need.EXTRACT, Format.PDF) is True  # nothing mechanical for pdf
        assert c.is_cognitive(Need.EXTRACT, Format.CSV) is False  # csv-builtin drains it
        # Transcription has NO cognitive floor: an unavailable provider means
        # the job waits for the capability, never lands on the session.
        assert c.is_cognitive(Need.TRANSCRIBE) is False


class TestReport:
    def test_active_dormant_and_floor_states(self):
        c = caps(
            transcribers=(
                FakeTranscriber("whisper-local"),
                FakeTranscriber("whisper-api", ok=False, reason="set OPENAI_API_KEY"),
            ),
            extractors=(FakeExtractor("anydoc"), FakeExtractor("csv-builtin")),
        )
        by_name = provider_rows(c.report_payload())
        assert by_name["transcribe"][0] == {"name": "whisper-local", "state": "active"}
        assert by_name["transcribe"][1] == {
            "name": "whisper-api",
            "state": "available",
            "note": "set OPENAI_API_KEY",
        }
        assert by_name["extract"][0]["state"] == "active"
        assert by_name["extract"][1] == {"name": "csv-builtin", "state": "available"}
        # Cognitive floors: last always; active only where nothing
        # mechanical outranks them (ocr), dormant otherwise (extract).
        assert by_name["extract"][-1]["name"] == "cognitive"
        assert by_name["extract"][-1]["state"] == "available"
        assert by_name["ocr"] == [
            {
                "name": "cognitive",
                "state": "active",
                "note": "the ingest session reads scanned pages with eyes",
            }
        ]

    def test_payload_renders_on_the_surface(self):
        # The designed example line, verbatim shape: active first, dormant with
        # its unmet requirement after the dot.
        c = caps(
            transcribers=(
                FakeTranscriber("whisper-local"),
                FakeTranscriber("whisper-api", ok=False, reason="set OPENAI_API_KEY"),
            ),
        )
        report = surfaces.render("capability-report", c.report_payload())
        flat = " ".join(report.split())  # the kernel may wrap long value lines
        assert "whisper-local (active)" in flat
        assert "whisper-api available — set OPENAI_API_KEY" in flat

    def test_real_build_payload_renders(self):
        # The registry built from a default Config must always produce a
        # renderable payload on this machine, whatever is installed.
        report = surfaces.render("capability-report", Capabilities.build(Config()).report_payload())
        assert report.startswith("capabilities")
        assert "transcribe" in report
        assert "ocr" in report
