"""Tests for render/surfaces.py: named surfaces, loud payload validation."""

import pytest

from dex_engine.render.surfaces import SURFACES, PayloadError, render

ENRICH_PAYLOAD = {
    "counts": {"done": 4, "blocked": 1, "waiting": 1},
    "items": [
        {"id": "2026-08-19-example-55ad7b", "reason": "fresh"},
        {"id": "2026-08-18-older-11aa22", "reason": "rerun"},
    ],
    "parked": [
        {
            "item": "2026-08-19-example-55ad7b",
            "url": "https://example.test/page",
            "status": "blocked",
            "reason": "403 challenge, attempt 2 of 5",
        }
    ],
    "cognitive": [
        {"item": "2026-08-19-scan-77cc88", "url": "file:media/x/scan.png", "need": "ocr"}
    ],
    "issues_filed": 2,
    "notes": ["first transcription downloads the whisper model once per machine"],
}


def assert_no_trailing_whitespace(text: str) -> None:
    for line in text.splitlines():
        assert line == line.rstrip(), f"trailing whitespace in {line!r}"


class TestRenderDispatch:
    def test_unknown_surface_is_loud_and_names_the_known_ones(self):
        with pytest.raises(PayloadError, match="enrich-report"):
            render("run-report", {})

    def test_every_registered_surface_is_reachable(self):
        assert set(SURFACES) == {
            "enrich-report",
            "status",
            "capability-report",
            "sync-report",
            "ingest-receipt",
            "health-report",
        }


class TestEnrichReport:
    def test_full_report(self):
        out = render("enrich-report", ENRICH_PAYLOAD)
        assert "enrich run — 6 units processed" in out
        assert "done" in out
        assert "cognitive work — 2 items with new or changed content:" in out
        assert "2026-08-19-example-55ad7b" in out
        assert "parked — 1 entry survives this session" in out
        assert "403 challenge, attempt 2 of 5" in out
        assert "cognitive jobs — the session completes these with eyes:" in out
        assert "reported upstream: 2 issues" in out
        assert "whisper model" in out
        assert_no_trailing_whitespace(out)

    def test_empty_run_states_the_absences(self):
        out = render("enrich-report", {"counts": {}, "items": [], "parked": []})
        assert "0 units processed" in out
        assert "cognitive work — none (nothing new or changed)" in out
        assert "parked — none" in out
        assert "reported upstream" not in out

    def test_zero_issues_filed_omits_the_line(self):
        payload = {**ENRICH_PAYLOAD, "issues_filed": 0}
        assert "reported upstream" not in render("enrich-report", payload)

    def test_missing_required_key_is_loud(self):
        with pytest.raises(PayloadError, match="parked"):
            render("enrich-report", {"counts": {}, "items": []})

    def test_unknown_key_is_loud(self):
        payload = {**ENRICH_PAYLOAD, "extra": 1}
        with pytest.raises(PayloadError, match="extra"):
            render("enrich-report", payload)

    def test_unknown_status_in_counts_is_loud(self):
        with pytest.raises(PayloadError, match="nocaptions"):
            render("enrich-report", {"counts": {"nocaptions": 1}, "items": [], "parked": []})

    def test_unparked_status_in_parked_is_loud(self):
        parked = [{"item": "i", "url": "u", "status": "done", "reason": "r"}]
        with pytest.raises(PayloadError, match="parked"):
            render("enrich-report", {"counts": {}, "items": [], "parked": parked})

    def test_bad_need_in_cognitive_is_loud(self):
        cognitive = [{"item": "i", "url": "u", "need": "summarize"}]
        with pytest.raises(PayloadError, match="summarize"):
            render(
                "enrich-report",
                {"counts": {}, "items": [], "parked": [], "cognitive": cognitive},
            )

    def test_mistyped_item_entry_is_loud(self):
        with pytest.raises(PayloadError, match=r"items\[0\]"):
            render("enrich-report", {"counts": {}, "items": [{"id": "x"}], "parked": []})

    def test_multiline_string_field_is_a_payload_error_not_a_kernel_error(self):
        items = [{"id": "x", "reason": "fresh\nand multiline"}]
        with pytest.raises(PayloadError, match="single-line"):
            render("enrich-report", {"counts": {}, "items": items, "parked": []})

    def test_multiline_note_is_a_payload_error(self):
        notes = ["a note\nwith a newline"]
        with pytest.raises(PayloadError, match=r"notes\[0\]"):
            render("enrich-report", {"counts": {}, "items": [], "parked": [], "notes": notes})


class TestStatusSurface:
    def test_summary_with_waiting_and_orphans(self):
        out = render(
            "status",
            {
                "counts": {"done": 40, "waiting": 3, "manual": 1},
                "waiting": {"transcribe": 3},
                "orphans": ["2026-08-19-example-55ad7b"],
            },
        )
        assert "ledger — 44 entries" in out
        assert "waiting on capabilities:" in out
        assert "transcribe" in out
        assert "interrupted-session backstop" in out
        assert "└─ 2026-08-19-example-55ad7b" in out
        assert_no_trailing_whitespace(out)

    def test_counts_only(self):
        out = render("status", {"counts": {"done": 1}})
        assert "ledger — 1 entry" in out
        assert "waiting on capabilities" not in out

    def test_bad_waiting_need_is_loud(self):
        with pytest.raises(PayloadError, match="summarize"):
            render("status", {"counts": {}, "waiting": {"summarize": 1}})

    def test_empty_orphan_id_is_a_payload_error_not_a_kernel_error(self):
        with pytest.raises(PayloadError, match=r"orphans\[0\]"):
            render("status", {"counts": {}, "orphans": [""]})

    def test_negative_count_is_loud(self):
        with pytest.raises(PayloadError, match="counts"):
            render("status", {"counts": {"done": -1}})


class TestCapabilityReport:
    def test_matches_the_design_example_shape(self):
        out = render(
            "capability-report",
            {
                "capabilities": [
                    {
                        "name": "transcribe",
                        "providers": [
                            {"name": "whisper-local", "state": "active"},
                            {
                                "name": "whisper-api",
                                "state": "available",
                                "note": "set OPENAI_API_KEY",
                            },
                        ],
                    },
                    {"name": "ocr", "providers": []},
                ]
            },
        )
        assert "transcribe" in out
        assert "whisper-local (active) · whisper-api available — set" in out
        assert "no providers" in out
        assert_no_trailing_whitespace(out)

    def test_unknown_capability_is_loud(self):
        with pytest.raises(PayloadError, match="summarize"):
            render("capability-report", {"capabilities": [{"name": "summarize", "providers": []}]})

    def test_bad_provider_state_is_loud(self):
        capability = {"name": "extract", "providers": [{"name": "anydoc", "state": "installed"}]}
        with pytest.raises(PayloadError, match="installed"):
            render("capability-report", {"capabilities": [capability]})

    def test_mistyped_provider_note_is_loud(self):
        capability = {
            "name": "extract",
            "providers": [{"name": "anydoc", "state": "available", "note": 42}],
        }
        with pytest.raises(PayloadError, match="note"):
            render("capability-report", {"capabilities": [capability]})

    def test_no_capabilities_is_loud(self):
        with pytest.raises(PayloadError, match="capabilities"):
            render("capability-report", {"capabilities": []})


class TestSyncReport:
    def test_pin_bump_with_migrations(self):
        out = render(
            "sync-report",
            {
                "pin": "v0.3.0",
                "previous": "v0.2.1",
                "migrations": [
                    {
                        "number": 1,
                        "intent": "renames + status vocabulary",
                        "actions": ["rewrote kinds in 214 items"],
                        "skipped": [
                            {"what": "corpus/2025/odd.md", "why": "frontmatter does not parse"}
                        ],
                        "anomalies": ["ledger line 40 had both error and path"],
                    }
                ],
                "skills_synced": 6,
            },
        )
        assert "sync — pin bumped v0.2.1 → v0.3.0" in out
        assert "migration 1 — renames + status vocabulary" in out
        assert "rewrote kinds in 214 items" in out
        assert "repair with judgment" in out
        assert "corpus/2025/odd.md — frontmatter does not parse" in out
        assert "REVIEW REQUIRED" in out
        assert "skills synced: 6" in out
        assert_no_trailing_whitespace(out)

    def test_steady_state_sync(self):
        out = render("sync-report", {"pin": "v0.3.0", "migrations": [], "skills_synced": 6})
        assert "sync — engine pinned at v0.3.0" in out
        assert "migrations applied — none (state already current)" in out

    def test_unpinned_pre_first_release(self):
        # §12: before any release tag exists, sync runs without pinning.
        out = render(
            "sync-report",
            {
                "migrations": [],
                "skills_synced": 3,
                "notes": ["no release tags on the remote yet — running unpinned"],
            },
        )
        assert "sync — engine unpinned" in out
        assert "no release tags on the remote yet" in out

    def test_previous_without_pin_is_loud(self):
        with pytest.raises(PayloadError, match="previous requires pin"):
            render(
                "sync-report",
                {"previous": "v0.1.0", "migrations": [], "skills_synced": 0},
            )

    def test_missing_skills_synced_is_loud(self):
        with pytest.raises(PayloadError, match="skills_synced"):
            render("sync-report", {"pin": "v0.3.0", "migrations": []})

    def test_migration_without_intent_is_loud(self):
        with pytest.raises(PayloadError, match="intent"):
            render(
                "sync-report",
                {"pin": "v1", "migrations": [{"number": 1}], "skills_synced": 0},
            )


class TestStubs:
    @pytest.mark.parametrize("surface", ["ingest-receipt", "health-report"])
    def test_stubs_raise_with_a_clear_message(self, surface):
        with pytest.raises(NotImplementedError, match="later phase"):
            render(surface, {})
