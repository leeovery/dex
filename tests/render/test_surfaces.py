"""Tests for render/surfaces.py: named surfaces, loud payload validation."""

import re

import pytest

from dex_engine.render.surfaces import SURFACES, PayloadError, render

# Shaped like real instance data, where ids run long and URLs run longer
# still. Both are identity, and no surface may shorten either.
LONG_ID = "2026-08-19-laravel-read-through-filesystem-migrations-with-real-drivers-14bf49"
LONG_URL = "https://example.test/watch?v=abcdefghijk&" + "&".join(
    f"utm_source=partner-{n}&utm_campaign=very-long-campaign-name-{n}" for n in range(9)
)

ENRICH_PAYLOAD = {
    "counts": {"done": 4, "blocked": 1, "waiting": 1},
    "items": [
        {"id": "2026-08-19-example-55ad7b", "reason": "3 new enrichment files, 1 rewritten"},
        {"id": "2026-08-18-older-11aa22", "reason": "1 new enrichment file"},
    ],
    "parked": [
        {
            "item": "2026-08-19-example-55ad7b",
            "url": "https://example.test/page",
            "status": "blocked",
            "reason": "403 challenge from the edge",
            "attempts": 2,
            "attempt_cap": 5,
        },
        {
            "item": "2026-08-18-google-developer-documentation-style-guide-87f21f",
            "url": "https://example.test/style-guide",
            "status": "manual",
            "reason": "thin extraction — fetch it yourself or skip it with a reason",
        },
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


def assert_whole(text: str, *values: str) -> None:
    """Every value present verbatim, on one line, with no ellipsis anywhere."""
    assert "…" not in text.replace("… and ", ""), f"an ellipsis reached the output:\n{text}"
    for value in values:
        assert any(value in line for line in text.splitlines()), (
            f"{value!r} is not present whole on any single line of:\n{text}"
        )


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
        assert out.startswith("## Enrich run — 6 units processed\n")
        assert "**done** 4 · **waiting** 1 · **blocked** 1" in out
        assert "### Needs writing up — 2 items have new material" in out
        assert "- **2026-08-19-example-55ad7b**" in out
        assert "  ↳ 3 new enrichment files, 1 rewritten" in out
        assert "### Read these yourself — 1 job the engine cannot do" in out
        assert "- **2026-08-19-scan-77cc88** · `ocr`" in out
        assert "**Reported upstream** — 2 issues filed" in out
        assert "### Notes — 1 note" in out
        assert "- first transcription downloads the whisper model once per machine" in out
        assert_no_trailing_whitespace(out)

    def test_unchanged_units_are_accounted_for_but_not_queued_for_writing(self):
        out = render(
            "enrich-report",
            {
                "counts": {"done": 2},
                "items": [],
                "parked": [],
                "unchanged": [{"id": "2026-08-19-example-55ad7b", "count": 2}],
            },
        )
        assert "### Already stored — 2 units re-fetched to what was on disk, across 1 item" in out
        assert "- **2026-08-19-example-55ad7b**" in out
        assert "  ↳ 2 units unchanged" in out
        assert "Needs writing up" not in out
        # The whole point: a run that processed units never claims otherwise.
        assert "Nothing to report from this run" not in out
        assert_no_trailing_whitespace(out)

    def test_a_run_with_genuinely_nothing_still_says_so(self):
        out = render("enrich-report", {"counts": {}, "items": [], "parked": []})
        assert "Nothing to report from this run" in out

    def test_parked_splits_by_who_owns_the_next_action(self):
        out = render("enrich-report", ENRICH_PAYLOAD)
        assert "### Needs you — 1 entry only you can move" in out
        assert (
            "- **2026-08-18-google-developer-documentation-style-guide-87f21f** · `manual`"
        ) in out
        assert "  ↳ thin extraction — fetch it yourself or skip it with a reason" in out
        assert "### Waiting on the engine — 1 entry it retries by itself" in out
        assert "- **2026-08-19-example-55ad7b** · `blocked` · attempt 2 of 5" in out

    def test_the_engine_retries_however_many_entries_it_holds(self):
        # "it retry by itself": the clause's subject is the engine, which is
        # one of it whether it is holding one entry or twenty.
        parked = [
            {"item": "a", "url": "https://a.test", "status": "error", "reason": "reset"},
            {"item": "b", "url": "https://b.test", "status": "error", "reason": "reset"},
        ]
        out = render("enrich-report", {"counts": {}, "items": [], "parked": parked})
        assert "### Waiting on the engine — 2 entries it retries by itself" in out

    def test_the_manual_entry_never_lands_in_the_engines_section(self):
        out = render("enrich-report", ENRICH_PAYLOAD)
        mine = out.split("### Waiting on the engine")[0]
        assert "style-guide-87f21f" in mine
        assert "style-guide-87f21f" not in out.split("### Waiting on the engine")[1]

    def test_waiting_and_error_say_what_the_engine_will_do(self):
        payload = {
            "counts": {},
            "items": [],
            "parked": [
                {
                    "item": "a",
                    "url": "https://a.test",
                    "status": "waiting",
                    "reason": "waiting on transcribe",
                },
                {
                    "item": "b",
                    "url": "https://b.test",
                    "status": "error",
                    "reason": "connection reset",
                },
            ],
        }
        out = render("enrich-report", payload)
        assert "- **a** · `waiting` · retries when a provider appears" in out
        assert "- **b** · `error` · retries on the next engine release" in out

    def test_a_drainable_waiting_row_names_the_active_provider_not_a_missing_one(self):
        # The field defect: a unit waiting on transcribe captioned "retries
        # when a provider appears" while the same output listed whisper as
        # active — sending the reader hunting for a provider that was there.
        parked = [
            {
                "item": "a",
                "url": "https://a.test",
                "status": "waiting",
                "reason": "waiting on transcribe",
                "drainable": True,
            }
        ]
        out = render("status", {"counts": {"waiting": 1}, "parked": parked})
        assert "a provider is active — drains on the next run" in out
        assert "retries when a provider appears" not in out

    def test_a_drainable_value_other_than_true_is_loud(self):
        parked = [{"item": "i", "url": "u", "status": "waiting", "reason": "r", "drainable": 1}]
        with pytest.raises(PayloadError, match="drainable"):
            render("status", {"counts": {}, "parked": parked})

    def test_a_cognitive_waiting_row_is_the_readers_work_not_the_engines(self):
        # The field defect's other half: a unit waiting on the cognitive
        # floor captioned "retries when a provider appears" and filed under
        # the engine's retry list, while the same output listed the floor
        # as the capability's active provider. Nothing will ever appear.
        parked = [
            {
                "item": "a",
                "url": "https://a.test",
                "status": "waiting",
                "reason": "no mechanical OCR provider — the session reads with eyes",
                "cognitive": True,
            }
        ]
        out = render("status", {"counts": {"waiting": 1}, "parked": parked})
        assert "Needs you — 1 entry only you can move" in out
        assert "Waiting on the engine" not in out
        assert "- **a** · `waiting` · no provider will appear — you read this one" in out
        assert "retries when a provider appears" not in out

    def test_a_cognitive_row_takes_no_attempt_framing(self):
        # The engine makes no attempt at a job it hands to the reader, so a
        # stale attempt count on the line must not caption it "attempt n of m".
        parked = [
            {
                "item": "a",
                "url": "https://a.test",
                "status": "waiting",
                "reason": "r",
                "attempts": 2,
                "attempt_cap": 5,
                "cognitive": True,
            }
        ]
        out = render("status", {"counts": {"waiting": 1}, "parked": parked})
        assert "no provider will appear — you read this one" in out
        assert "attempt 2 of 5" not in out

    def test_a_cognitive_value_other_than_true_is_loud(self):
        parked = [{"item": "i", "url": "u", "status": "waiting", "reason": "r", "cognitive": 1}]
        with pytest.raises(PayloadError, match="cognitive"):
            render("status", {"counts": {}, "parked": parked})

    def test_drainable_and_cognitive_together_is_loud(self):
        parked = [
            {
                "item": "i",
                "url": "u",
                "status": "waiting",
                "reason": "r",
                "drainable": True,
                "cognitive": True,
            }
        ]
        with pytest.raises(PayloadError, match="exclusive"):
            render("status", {"counts": {}, "parked": parked})

    def test_attempt_cap_without_attempts_is_loud(self):
        parked = [{"item": "a", "url": "u", "status": "blocked", "reason": "r", "attempt_cap": 5}]
        with pytest.raises(PayloadError, match="attempt_cap"):
            render("enrich-report", {"counts": {}, "items": [], "parked": parked})

    def test_empty_run_says_so_once_instead_of_a_list_of_nones(self):
        out = render("enrich-report", {"counts": {}, "items": [], "parked": []})
        assert "## Enrich run — 0 units processed" in out
        assert "Nothing to report from this run" in out
        assert "###" not in out
        assert "Reported upstream" not in out

    def test_zero_issues_filed_omits_the_line(self):
        payload = {**ENRICH_PAYLOAD, "issues_filed": 0}
        assert "Reported upstream" not in render("enrich-report", payload)

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
        assert "### Not finished — 1 item stays raw until every unit lands" in out
        assert "  ↳ 3 of 4 units landed — 1 waiting on transcription" in out
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
        assert "  ↳ 0 of 3 units landed — 2 blocked, 1 needing a decision" in out

    def test_absent_incomplete_says_nothing(self):
        assert "Not finished" not in render("enrich-report", {**ENRICH_PAYLOAD, "incomplete": []})

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
        assert out.startswith("## Ledger — 44 entries\n")
        assert "**done** 40 · **manual** 1 · **waiting** 3" in out
        assert "### Waiting on a capability — 3 units" in out
        assert "- `transcribe` — 3" in out
        assert ("### Digest these — 1 item whose enrichment is newer than its digest") in out
        assert "- **2026-08-19-example-55ad7b**" in out
        assert_no_trailing_whitespace(out)

    def test_counts_only(self):
        out = render("status", {"counts": {"done": 1}})
        assert "## Ledger — 1 entry" in out
        assert "Waiting on a capability" not in out

    def test_parked_work_is_named_not_merely_counted(self):
        # `**manual** 1` told the reader a count and nothing else — no id,
        # no URL, no reason, on the one surface that holds standing state.
        out = render(
            "status",
            {
                "counts": {"manual": 1, "waiting": 1},
                "parked": [
                    {
                        "item": "2026-05-19-scanned-pdf-87f21f",
                        "url": "https://example.test/scan.pdf",
                        "status": "manual",
                        "reason": "no extractor for this format",
                    },
                    {
                        "item": "2026-06-01-talk-55ad7b",
                        "url": "https://example.test/talk",
                        "status": "waiting",
                        "reason": "waiting on transcribe",
                    },
                ],
            },
        )
        assert "### Needs you — 1 entry only you can move" in out
        assert "- **2026-05-19-scanned-pdf-87f21f** · `manual`" in out
        assert "↳ no extractor for this format" in out
        assert "↳ https://example.test/scan.pdf" in out
        assert "### Waiting on the engine — 1 entry it retries by itself" in out
        assert_no_trailing_whitespace(out)

    def test_no_parked_work_renders_no_section(self):
        out = render("status", {"counts": {"done": 1}, "parked": []})
        assert "Needs you" not in out
        assert "Waiting on the engine" not in out

    def test_a_resting_row_is_yours_not_the_engines(self):
        # The builder marks a row resting when the engine will not act on
        # it (a media unit under `media_fetch: none`); "Waiting on the
        # engine — it retries by itself" would be false for it.
        parked = [
            {
                "item": "2026-08-19-example-55ad7b",
                "url": "https://cdn.example.test/a.png",
                "status": "blocked",
                "reason": "media_fetch is `none` — stays parked until it is turned back on",
                "resting": True,
            }
        ]
        out = render("status", {"counts": {"blocked": 1}, "parked": parked})
        assert "### Needs you — 1 entry only you can move" in out
        assert "Waiting on the engine" not in out
        assert "  ↳ media_fetch is `none` — stays parked until it is turned back on" in out

    def test_a_resting_row_carries_no_retry_note(self):
        # "retries when a provider appears" is the waiting status's note,
        # and it is exactly what a resting unit will not do.
        parked = [
            {
                "item": "2026-08-19-example-55ad7b",
                "url": "https://cdn.example.test/a.png",
                "status": "waiting",
                "reason": "media_fetch is `none` — stays parked until it is turned back on",
                "resting": True,
            }
        ]
        out = render("status", {"counts": {"waiting": 1}, "parked": parked})
        assert "- **2026-08-19-example-55ad7b** · `waiting`" in out
        assert "retries when a provider appears" not in out

    def test_a_resting_value_other_than_true_is_loud(self):
        parked = [{"item": "i", "url": "u", "status": "blocked", "reason": "r", "resting": False}]
        with pytest.raises(PayloadError, match="resting"):
            render("status", {"counts": {}, "parked": parked})

    def test_unparked_status_in_parked_is_loud(self):
        parked = [{"item": "i", "url": "u", "status": "done", "reason": "r"}]
        with pytest.raises(PayloadError, match="parked"):
            render("status", {"counts": {}, "parked": parked})

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
        # Every heading states its scale — a reader who stops at them knows
        # how many capabilities there are and how many providers each has.
        assert out.startswith("## Capabilities — 2 capabilities\n")
        assert "### transcribe — 2 providers" in out
        assert "- **whisper-local** · `active`" in out
        assert "- **whisper-api** · `available`" in out
        assert "  ↳ set OPENAI_API_KEY" in out
        assert "### ocr — 0 providers" in out
        assert "- no providers" in out
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
        assert out.startswith(
            "## Sync — pin bumped v0.2.1 → v0.3.0 — 1 migration · 6 machinery changes\n"
        )
        assert "### Migrations applied — 1" in out
        assert "- **migration 1** — renames + status vocabulary" in out
        assert "    - rewrote kinds in 214 items" in out
        assert "  - **Repair with judgment** — 1 skipped" in out
        assert "    - `corpus/2025/odd.md` — frontmatter does not parse" in out
        assert "  - **REVIEW REQUIRED** — 1 anomaly" in out
        assert "**Machinery changes** — 6" in out
        assert_no_trailing_whitespace(out)

    def test_steady_state_sync(self):
        out = render("sync-report", {"pin": "v0.3.0", "migrations": [], "machinery_changes": 6})
        assert "## Sync — engine pinned at v0.3.0" in out
        assert "No migrations to apply — state was already current." in out

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
        assert "## Sync — engine unpinned" in out
        assert "- no release tags on the remote yet — running unpinned" in out

    def test_a_major_bump_renders_distinctly_and_loudly(self):
        # A major is an owner-visible event, not a row — the heading
        # names it and a loud line follows, unlike any patch bump.
        out = render(
            "sync-report",
            {
                "pin": "v1.0.0",
                "previous": "v0.9.3",
                "major": True,
                "migrations": [],
                "machinery_changes": 0,
            },
        )
        assert out.startswith(
            "## Sync — MAJOR upgrade v0.9.3 → v1.0.0 — 0 migrations · 0 machinery changes\n"
        )
        assert "**MAJOR ENGINE UPGRADE**" in out
        assert "always-migratable commitment" in out
        assert_no_trailing_whitespace(out)

    def test_a_patch_bump_never_reads_as_major(self):
        out = render(
            "sync-report",
            {"pin": "v0.2.2", "previous": "v0.2.1", "migrations": [], "machinery_changes": 0},
        )
        assert "MAJOR" not in out

    def test_major_without_a_pin_transition_is_loud(self):
        with pytest.raises(PayloadError, match="major requires"):
            render(
                "sync-report",
                {"pin": "v1.0.0", "major": True, "migrations": [], "machinery_changes": 0},
            )

    def test_non_boolean_major_is_loud(self):
        with pytest.raises(PayloadError, match="major"):
            render(
                "sync-report",
                {
                    "pin": "v1.0.0",
                    "previous": "v0.9.3",
                    "major": "yes",
                    "migrations": [],
                    "machinery_changes": 0,
                },
            )

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
    "digests": 7,
    "waiting": {"transcribe": 3},
    "cognitive": [
        {"item": "2026-01-03-scan-cccccc", "url": "file:media/cccccc/scan.pdf", "need": "ocr"}
    ],
    "stale_passes": [{"item": "2026-01-04-old-dddddd", "rules": 0}],
    "capped": [
        # lint sends the bound, one canonical label per bound — never the
        # ledger line's stated reason.
        {
            "item": "2026-01-06-deep-ffffff",
            "url": "https://example.test/deep",
            "reason": "depth cap (4)",
        },
        {
            "item": "2026-01-06-deep-ffffff",
            "url": "https://example.test/wide",
            "reason": "url cap (12 per item)",
        },
        {
            "item": "2026-01-07-wide-999999",
            "url": "https://example.test/other",
            "reason": "url cap (12 per item)",
        },
    ],
    "incomplete_threads": [
        {
            "path": "enrichment/2026-01-08-thread-888888/x-abc123.md",
            "why": "parent fetch failed after 3 post(s): fxtwitter says the post is gone",
        }
    ],
    "digest_errors": [{"item": "2026-01-09-broken-777777", "why": "frontmatter missing topics"}],
    "digest_orphans": ["2026-01-05-undigested-eeeeee"],
    "reconciled": ["pour-over: items: 3 -> 5"],
    "notes": ["one free note"],
}

CLEAN_HEALTH = {"summary": {"corpus_items": 0, "pages": 0, "cited": 0}}


class TestItemStatus:
    def test_full_item_view(self):
        out = render(
            "item-status",
            {
                "item": "2026-08-19-example-55ad7b",
                "units": [
                    {
                        "url": "https://example.test/post",
                        "status": "done",
                        "path": "enrichment/2026-08-19-example-55ad7b/web-73bd78.md",
                    },
                    {
                        "url": "https://youtube.test/w",
                        "status": "waiting",
                        "needs": "transcribe",
                        "via": "harvest",
                        "depth": 1,
                    },
                    {
                        "url": "https://paywalled.test/a",
                        "status": "manual",
                        "reason": "thin-extraction",
                    },
                    {
                        "url": "https://cdn.example.test/img.png?sig=abc",
                        "status": "done",
                        "job": "media",
                        "depth": 1,
                    },
                ],
            },
        )
        assert out.startswith("## Item 2026-08-19-example-55ad7b — 4 units\n")
        assert "- **https://example.test/post** · `done`" in out
        assert "  ↳ wrote `enrichment/2026-08-19-example-55ad7b/web-73bd78.md`" in out
        assert (
            "- **https://youtube.test/w** · `waiting` · needs `transcribe` · via harvest, depth 1"
        ) in out
        assert "- **https://paywalled.test/a** · `manual`" in out
        assert "  ↳ thin-extraction" in out
        assert (
            "- **https://cdn.example.test/img.png?sig=abc** · `done` · job media, depth 1"
        ) in out

    def test_no_units_reads_honestly(self):
        out = render("item-status", {"item": "2026-08-19-note-aaaaaa", "units": []})
        assert "## Item 2026-08-19-note-aaaaaa — no work units" in out
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
                {
                    "item": "x-a",
                    "units": [{"url": "https://a.test", "status": "waiting", "needs": "transcode"}],
                },
            )

    def test_unknown_unit_key_is_loud(self):
        with pytest.raises(PayloadError, match="parent"):
            render(
                "item-status",
                {
                    "item": "x-a",
                    "units": [{"url": "https://a.test", "status": "done", "parent": "abc"}],
                },
            )


class TestHealthReport:
    def test_full_report(self):
        out = render("health-report", HEALTH_PAYLOAD)
        assert out.startswith("## Health check — 120 corpus items · 14 pages · 118 cited\n")
        assert "### Wiki — 14 pages" in out
        assert "- broken wikilinks — **1**" in out
        assert "  - **pour-over** → `[[brewing]]`" in out
        assert "- reserved/unbuilt links (informational) — **3**" in out
        assert "- bad citations (id not in corpus) — **1**" in out
        assert "  - **pour-over** → **2026-01-01-gone-aaaaaa**" in out
        assert "- shortid-shaped citations (citations are full item ids, always) — **1**" in out
        assert "  - **index** → `a1b2c3`" in out
        assert "- items no page cites and the taxonomy does not record — **1**" in out
        assert "- pages missing from index — **1**" in out
        assert "- ghost index entries — **1**" in out
        assert "- stale pages (members newer than the page) — **1**" in out
        assert "  - **pour-over** — 2 newer items" in out
        assert "- item-count drift (frontmatter `items:` vs members) — **1**" in out
        assert "  - **pour-over** — `items: 3`, 5 members" in out
        assert "- possible restated facts (same page, merge?) — **1**" in out
        assert "### State — 42 ledger entries" in out
        assert "- ledger — **42 entries**, schema valid" in out
        assert "- waiting on a capability — `transcribe` 3" in out
        assert "- read these yourself (the engine cannot do them) — **1**" in out
        assert "- harvest passes under old rules (re-judge) — **1**" in out
        assert "### Digests — 7 digests" in out
        assert "- digest these (enrichment newer than digest) — **1**" in out
        assert "### Reconciled by `--write` — 1 repair" in out
        assert "### Notes — 1 note" in out
        assert_no_trailing_whitespace(out)

    def test_cap_fires_lead_with_the_shape_of_the_drift(self):
        # The tuning question is never "is this one fire wrong" — it is how
        # many, over how many items, under which bound.
        out = render("health-report", HEALTH_PAYLOAD)
        assert "- re-entry cap fires (tuning signal, not an alarm) — **3** across 2 items" in out
        assert "  - by bound: `depth cap (4)` 1 · `url cap (12 per item)` 2" in out
        assert (
            "  - most often: **2026-01-06-deep-ffffff** 2 · **2026-01-07-wide-999999** 1"
        ) in out
        assert "  - **2026-01-06-deep-ffffff**" in out
        assert "    ↳ https://example.test/deep" in out

    def test_offenders_tied_on_count_are_named_in_id_order(self):
        # Rows arrive in whatever order the check produced; two items with
        # the same number of fires read in id order, not in arrival order,
        # so the same ledger always renders the same line.
        payload = dict(HEALTH_PAYLOAD)
        payload["capped"] = [
            {
                "item": "2026-01-07-wide-999999",
                "url": "https://example.test/a",
                "reason": "url cap (12 per item)",
            },
            {
                "item": "2026-01-06-deep-ffffff",
                "url": "https://example.test/b",
                "reason": "url cap (12 per item)",
            },
            {
                "item": "2026-01-07-wide-999999",
                "url": "https://example.test/c",
                "reason": "url cap (12 per item)",
            },
            {
                "item": "2026-01-06-deep-ffffff",
                "url": "https://example.test/d",
                "reason": "url cap (12 per item)",
            },
        ]
        out = render("health-report", payload)
        assert "most often: **2026-01-06-deep-ffffff** 2 · **2026-01-07-wide-999999** 2" in out

    def test_no_cap_fires_reads_as_none(self):
        out = render("health-report", CLEAN_HEALTH)
        assert "- re-entry cap fires (tuning signal, not an alarm) — none" in out

    def test_the_offenders_listing_says_how_much_it_withheld(self):
        # A capped listing says it is capped — the worst-offenders
        # line shows five items and must name how many it did not.
        payload = dict(HEALTH_PAYLOAD)
        payload["capped"] = [
            {
                "item": f"2026-02-{n:02d}-item-{n:06x}",
                "url": f"https://example.test/{n}",
                "reason": "url cap (12 per item)",
            }
            for n in range(1, 8)
        ]
        out = render("health-report", payload)
        line = next(part for part in out.splitlines() if "most often:" in part)
        assert line.count("**2026-02-") == 5
        assert "… and 2 more items, not listed" in line

    def test_incomplete_threads_name_the_file_and_the_gap(self):
        out = render("health-report", HEALTH_PAYLOAD)
        assert "- stored threads recorded incomplete (never cite one as whole) — **1**" in out
        assert (
            "  - `enrichment/2026-01-08-thread-888888/x-abc123.md` — parent fetch failed "
            "after 3 post(s): fxtwitter says the post is gone"
        ) in out

    def test_malformed_digests_render_loud(self):
        out = render("health-report", HEALTH_PAYLOAD)
        assert "- MALFORMED DIGESTS (the wiki layer reads these) — **1**" in out
        assert "  - **2026-01-09-broken-777777** — frontmatter missing topics" in out

    def test_a_restated_pair_carries_one_marker_each(self):
        # The marker rides inside the bullet text, not in a prefix — a
        # prefix repeats on every continuation line, so two facts rendered
        # four markers.
        long_fact = (
            "The dose is 60 grams of coffee per litre of water, measured "
            "on a scale rather than by volume, for every brew method here."
        )
        out = render(
            "health-report",
            {
                "summary": {"corpus_items": 1, "pages": 1, "cited": 1},
                "restated": [{"page": "pour-over", "first": long_fact, "second": long_fact}],
            },
        )
        marker_lines = [line for line in out.splitlines() if line.lstrip().startswith("- ~")]
        assert len(marker_lines) == 2
        assert all(line == f"    - ~ {long_fact}" for line in marker_lines)

    def test_clean_instance_still_states_every_check(self):
        # A check report is not a run report: the "none" line is the finding
        # and the proof the check ran.
        out = render("health-report", CLEAN_HEALTH)
        assert "- broken wikilinks — none" in out
        assert "- waiting on a capability — none" in out
        assert "- MALFORMED DIGESTS (the wiki layer reads these) — none" in out
        assert "- reserved/unbuilt links (informational) — none" in out
        assert "Reconciled by" not in out

    def test_a_capped_listing_says_how_much_it_withheld(self):
        out = render(
            "health-report",
            {
                "summary": {"corpus_items": 30, "pages": 0, "cited": 0},
                "orphans": [f"2026-01-{n:02d}-item-{n:06x}" for n in range(1, 26)],
            },
        )
        assert "- items no page cites and the taxonomy does not record — **25**" in out
        assert "  - … and 5 more, not listed" in out

    def test_ledger_error_renders_loud(self):
        out = render(
            "health-report",
            {
                "summary": {"corpus_items": 1, "pages": 0, "cited": 0},
                "ledger_error": "state/enrichment-ledger.jsonl:3: unknown field(s) ['note']",
            },
        )
        assert "**LEDGER SCHEMA FAILURE**" in out
        assert "unknown field(s)" in out
        # The section states its scale off the ledger, and a ledger that will
        # not load has none: "0 ledger entries" would report an unreadable
        # file as an empty one.
        assert "### State — ledger unreadable" in out

    def test_taxonomy_error_renders_loud(self):
        out = render(
            "health-report",
            {
                "summary": {"corpus_items": 1, "pages": 0, "cited": 0},
                "taxonomy_error": "taxonomy.json: expected a JSON object, got list",
            },
        )
        assert "**TAXONOMY FAILURE**" in out
        assert "expected a JSON object" in out

    def test_summary_is_required(self):
        with pytest.raises(PayloadError, match="summary"):
            render("health-report", {})

    def test_unknown_key_is_loud(self):
        with pytest.raises(PayloadError, match="broken_wikilinks"):
            render("health-report", {**CLEAN_HEALTH, "broken_wikilinks": []})

    def test_pre_taxonomy_fresh_instance_names_its_scale(self):
        out = render("health-report", {"pre_taxonomy": {"stranded": []}})
        assert out.startswith("## Health check — fresh instance, 0 corpus items\n")
        assert "No `state/taxonomy.json` yet — nothing to lint." in out
        assert_no_trailing_whitespace(out)

    def test_pre_taxonomy_stranded_items_render_broken_mid_ingest(self):
        stranded = ["2026-01-01-first-aaaaaa", "2026-01-02-second-bbbbbb"]
        out = render("health-report", {"pre_taxonomy": {"stranded": stranded}})
        assert out.startswith("## Health check — broken mid-ingest, 2 corpus items\n")
        assert "- **BROKEN MID-INGEST** — 2 corpus items but no `state/taxonomy.json`" in out
        assert "placement never ran" in out
        assert "  - **2026-01-01-first-aaaaaa**" in out
        assert "  - **2026-01-02-second-bbbbbb**" in out
        assert_no_trailing_whitespace(out)

    def test_pre_taxonomy_listing_says_how_much_it_withheld(self):
        stranded = [f"2026-01-{n:02d}-item-{n:06x}" for n in range(1, 26)]
        out = render("health-report", {"pre_taxonomy": {"stranded": stranded}})
        assert "broken mid-ingest, 25 corpus items" in out
        assert "  - … and 5 more, not listed" in out

    def test_pre_taxonomy_travels_alone(self):
        # A full payload beside it would render checks that never ran.
        with pytest.raises(PayloadError, match="summary"):
            render("health-report", {**CLEAN_HEALTH, "pre_taxonomy": {"stranded": []}})

    def test_pre_taxonomy_shape_is_validated_loudly(self):
        with pytest.raises(PayloadError, match="stranded"):
            render("health-report", {"pre_taxonomy": {}})
        with pytest.raises(PayloadError, match="must be an object"):
            render("health-report", {"pre_taxonomy": ["stranded"]})

    def test_bad_waiting_need_is_loud(self):
        with pytest.raises(PayloadError, match="transcode"):
            render("health-report", {**CLEAN_HEALTH, "waiting": {"transcode": 1}})


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
        assert out.startswith(
            "## Ingested 2026-08-18-rag-eval-harness-a1b2c3 — 3 units fetched, 1 outstanding\n"
        )
        assert "RAG eval harness" in out
        assert "- fetched 3 · outstanding 1" in out
        assert "- signal `high`" in out
        assert "- topics: agent-architecture" in out
        assert "- pages: agent-architecture, evals" in out
        assert "- thread walk-up found the repo link" in out

    def test_minimal_receipt_is_one_line(self):
        out = render("ingest-receipt", {"item": "2026-08-18-note-a1b2c3"})
        assert out == "## Ingested 2026-08-18-note-a1b2c3 — no units fetched\n"

    def test_item_is_required(self):
        with pytest.raises(PayloadError, match="item"):
            render("ingest-receipt", {"title": "no item"})

    def test_bad_signal_is_loud(self):
        with pytest.raises(PayloadError, match="signal"):
            render("ingest-receipt", {"item": "2026-08-18-note-a1b2c3", "signal": "shrug"})


class TestIdentityIsNeverTruncated:
    """The rule the whole rewrite exists to enforce, on every surface.

    The old layout hard-split any token wider than its column, so a long URL
    reached the reader as fragments and an id could break across a line.
    These assert the opposite property directly.
    """

    def test_enrich_report_over_a_real_parked_ledger(self):
        parked = [
            {
                "item": f"2026-08-{(i % 28) + 1:02d}-a-long-corpus-item-slug-that-runs-on-{i:06x}",
                "url": LONG_URL if i == 0 else f"https://example.test/page/{i}",
                "status": "blocked" if i % 2 else "manual",
                "reason": "403 challenge from the edge",
            }
            for i in range(82)
        ]
        out = render(
            "enrich-report",
            {
                "counts": {"done": 40, "blocked": 41, "manual": 41},
                "items": [{"id": p["item"], "reason": "1 new enrichment file"} for p in parked],
                "parked": parked,
                "cognitive": [
                    {"item": p["item"], "url": p["url"], "need": "extract"} for p in parked[:10]
                ],
            },
        )
        assert_whole(out, LONG_URL, *[str(p["item"]) for p in parked])

    def test_no_two_parked_entries_can_render_the_same(self):
        # URLs that agree for their first few hundred characters and differ
        # only deep in the query string: the shape a splitting layout used to
        # break across lines at the same place.
        base = "https://example.test/a-very-long-path-segment-that-forces-a-line-break/"
        urls = [base + f"{n:04d}" + "?utm_campaign=" + "x" * 200 for n in range(40)]
        parked = [
            {"item": LONG_ID, "url": url, "status": "manual", "reason": "paywall"} for url in urls
        ]
        out = render("enrich-report", {"counts": {}, "items": [], "parked": parked})
        rendered = [line for line in out.splitlines() if line.strip().startswith("↳ https")]
        assert len(rendered) == len(urls)
        assert len(set(rendered)) == len(urls)

    @pytest.mark.parametrize(
        ("surface", "payload"),
        [
            (
                "status",
                {"counts": {"done": 400}, "orphans": [LONG_ID, LONG_ID[:-6] + "ffffff"]},
            ),
            (
                "item-status",
                {
                    "item": LONG_ID,
                    "units": [
                        {"url": LONG_URL, "status": "done", "path": f"enrichment/{LONG_ID}/web.md"}
                    ],
                },
            ),
            ("ingest-receipt", {"item": LONG_ID, "title": LONG_URL, "fetched": 1}),
            (
                "health-report",
                {
                    "summary": {"corpus_items": 1, "pages": 1, "cited": 1},
                    "orphans": [LONG_ID],
                    "capped": [{"item": LONG_ID, "url": LONG_URL, "reason": "depth cap"}],
                    "cognitive": [{"item": LONG_ID, "url": LONG_URL, "need": "ocr"}],
                    "digest_orphans": [LONG_ID],
                },
            ),
            (
                "sync-report",
                {
                    "migrations": [{"number": 1, "intent": LONG_URL, "actions": [LONG_ID]}],
                    "machinery_changes": 0,
                    "pin": "v1",
                },
            ),
            (
                "capability-report",
                {
                    "capabilities": [
                        {"name": "ocr", "providers": [{"name": LONG_ID, "state": "active"}]}
                    ]
                },
            ),
        ],
    )
    def test_every_surface_emits_long_identity_whole(self, surface, payload):
        out = render(surface, payload)
        for value in (LONG_ID, LONG_URL):
            if value in repr(payload):
                assert_whole(out, value)

    def test_a_cjk_title_is_not_measured_wrapped_or_cut(self):
        title = "駆動型インジェストパイプラインの設計と実装についての詳細な記録" * 3
        out = render("ingest-receipt", {"item": LONG_ID, "title": title, "fetched": 3})
        assert_whole(out, title, LONG_ID)


SURFACE_PAYLOADS = {
    "enrich-report": ENRICH_PAYLOAD,
    "status": {"counts": {"done": 4}, "waiting": {"ocr": 1}, "orphans": [LONG_ID]},
    "item-status": {"item": LONG_ID, "units": [{"url": LONG_URL, "status": "done"}]},
    "capability-report": {
        "capabilities": [{"name": "ocr", "providers": [{"name": "vision", "state": "active"}]}]
    },
    "sync-report": {"pin": "v1", "migrations": [], "machinery_changes": 2},
    "ingest-receipt": {"item": LONG_ID, "fetched": 2},
    "health-report": HEALTH_PAYLOAD,
}


class TestNoSurfaceRendersATable:
    """Markdown, headings and bullets — no tables, and no hard wrapping."""

    def test_every_surface_has_a_payload_here(self):
        assert set(SURFACE_PAYLOADS) == set(SURFACES)

    @pytest.mark.parametrize("surface", sorted(SURFACE_PAYLOADS))
    def test_no_pipe_table_and_no_column_padding(self, surface):
        out = render(surface, SURFACE_PAYLOADS[surface])
        assert "|" not in out
        assert "─" not in out
        # Two or more spaces mid-line is column padding; markdown indents
        # are leading whitespace only.
        for line in out.splitlines():
            assert not re.search(r"\S {2,}\S", line), f"{surface}: padded columns in {line!r}"

    @pytest.mark.parametrize("surface", sorted(SURFACE_PAYLOADS))
    def test_output_is_markdown_and_newline_terminated(self, surface):
        out = render(surface, SURFACE_PAYLOADS[surface])
        assert out.startswith("## ")
        assert out.endswith("\n")
        assert not out.endswith("\n\n")
        assert_no_trailing_whitespace(out)
