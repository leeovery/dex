"""Tests for pipeline/types.py: enums, dataclass validation, config, versions."""

import dataclasses
import datetime
import json
from pathlib import Path

import pytest

from dex_engine.pipeline.types import (
    DRIVER_STATUSES,
    Availability,
    Child,
    Config,
    Format,
    Instance,
    Kind,
    LedgerEntry,
    MediaFetch,
    MigrationReport,
    Need,
    Result,
    Skipped,
    Status,
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

    def test_driver_statuses_exclude_only_queued(self):
        assert Status.QUEUED not in DRIVER_STATUSES
        assert frozenset(Status) - {Status.QUEUED} == DRIVER_STATUSES


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


class TestChild:
    @pytest.mark.parametrize(
        "via",
        ["harvest", "thread", "media", "sniff", "extract-asset", "migration-1", "migration-12"],
    )
    def test_known_via_accepted(self, via):
        assert Child(url="https://example.test", via=via).via == via

    @pytest.mark.parametrize("via", ["", "harvested", "migration-", "migration-0", "Harvest"])
    def test_unknown_via_rejected(self, via):
        with pytest.raises(ValueError, match="via"):
            Child(url="https://example.test", via=via)


class TestResult:
    def test_simple_driver_return_uses_defaults(self):
        result = Result(status=Status.DONE, meta={"title": "t"}, body="text")
        assert result.media == []
        assert result.children == []
        assert result.needs is None

    def test_queued_is_a_birth_state_not_a_driver_outcome(self):
        with pytest.raises(ValueError, match="birth state"):
            Result(status=Status.QUEUED, meta={})

    def test_waiting_requires_needs(self):
        with pytest.raises(ValueError, match="needs"):
            Result(status=Status.WAITING, meta={})

    def test_needs_requires_waiting(self):
        with pytest.raises(ValueError, match="waiting"):
            Result(status=Status.DONE, meta={}, needs=Need.TRANSCRIBE)

    def test_waiting_with_needs_ok(self):
        result = Result(status=Status.WAITING, meta={}, needs=Need.TRANSCRIBE)
        assert result.needs is Need.TRANSCRIBE


class TestResultReason:
    """Result.reason mirrors the ledger's stated-reason contract (§2/§5)."""

    @pytest.mark.parametrize("status", [Status.MANUAL, Status.SKIPPED])
    def test_reason_is_required_on_deliberate_parking(self, status):
        with pytest.raises(ValueError, match="reason"):
            Result(status=status, meta={})
        with pytest.raises(ValueError, match="reason"):
            Result(status=status, meta={}, reason="")
        assert Result(status=status, meta={}, reason="thin-extraction").reason == "thin-extraction"

    def test_reason_is_optional_on_waiting_blocked_dead(self):
        waiting = Result(status=Status.WAITING, meta={}, needs=Need.TRANSCRIBE)
        assert waiting.reason is None
        primed = Result(status=Status.WAITING, meta={}, needs=Need.TRANSCRIBE, reason="no captions")
        assert primed.reason == "no captions"
        assert Result(status=Status.BLOCKED, meta={}, reason="HTTP 403").reason == "HTTP 403"
        assert Result(status=Status.DEAD, meta={}).reason is None
        assert Result(status=Status.DEAD, meta={}, reason="HTTP 404").reason == "HTTP 404"

    def test_reason_is_forbidden_on_done_and_error(self):
        with pytest.raises(ValueError, match="forbidden"):
            Result(status=Status.DONE, meta={}, body="text", reason="unneeded")
        with pytest.raises(ValueError, match="forbidden"):
            Result(status=Status.ERROR, meta={}, reason="unneeded")


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

    def test_needs_is_waiting_only(self):
        with pytest.raises(ValueError, match="waiting-only"):
            entry(status=Status.QUEUED, needs=Need.OCR)

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

    def test_via_is_validated(self):
        assert entry(via="migration-2", rerun=True).via == "migration-2"
        with pytest.raises(ValueError, match="via"):
            entry(via="freehand")

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
        with pytest.raises(ValueError, match="both or neither"):
            entry(parent="a1b2c3d4e5")
        with pytest.raises(ValueError, match="both or neither"):
            entry(depth=2)
        child = entry(parent="a1b2c3d4e5", depth=2)
        assert (child.parent, child.depth) == ("a1b2c3d4e5", 2)


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
        assert config.report_issues is True
        assert config.providers == {}
        assert config.name_map == {}
        assert config.internal_domains == []
        assert config.noise_prefixes == []

    def test_full_file_parses(self, tmp_path: Path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps(
                {
                    "media_fetch": "none",
                    "transcribe_model": "small",
                    "report_issues": False,
                    "providers": {"transcribe": ["whisper-api", "whisper-local"]},
                    "name_map": {"lee.overy": "Lee"},
                    "internal_domains": ["example.internal"],
                    "noise_prefixes": ["Updated room membership"],
                }
            )
        )
        config = Config.load(path)
        assert config.media_fetch is MediaFetch.NONE
        assert config.transcribe_model == "small"
        assert config.report_issues is False
        assert config.providers == {"transcribe": ["whisper-api", "whisper-local"]}
        assert config.name_map == {"lee.overy": "Lee"}
        assert config.internal_domains == ["example.internal"]
        assert config.noise_prefixes == ["Updated room membership"]

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
        # The §5 pin: "0.10.0" > "0.9.1" is False as strings.
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
