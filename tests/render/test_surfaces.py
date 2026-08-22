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
            "item-status",
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

    def test_incomplete_items_state_the_shape_of_what_is_missing(self):
        payload = {
            **ENRICH_PAYLOAD,
            "incomplete": [
                {
                    "item": "2026-08-19-example-55ad7b",
                    "landed": 3,
                    "total": 4,
                    "outstanding": [{"status": "waiting", "needs": "transcribe", "count": 1}],
                }
            ],
        }
        out = render("enrich-report", payload)
        assert "incomplete — 1 item still raw until every unit lands" in out
        assert "3 of 4 units landed — 1 waiting on transcription" in out
        assert_no_trailing_whitespace(out)

    def test_several_outstanding_shapes_read_as_one_line(self):
        payload = {
            "counts": {},
            "items": [],
            "parked": [],
            "incomplete": [
                {
                    "item": "2026-08-19-example-55ad7b",
                    "landed": 0,
                    "total": 3,
                    "outstanding": [
                        {"status": "blocked", "count": 2},
                        {"status": "manual", "count": 1},
                    ],
                }
            ],
        }
        out = render("enrich-report", payload)
        assert "0 of 3 units landed — 2 blocked, 1 needing a decision" in out

    def test_absent_incomplete_says_nothing(self):
        assert "incomplete" not in render("enrich-report", {**ENRICH_PAYLOAD, "incomplete": []})

    def test_a_landed_status_in_outstanding_is_loud(self):
        payload = {
            "counts": {},
            "items": [],
            "parked": [],
            "incomplete": [
                {
                    "item": "i",
                    "landed": 0,
                    "total": 1,
                    "outstanding": [{"status": "done", "count": 1}],
                }
            ],
        }
        with pytest.raises(PayloadError, match="outstanding status"):
            render("enrich-report", payload)

    def test_landed_beyond_total_is_loud(self):
        payload = {
            "counts": {},
            "items": [],
            "parked": [],
            "incomplete": [
                {
                    "item": "i",
                    "landed": 5,
                    "total": 1,
                    "outstanding": [{"status": "queued", "count": 1}],
                }
            ],
        }
        with pytest.raises(PayloadError, match="exceeds total"):
            render("enrich-report", payload)

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
                "machinery_changes": 6,
            },
        )
        assert "sync — pin bumped v0.2.1 → v0.3.0" in out
        assert "migration 1 — renames + status vocabulary" in out
        assert "rewrote kinds in 214 items" in out
        assert "repair with judgment" in out
        assert "corpus/2025/odd.md — frontmatter does not parse" in out
        assert "REVIEW REQUIRED" in out
        assert "machinery changes: 6" in out
        assert_no_trailing_whitespace(out)

    def test_steady_state_sync(self):
        out = render("sync-report", {"pin": "v0.3.0", "migrations": [], "machinery_changes": 6})
        assert "sync — engine pinned at v0.3.0" in out
        assert "migrations applied — none (state already current)" in out

    def test_unpinned_pre_first_release(self):
        # Before any release tag exists, sync runs without pinning.
        out = render(
            "sync-report",
            {
                "migrations": [],
                "machinery_changes": 3,
                "notes": ["no release tags on the remote yet — running unpinned"],
            },
        )
        assert "sync — engine unpinned" in out
        assert "no release tags on the remote yet" in out

    def test_previous_without_pin_is_loud(self):
        with pytest.raises(PayloadError, match="previous requires pin"):
            render(
                "sync-report",
                {"previous": "v0.1.0", "migrations": [], "machinery_changes": 0},
            )

    def test_missing_machinery_changes_is_loud(self):
        with pytest.raises(PayloadError, match="machinery_changes"):
            render("sync-report", {"pin": "v0.3.0", "migrations": []})

    def test_migration_without_intent_is_loud(self):
        with pytest.raises(PayloadError, match="intent"):
            render(
                "sync-report",
                {"pin": "v1", "migrations": [{"number": 1}], "machinery_changes": 0},
            )


HEALTH_PAYLOAD = {
    "summary": {"corpus_items": 120, "pages": 14, "cited": 118},
    "broken_links": [{"page": "pour-over", "target": "brewing"}],
    "reserved_links": 3,
    "bad_citations": [{"page": "pour-over", "id": "2026-01-01-gone-aaaaaa"}],
    "shortid_citations": [{"page": "index", "token": "a1b2c3"}],
    "orphans": ["2026-01-02-orphan-bbbbbb"],
    "unindexed": ["new-page"],
    "ghost_index": ["deleted-page"],
    "stale_pages": [{"page": "pour-over", "newer": 2}],
    "count_drift": [{"page": "pour-over", "recorded": "3", "actual": 5}],
    "restated": [
        {"page": "pour-over", "first": "The dose is 60g/L.", "second": "Use a 60g/L dose."}
    ],
    "ledger_entries": 42,
    "waiting": {"transcribe": 3},
    "cognitive": [
        {"item": "2026-01-03-scan-cccccc", "url": "file:media/cccccc/scan.pdf", "need": "ocr"}
    ],
    "stale_passes": [{"item": "2026-01-04-old-dddddd", "rules": 0}],
    "digest_orphans": ["2026-01-05-undigested-eeeeee"],
    "reconciled": ["pour-over: items: 3 -> 5"],
    "notes": ["one free note"],
}


class TestItemStatus:
    def test_full_item_view(self):
        out = render(
            "item-status",
            {
                "item": "2026-08-19-example-55ad7b",
                "units": [
                    {"url": "https://example.test/post", "status": "done",
                     "path": "enrichment/2026-08-19-example-55ad7b/web-73bd78.md"},
                    {"url": "https://youtube.test/w", "status": "waiting",
                     "needs": "transcribe", "via": "harvest", "depth": 1},
                    {"url": "https://paywalled.test/a", "status": "manual",
                     "reason": "thin-extraction"},
                ],
            },
        )
        assert out.startswith("item 2026-08-19-example-55ad7b — 3 units")
        assert "done" in out
        assert "→ enrichment/2026-08-19-example-55ad7b/web-73bd78.md" in out
        assert "needs transcribe" in out
        assert "(via harvest, depth" in out  # provenance may wrap at width
        assert "manual" in out
        assert "thin-extraction" in out

    def test_no_units_reads_honestly(self):
        out = render("item-status", {"item": "2026-08-19-note-aaaaaa", "units": []})
        assert "no ledger work units" in out
        assert "no-source capture" in out

    def test_units_are_required(self):
        with pytest.raises(PayloadError, match="units"):
            render("item-status", {"item": "2026-08-19-note-aaaaaa"})

    def test_bad_status_is_loud(self):
        with pytest.raises(PayloadError, match="finished"):
            render(
                "item-status",
                {"item": "x-a", "units": [{"url": "https://a.test", "status": "finished"}]},
            )

    def test_bad_needs_is_loud(self):
        with pytest.raises(PayloadError, match="transcode"):
            render(
                "item-status",
                {"item": "x-a",
                 "units": [{"url": "https://a.test", "status": "waiting",
                            "needs": "transcode"}]},
            )

    def test_unknown_unit_key_is_loud(self):
        with pytest.raises(PayloadError, match="parent"):
            render(
                "item-status",
                {"item": "x-a",
                 "units": [{"url": "https://a.test", "status": "done",
                            "parent": "abc"}]},
            )


class TestHealthReport:
    def test_full_report(self):
        out = render("health-report", HEALTH_PAYLOAD)
        assert out.startswith("health check — 120 corpus items · 14 pages · 118 cited")
        assert "broken wikilinks — 1" in out
        assert "pour-over -> [[brewing]]" in out
        assert "reserved/unbuilt links: 3" in out
        assert "bad citations (id not in corpus) — 1" in out
        assert "shortid-shaped citations (citations are full item ids, always) — 1" in out
        assert "index -> `a1b2c3`" in out
        assert "orphan items (uncited, unledgered) — 1" in out
        assert "pages missing from index — 1" in out
        assert "ghost index entries — 1" in out
        assert "stale pages (members newer than the page) — 1" in out
        assert "pour-over: 2 newer item(s)" in out
        assert "item-count drift (frontmatter items: vs members) — 1" in out
        assert "pour-over: items: 3, members 5" in out
        assert "possible restated facts (same page — merge?) — 1" in out
        assert "ledger — 42 entries, schema valid" in out
        assert "waiting cohorts: transcribe 3" in out
        assert "cognitive jobs (the session completes these with eyes) — 1" in out
        assert "harvest passes under old rules (re-judge) — 1" in out
        assert "enrichment newer than digest" in out
        assert "reconciled by --write:" in out
        assert "notes:" in out

    def test_clean_instance_reads_clean(self):
        out = render("health-report", {"summary": {"corpus_items": 0, "pages": 0, "cited": 0}})
        assert "broken wikilinks — none" in out
        assert "waiting cohorts: none" in out
        assert "reconciled" not in out

    def test_ledger_error_renders_loud(self):
        out = render(
            "health-report",
            {
                "summary": {"corpus_items": 1, "pages": 0, "cited": 0},
                "ledger_error": "state/enrichment-ledger.jsonl:3: unknown field(s) ['note']",
            },
        )
        assert "LEDGER SCHEMA FAILURE" in out
        assert "unknown field(s)" in out

    def test_taxonomy_error_renders_loud(self):
        out = render(
            "health-report",
            {
                "summary": {"corpus_items": 1, "pages": 0, "cited": 0},
                "taxonomy_error": "taxonomy.json: expected a JSON object, got list",
            },
        )
        assert "TAXONOMY FAILURE" in out
        assert "expected a JSON object" in out

    def test_summary_is_required(self):
        with pytest.raises(PayloadError, match="summary"):
            render("health-report", {})

    def test_unknown_key_is_loud(self):
        with pytest.raises(PayloadError, match="broken_wikilinks"):
            render(
                "health-report",
                {"summary": {"corpus_items": 0, "pages": 0, "cited": 0},
                 "broken_wikilinks": []},
            )

    def test_bad_waiting_need_is_loud(self):
        with pytest.raises(PayloadError, match="transcode"):
            render(
                "health-report",
                {"summary": {"corpus_items": 0, "pages": 0, "cited": 0},
                 "waiting": {"transcode": 1}},
            )


class TestIngestReceipt:
    def test_full_receipt(self):
        out = render(
            "ingest-receipt",
            {
                "item": "2026-08-18-rag-eval-harness-a1b2c3",
                "title": "RAG eval harness",
                "fetched": 3,
                "parked": 1,
                "signal": "high",
                "topics": ["agent-architecture"],
                "pages": ["agent-architecture", "evals"],
                "notes": ["thread walk-up found the repo link"],
            },
        )
        assert out.startswith("ingested 2026-08-18-rag-eval-harness-a1b2c3 — RAG eval harness")
        assert "fetched 3 · parked 1" in out
        assert "signal high · topics: agent-architecture" in out
        assert "pages: agent-architecture, evals" in out
        assert "- thread walk-up found the repo link" in out

    def test_minimal_receipt_is_one_line(self):
        out = render("ingest-receipt", {"item": "2026-08-18-note-a1b2c3"})
        assert out == "ingested 2026-08-18-note-a1b2c3\n"

    def test_item_is_required(self):
        with pytest.raises(PayloadError, match="item"):
            render("ingest-receipt", {"title": "no item"})

    def test_bad_signal_is_loud(self):
        with pytest.raises(PayloadError, match="signal"):
            render(
                "ingest-receipt",
                {"item": "2026-08-18-note-a1b2c3", "signal": "shrug"},
            )
