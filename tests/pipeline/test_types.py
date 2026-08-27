"""Tests for pipeline/types.py: enums, dataclass validation, config, versions."""

import dataclasses
import datetime
import json
from pathlib import Path

import pytest

from dex_engine.pipeline.types import (
    Availability,
    Cap,
    Config,
    Content,
    Format,
    Instance,
    Job,
    Kind,
    LedgerEntry,
    MediaFetch,
    MigrationReport,
    Missing,
    Need,
    NeedsCapability,
    Redetected,
    Refused,
    Skipped,
    Status,
    Unusable,
    WorkUnit,
    parse_version,
    version_newer,
)

TODAY = datetime.date(2026, 8, 20)


BASE_ENTRY = LedgerEntry(
    hash="73bd784849",
    url="https://example.test/post",
    item="2026-08-19-example-55ad7b",
    kind=Kind.WEB,
    status=Status.QUEUED,
    engine="0.2.1",
    date=TODAY,
)


def entry(**overrides: object) -> LedgerEntry:
    return dataclasses.replace(BASE_ENTRY, **overrides)


class TestEnums:
    def test_enums_serialize_as_plain_strings(self):
        assert f"{Kind.X}" == "x"
        assert f"{Status.WAITING}" == "waiting"
        assert f"{Need.TRANSCRIBE}" == "transcribe"
        assert f"{MediaFetch.LEAD}" == "lead"
        assert json.dumps(Kind.YOUTUBE) == '"youtube"'

    def test_renames_are_clean_breaks_no_aliases(self):
        with pytest.raises(ValueError, match="tweet"):
            Kind("tweet")
        with pytest.raises(ValueError, match="blog"):
            Kind("blog")


class TestAvailability:
    def test_defaults_and_frozen(self):
        availability = Availability(ok=True)
        assert availability.reason == ""
        with pytest.raises(dataclasses.FrozenInstanceError):
            availability.ok = False  # ty: ignore[invalid-assignment]


def unit(**overrides: object) -> WorkUnit:
    base = WorkUnit(
        hash="73bd784849",
        url="https://example.test",
        kind=Kind.WEB,
        item="2026-08-19-example-55ad7b",
        depth=0,
    )
    return dataclasses.replace(base, **overrides)


class TestWorkUnit:
    def test_carries_no_ledger_bookkeeping(self):
        work = unit()
        assert work.format is None
        assert work.parent is None
        for bookkeeping in ("attempts", "engine", "rerun"):
            assert not hasattr(work, bookkeeping)

    @pytest.mark.parametrize("kind", [Kind.IMAGE, Kind.TEXT])
    def test_corpus_only_kinds_never_become_work_units(self, kind):
        with pytest.raises(ValueError, match="never becomes"):
            unit(kind=kind)

    def test_format_is_file_work_only(self):
        with pytest.raises(ValueError, match="file-work only"):
            unit(format=Format.PDF)
        assert unit(kind=Kind.FILE, format=Format.PDF).format is Format.PDF
        assert unit(kind=Kind.FILE).format is None

    @pytest.mark.parametrize("bad", ["", "xyz", "73BD784849", "73bd78484", "73bd7848499"])
    def test_hash_must_be_ten_lowercase_hex(self, bad):
        with pytest.raises(ValueError, match="hash"):
            unit(hash=bad)

    def test_url_and_item_must_not_be_empty(self):
        with pytest.raises(ValueError, match="url"):
            unit(url="")
        with pytest.raises(ValueError, match="item"):
            unit(item="")

    def test_depth_must_be_a_nonnegative_integer(self):
        with pytest.raises(ValueError, match=">= 0"):
            unit(depth=-1, parent="a1b2c3d4e5")
        with pytest.raises(ValueError, match="boolean"):
            unit(depth=True, parent="a1b2c3d4e5")

    def test_parent_and_depth_travel_together(self):
        # Depth 0 is the shared URL — it has no parent; spawned units have both.
        with pytest.raises(ValueError, match="parent and depth"):
            unit(parent="a1b2c3d4e5")
        with pytest.raises(ValueError, match="parent and depth"):
            unit(depth=1)
        spawned = unit(depth=1, parent="a1b2c3d4e5")
        assert spawned.parent == "a1b2c3d4e5"


class TestOutcomeUnion:
    """What a driver may state, with the wrong states unrepresentable.

    No outcome carries a Status at all; outputs exist only on Content; a
    failure variant must state its evidence; a redetection carries the
    corrected identity and nothing else.
    """

    def test_no_outcome_carries_a_status(self):
        for outcome in (
            Content(meta={}),
            Missing(evidence="HTTP 404"),
            Refused(evidence="HTTP 429"),
            Unusable(evidence="thin-extraction"),
            NeedsCapability(need=Need.TRANSCRIBE),
            Redetected(kind=Kind.FILE, format=Format.PDF),
        ):
            assert not hasattr(outcome, "status")

    def test_a_simple_content_return_uses_defaults(self):
        content = Content(meta={"title": "t"}, body="text")
        assert content.media == []
        assert content.assets == []

    def test_outputs_ride_content_only(self):
        # The flat Result admitted media on any status and validated assets
        # done-only by hand; on the union, no other variant has the fields.
        for variant in (Missing, Refused, Unusable):
            assert not hasattr(variant(evidence="e"), "media")
            assert not hasattr(variant(evidence="e"), "assets")
        assert not hasattr(NeedsCapability(need=Need.OCR), "media")
        assert not hasattr(Redetected(kind=Kind.WEB), "body")

    @pytest.mark.parametrize("variant", [Missing, Refused, Unusable])
    def test_a_failure_outcome_must_state_its_evidence(self, variant):
        with pytest.raises(ValueError, match="evidence"):
            variant(evidence="")
        # Whitespace-only is the same absence with padding — the design's
        # "required and non-empty" claim, not merely "truthy".
        with pytest.raises(ValueError, match="evidence"):
            variant(evidence="   ")

    def test_refused_is_transient_unless_the_driver_knows_better(self):
        assert Refused(evidence="HTTP 429").permanent is False
        assert Refused(evidence="payment/login required", permanent=True).permanent is True

    def test_unusable_is_rescuable_unless_nothing_is_there(self):
        assert Unusable(evidence="thin-extraction").rescuable is True
        assert Unusable(evidence="root url", rescuable=False).rescuable is False

    def test_needs_capability_carries_the_partial_content(self):
        park = NeedsCapability(
            need=Need.TRANSCRIBE, meta={"enclosure": "https://cdn.test/a.mp3"}, reason="resolved"
        )
        assert park.body is None
        assert park.meta["enclosure"] == "https://cdn.test/a.mp3"

    def test_redetected_non_work_kinds_are_rejected(self):
        with pytest.raises(ValueError, match="never becomes a work unit"):
            Redetected(kind=Kind.IMAGE)

    def test_redetected_format_is_file_work_only(self):
        with pytest.raises(ValueError, match="file-work only"):
            Redetected(kind=Kind.WEB, format=Format.PDF)

    def test_outcomes_are_frozen(self):
        outcome = Missing(evidence="HTTP 404")
        with pytest.raises(dataclasses.FrozenInstanceError):
            outcome.evidence = "rewritten"  # ty: ignore[invalid-assignment]


class TestLedgerEntryInvariants:
    def test_minimal_queued_entry(self):
        assert entry().status is Status.QUEUED

    def test_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry().status = Status.DONE  # ty: ignore[invalid-assignment]

    def test_waiting_requires_needs(self):
        with pytest.raises(ValueError, match="waiting"):
            entry(status=Status.WAITING)
        assert entry(status=Status.WAITING, needs=Need.EXTRACT).needs is Need.EXTRACT

    def test_needs_rides_waiting_and_blocked_only(self):
        with pytest.raises(ValueError, match="waiting parks and blocked retries"):
            entry(status=Status.QUEUED, needs=Need.OCR)
        with pytest.raises(ValueError, match="waiting parks and blocked retries"):
            entry(status=Status.DONE, needs=Need.TRANSCRIBE)

    def test_blocked_may_carry_needs(self):
        # The typed routing signal for an acquisition retry — never a
        # reason-prefix match.
        retry = entry(status=Status.BLOCKED, attempts=1, needs=Need.TRANSCRIBE)
        assert retry.needs is Need.TRANSCRIBE

    def test_the_cap_marker_is_skipped_only(self):
        marker = entry(status=Status.SKIPPED, cap=Cap.URL_REQUESTED, reason="url cap reached")
        assert marker.cap is Cap.URL_REQUESTED
        with pytest.raises(ValueError, match="skipped-only"):
            entry(status=Status.QUEUED, cap=Cap.URL_REQUESTED)
        with pytest.raises(ValueError, match="skipped-only"):
            entry(status=Status.DONE, cap=Cap.URL_REQUESTED)

    def test_blocked_requires_attempts_of_at_least_one(self):
        with pytest.raises(ValueError, match="attempts"):
            entry(status=Status.BLOCKED)
        with pytest.raises(ValueError, match="attempts"):
            entry(status=Status.BLOCKED, attempts=0)
        assert entry(status=Status.BLOCKED, attempts=1).attempts == 1

    def test_attempts_is_blocked_only(self):
        with pytest.raises(ValueError, match="blocked-only"):
            entry(status=Status.QUEUED, attempts=2)

    def test_error_requires_message(self):
        with pytest.raises(ValueError, match="error"):
            entry(status=Status.ERROR)
        assert entry(status=Status.ERROR, error="scrubbed: boom").error == "scrubbed: boom"

    def test_error_message_is_error_only(self):
        with pytest.raises(ValueError, match="error-only"):
            entry(status=Status.QUEUED, error="boom")

    def test_outputs_are_success_only(self):
        with pytest.raises(ValueError, match="success-only"):
            entry(status=Status.QUEUED, path="enrichment/i/web-73bd78.md")
        with pytest.raises(ValueError, match="success-only"):
            entry(status=Status.DEAD, title="gone")
        done = entry(status=Status.DONE, path="enrichment/i/web-73bd78.md", title="t")
        assert done.path is not None

    def test_done_without_output_is_representable(self):
        # `enrich mark <url> done` heals record no path — done alone is valid.
        assert entry(status=Status.DONE).path is None

    def test_engine_is_always_required(self):
        with pytest.raises(ValueError, match="engine"):
            entry(engine="")

    @pytest.mark.parametrize("via", ["harvest", "sniff", "migration-1", "migration-12"])
    def test_the_known_provenance_vocabulary_is_accepted(self, via):
        assert entry(via=via, rerun=True).via == via

    @pytest.mark.parametrize(
        "via",
        [
            "",
            "harvested",
            "migration-",
            "migration-0",
            "Harvest",
            "thread",
            "freehand",
            "media",
            "extract-asset",
        ],
    )
    def test_via_outside_the_vocabulary_is_rejected(self, via):
        # `thread` among them: the walk-up writes its chain into ONE
        # enrichment file, so nothing was ever ledgered under it.
        # `media` and `extract-asset` too: routing left `via` for the typed
        # `job` field, and the old spellings must not quietly come back.
        with pytest.raises(ValueError, match="via"):
            entry(via=via)

    def test_job_marks_media_and_asset_work(self):
        assert entry(job=Job.MEDIA, parent="a1b2c3d4e5", depth=1).job is Job.MEDIA
        assert entry(job=Job.ASSET, parent="a1b2c3d4e5", depth=1).job is Job.ASSET
        assert entry().job is None

    def test_title_without_path_is_rejected(self):
        with pytest.raises(ValueError, match="travel together"):
            entry(status=Status.DONE, title="orphaned title")

    @pytest.mark.parametrize("kind", [Kind.IMAGE, Kind.TEXT])
    def test_corpus_only_kinds_never_become_entries(self, kind):
        with pytest.raises(ValueError, match="never becomes"):
            entry(kind=kind)

    def test_format_is_file_work_only(self):
        with pytest.raises(ValueError, match="file-work only"):
            entry(format=Format.PDF)
        assert entry(kind=Kind.FILE, format=Format.PDF).format is Format.PDF

    @pytest.mark.parametrize("bad", ["", "xyz", "73BD784849", "73bd78484", "73bd7848499"])
    def test_hash_must_be_ten_lowercase_hex(self, bad):
        with pytest.raises(ValueError, match="hash"):
            entry(hash=bad)

    def test_url_and_item_must_not_be_empty(self):
        with pytest.raises(ValueError, match="url"):
            entry(url="")
        with pytest.raises(ValueError, match="item"):
            entry(item="")

    @pytest.mark.parametrize("bad", ["https://example.test/a\nb", "https://example.test/a\rb"])
    def test_url_must_be_a_single_line(self, bad):
        # A multi-line work key would brick the single-line item-status
        # surface; every legitimate writer produces one line.
        with pytest.raises(ValueError, match="single line"):
            entry(url=bad)

    def test_attempts_must_not_be_a_boolean(self):
        # attempts=True would serialize as JSON `true` — a line the boundary
        # itself refuses to re-parse.
        with pytest.raises(ValueError, match="boolean"):
            entry(status=Status.BLOCKED, attempts=True)

    def test_depth_must_be_a_nonnegative_integer(self):
        with pytest.raises(ValueError, match=">= 0"):
            entry(parent="a1b2c3d4e5", depth=-1)
        with pytest.raises(ValueError, match="boolean"):
            entry(parent="a1b2c3d4e5", depth=True)

    def test_parent_and_depth_are_paired_provenance(self):
        with pytest.raises(ValueError, match="parent and depth"):
            entry(parent="a1b2c3d4e5")
        with pytest.raises(ValueError, match="parent and depth"):
            entry(depth=2)
        child = entry(parent="a1b2c3d4e5", depth=2)
        assert (child.parent, child.depth) == ("a1b2c3d4e5", 2)

    def test_a_spawned_entrys_depth_starts_at_one(self):
        # One rule with WorkUnit, stated once: an entry pairing a parent
        # with depth 0 was legal here and refused there, so a whole class
        # of ledger states could never convert to work units.
        with pytest.raises(ValueError, match="starts at 1"):
            entry(parent="a1b2c3d4e5", depth=0)
        with pytest.raises(ValueError, match="parent and depth"):
            WorkUnit(
                hash="73bd784849",
                url="https://example.test",
                kind=Kind.WEB,
                item="2026-08-19-example-55ad7b",
                depth=0,
                parent="a1b2c3d4e5",
            )


class TestLedgerEntryReason:
    @pytest.mark.parametrize("status", [Status.MANUAL, Status.SKIPPED])
    def test_reason_is_required_on_deliberate_parking(self, status):
        with pytest.raises(ValueError, match="stated reason"):
            entry(status=status)
        with pytest.raises(ValueError, match="stated reason"):
            entry(status=status, reason="")
        assert entry(status=status, reason="thin-extraction").reason == "thin-extraction"

    def test_reason_is_optional_on_waiting_blocked_dead(self):
        waiting = entry(status=Status.WAITING, needs=Need.TRANSCRIBE)
        assert waiting.reason is None
        assert (
            entry(status=Status.WAITING, needs=Need.TRANSCRIBE, reason="model missing").reason
            == "model missing"
        )
        assert entry(status=Status.BLOCKED, attempts=1, reason="403 challenge").reason
        assert entry(status=Status.DEAD).reason is None
        assert entry(status=Status.DEAD, reason="NXDOMAIN").reason == "NXDOMAIN"

    @pytest.mark.parametrize("status", [Status.DONE, Status.QUEUED])
    def test_reason_is_forbidden_on_done_and_queued(self, status):
        with pytest.raises(ValueError, match="forbidden"):
            entry(status=status, reason="unneeded")

    def test_reason_and_error_never_coexist(self):
        with pytest.raises(ValueError, match="forbidden"):
            entry(status=Status.ERROR, error="scrubbed: boom", reason="also a reason")

    def test_reason_is_single_line_for_every_writer(self):
        # Reasons render on single-line report surfaces; a newline written
        # here would brick the item-status view at read time instead.
        with pytest.raises(ValueError, match="single line"):
            entry(status=Status.MANUAL, reason="paywalled;\nrescue by hand")


class TestMigrationReport:
    def test_defaults(self):
        report = MigrationReport()
        assert (report.actions, report.skipped, report.anomalies) == ([], [], [])

    def test_skipped_carries_what_and_why(self):
        skip = Skipped(what="corpus/x.md", why="frontmatter does not parse")
        report = MigrationReport(actions=["renamed 2 files"], skipped=[skip])
        assert report.skipped[0].why == "frontmatter does not parse"


class TestInstance:
    def test_derived_paths(self, tmp_path: Path):
        inst = Instance(root=tmp_path)
        assert inst.corpus_dir == tmp_path / "corpus"
        assert inst.state_dir == tmp_path / "state"
        assert inst.enrichment_dir == tmp_path / "enrichment"
        assert inst.cache_dir == tmp_path / "cache"
        assert inst.ledger_path == tmp_path / "state" / "enrichment-ledger.jsonl"
        assert inst.config_path == tmp_path / "state" / "config.json"
        assert inst.passes_path == tmp_path / "state" / "passes.jsonl"
        assert inst.digests_dir == tmp_path / "state" / "digests"

    def test_fixture_builds_the_skeleton(self, instance: Instance):
        assert instance.corpus_dir.is_dir()
        assert instance.state_dir.is_dir()
        assert instance.enrichment_dir.is_dir()
        assert instance.cache_dir.is_dir()


class TestConfig:
    def test_missing_file_yields_defaults(self, tmp_path: Path):
        config = Config.load(tmp_path / "config.json")
        assert config.media_fetch is MediaFetch.LEAD
        assert config.transcribe_model == "medium"
        assert config.transcribe_api_model is None
        assert config.instagram_base_url is None
        assert config.report_issues is True
        assert config.providers == {}
        assert config.internal_domains == []
        assert config.noise_prefixes == []

    def test_full_file_parses(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps(
                {
                    "media_fetch": "none",
                    "transcribe_model": "small",
                    "transcribe_api_model": "whisper-large-v3",
                    "instagram_base_url": "https://mirror.test",
                    "report_issues": False,
                    "providers": {"transcribe": ["whisper-api", "whisper-local"]},
                    "internal_domains": ["example.internal"],
                    "noise_prefixes": ["Updated room membership"],
                }
            )
        )
        config = Config.load(path)
        assert config.media_fetch is MediaFetch.NONE
        assert config.transcribe_model == "small"
        assert config.transcribe_api_model == "whisper-large-v3"
        assert config.instagram_base_url == "https://mirror.test"
        assert config.report_issues is False
        assert config.providers == {"transcribe": ["whisper-api", "whisper-local"]}
        assert config.internal_domains == ["example.internal"]
        assert config.noise_prefixes == ["Updated room membership"]

    def test_deleted_name_map_key_is_rejected(self, tmp_path: Path):
        # name_map was deleted (never applied by any engine version);
        # migration 1 drops it — a survivor must fail loudly, not lurk.
        path = tmp_path / "config.json"
        path.write_text('{"name_map": {}}')
        with pytest.raises(ValueError, match="name_map"):
            Config.load(path)

    def test_invalid_json_is_loud(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text("{not json")
        with pytest.raises(ValueError, match="invalid JSON"):
            Config.load(path)

    def test_non_object_is_loud(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text("[1, 2]")
        with pytest.raises(ValueError, match="JSON object"):
            Config.load(path)

    def test_unknown_key_is_loud(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"media_fech": "lead"}))
        with pytest.raises(ValueError, match="media_fech"):
            Config.load(path)

    @pytest.mark.parametrize("key", ["transcribe_base_url", "instagram_base_url"])
    def test_a_base_url_the_transport_cannot_fetch_is_loud(self, tmp_path: Path, key: str):
        # Reached at fetch time instead, a schemeless URL crashes the unit
        # as an engine error — retried only on the next release, and filed
        # upstream as an engine bug when issue reporting is on.
        path = tmp_path / "config.json"
        path.write_text(json.dumps({key: "uuinstagram.com"}))
        with pytest.raises(ValueError, match="http"):
            Config.load(path)

    @pytest.mark.parametrize("key", ["transcribe_base_url", "instagram_base_url"])
    def test_a_fetchable_base_url_loads(self, tmp_path: Path, key: str):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({key: "https://proxy.example"}))
        assert getattr(Config.load(path), key) == "https://proxy.example"

    def test_bad_media_fetch_is_loud(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"media_fetch": "all"}))
        with pytest.raises(ValueError, match="media_fetch"):
            Config.load(path)

    def test_bad_report_issues_type_is_loud(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"report_issues": "yes"}))
        with pytest.raises(ValueError, match="report_issues"):
            Config.load(path)

    def test_bad_providers_shape_is_loud(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"providers": {"transcribe": "whisper-local"}}))
        with pytest.raises(ValueError, match="providers"):
            Config.load(path)

    def test_typoed_provider_capability_is_loud(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"providers": {"transcibe": ["whisper-local"]}}))
        with pytest.raises(ValueError, match="transcibe"):
            Config.load(path)


class TestVersions:
    def test_tuple_compare_not_string_compare(self):
        # The pin: "0.10.0" > "0.9.1" is False as strings.
        assert "0.10.0" < "0.9.1"  # noqa: PLR0133 — the string-compare trap, demonstrated
        assert version_newer("0.10.0", "0.9.1") is True

    def test_equal_versions_are_not_newer(self):
        assert version_newer("0.2.1", "0.2.1") is False

    def test_leading_v_accepted(self):
        assert parse_version("v0.3.0") == (0, 3, 0)
        assert version_newer("v0.3.0", "0.2.9") is True

    @pytest.mark.parametrize("bad", ["", "abc", "1.2.x", "1..2"])
    def test_unparseable_versions_are_loud(self, bad):
        with pytest.raises(ValueError, match="version"):
            parse_version(bad)
