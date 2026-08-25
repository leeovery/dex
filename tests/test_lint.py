"""Tests for lint.py: the mechanical health check."""

import datetime
import json

import pytest

from dex_engine.lint import LintOutcome, build_parser, main, run_lint
from dex_engine.pipeline import ledger
from dex_engine.pipeline.ownership import work_identity
from dex_engine.pipeline.registry import DRIVERS
from dex_engine.pipeline.types import Instance, Kind, LedgerEntry, Need, Status

TODAY = datetime.date(2026, 8, 20)
ITEM = "2026-08-19-example-55ad7b"


def never_cognitive(_need, _fmt=None):
    return False


def always_cognitive(_need, _fmt=None):
    return True


def lint(instance: Instance, *, is_cognitive=never_cognitive, write=False) -> LintOutcome:
    return run_lint(instance, is_cognitive=is_cognitive, today=lambda: TODAY, write=write)


def write_taxonomy(instance: Instance, topics=None, entities=None) -> None:
    tax = {"topics": topics or {}, "entities": entities or {}}
    (instance.state_dir / "taxonomy.json").write_text(json.dumps(tax))


def write_corpus_stub(instance: Instance, item_id: str = ITEM) -> None:
    path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nstub\n---\n")  # lint only reads stems


def write_corpus_item(instance: Instance, item_id: str, *, urls: list[str]) -> None:
    """A parseable item — the URL-claim check reads frontmatter, not stems."""
    path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    listing = "".join(f"  - {url}\n" for url in urls)
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: manual\nchannel: inbox\nshared_by: alex\ndate: 2026-08-19\n"
        f"urls:\n{listing}"
        "kinds: [web]\nstatus: raw\nenrichment: []\n---\n**alex**: note\n"
    )


def write_page(instance: Instance, name: str, text: str, group: str = "topics") -> None:
    path = instance.root / "wiki" / group / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def write_index(instance: Instance, text: str) -> None:
    path = instance.root / "wiki" / "index.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def page_text(*, items: int | None = 1, generated: str | None = "2026-08-01", body: str) -> str:
    fm = ["---", "topic: brewing"]
    if generated is not None:
        fm.append(f"generated: {generated}")
    if items is not None:
        fm.append(f"items: {items}")
    fm.append("---")
    return "\n".join(fm) + "\n" + body


class TestPreTaxonomy:
    def test_fresh_instance_is_clean(self, instance):
        outcome = lint(instance)
        assert outcome.exit_code == 0
        assert "fresh instance" in outcome.report

    def test_stranded_corpus_without_taxonomy_is_broken_mid_ingest(self, instance):
        write_corpus_stub(instance)
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert "broken mid-ingest" in outcome.report
        assert ITEM in outcome.report


class TestMalformedTaxonomy:
    """A broken state/taxonomy.json is a lint FINDING, never a crash."""

    def test_non_object_taxonomy_is_a_loud_finding(self, instance):
        (instance.state_dir / "taxonomy.json").write_text('["not", "an", "object"]')
        write_corpus_stub(instance)
        outcome = lint(instance)  # pre-fix: AttributeError traceback
        assert outcome.exit_code == 1
        assert "TAXONOMY FAILURE" in outcome.report
        assert "expected a JSON object, got list" in outcome.report

    def test_unparseable_taxonomy_is_a_loud_finding(self, instance):
        (instance.state_dir / "taxonomy.json").write_text("{not json")
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert "TAXONOMY FAILURE" in outcome.report
        assert "invalid JSON" in outcome.report

    def test_non_dict_topics_never_crashes_the_uncategorized_ledger(self, instance):
        write_corpus_stub(instance)
        (instance.state_dir / "taxonomy.json").write_text(
            json.dumps({"topics": ["uncategorized-shares"]})
        )
        write_index(instance, "")
        outcome = lint(instance)  # pre-fix: AttributeError on list.get
        assert ITEM in outcome.report  # nothing ledgered — the item is an orphan

    def test_non_string_topic_items_never_crash_staleness(self, instance):
        # A hand-mangled member list: the string member still counts, the
        # non-string one is ignored instead of a TypeError on slicing.
        newer = "2026-08-19-newer-cccccc"
        write_taxonomy(instance, topics={"brewing": {"items": [123, newer]}})
        write_corpus_stub(instance, newer)
        write_page(instance, "brewing", page_text(body=f"cite `{newer}`\n"))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert "brewing: 1 newer item(s)" in outcome.report


class TestWikiChecks:
    def test_clean_instance_passes(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM]}})
        write_corpus_stub(instance)
        write_page(instance, "brewing", page_text(body=f"A fact `{ITEM}`.\n"))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert outcome.exit_code == 0
        assert "1 corpus items · 1 pages · 1 cited" in outcome.report
        assert "broken wikilinks — none" in outcome.report

    def test_broken_vs_reserved_wikilinks(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": []}, "grinders": {"items": []}})
        write_page(
            instance,
            "brewing",
            page_text(items=None, body="See [[grinders]] and [[no-such-page]].\n"),
        )
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert outcome.exit_code == 1  # broken link
        assert "brewing -> [[no-such-page]]" in outcome.report
        assert "reserved/unbuilt links: 1" in outcome.report

    def test_bad_citation_fails(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": []}})
        write_page(
            instance,
            "brewing",
            page_text(items=None, body="Gone `2026-01-01-vanished-abc123`.\n"),
        )
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert "brewing -> 2026-01-01-vanished-abc123" in outcome.report

    def test_shortid_citations_flagged_everywhere_including_index(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": []}})
        write_page(instance, "brewing", page_text(items=None, body="A fact `a1b2c3`.\n"))
        write_index(instance, "[[brewing]] and a stray `d4e5f6`\n")
        outcome = lint(instance)
        assert "brewing -> `a1b2c3`" in outcome.report
        assert "index -> `d4e5f6`" in outcome.report

    def test_orphans_respect_the_uncategorized_ledger(self, instance):
        ledgered = "2026-08-19-ledgered-aaaaaa"
        orphaned = "2026-08-19-orphaned-bbbbbb"
        write_corpus_stub(instance, ledgered)
        write_corpus_stub(instance, orphaned)
        write_taxonomy(instance, topics={"uncategorized-shares": {"items": [ledgered]}})
        write_index(instance, "")
        outcome = lint(instance)
        assert orphaned in outcome.report
        assert "orphan items (uncited, unledgered) — 1" in outcome.report

    def test_index_consistency(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": []}})
        write_page(instance, "brewing", page_text(items=None, body="text\n"))
        write_index(instance, "[[ghost-page]]\n")  # brewing missing, ghost present
        outcome = lint(instance)
        assert "pages missing from index — 1" in outcome.report
        assert "ghost index entries — 1" in outcome.report
        assert "ghost-page" in outcome.report

    def test_stale_pages_count_newer_members(self, instance):
        newer = "2026-08-19-newer-cccccc"
        write_taxonomy(instance, topics={"brewing": {"items": [newer]}})
        write_corpus_stub(instance, newer)
        write_page(
            instance,
            "brewing",
            page_text(items=None, generated="2026-08-01", body=f"cite `{newer}`\n"),
        )
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert "stale pages (members newer than the page) — 1" in outcome.report
        assert "brewing: 1 newer item(s)" in outcome.report

    def test_count_drift_compares_members_not_citations(self, instance):
        # items: records the topic's MEMBER count; a page routinely cites
        # fewer items than its topic holds, and that is not drift.
        second = "2026-08-19-second-bbbbbb"
        write_corpus_stub(instance)
        write_corpus_stub(instance, second)
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM, second]}})
        write_page(instance, "brewing", page_text(items=3, body=f"cite `{ITEM}`\n"))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert "brewing: items: 3, members 2" in outcome.report
        assert outcome.exit_code == 0  # drift is repairable, not a failure

    def test_matching_member_count_is_never_drift(self, instance):
        second = "2026-08-19-second-bbbbbb"
        write_corpus_stub(instance)
        write_corpus_stub(instance, second)
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM, second]}})
        # Cites 1 of 2 members — items: 2 is correct under member semantics.
        write_page(instance, "brewing", page_text(items=2, body=f"cite `{ITEM}`\n"))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert "item-count drift (frontmatter items: vs members) — none" in outcome.report

    def test_entity_pages_count_against_entity_members(self, instance):
        write_corpus_stub(instance)
        write_taxonomy(instance, entities={"anthropic": {"kind": "org", "raw": []}})
        (instance.state_dir / "entity-members.json").write_text(
            json.dumps({"anthropic": [ITEM, "2026-08-19-second-bbbbbb"]})
        )
        text = f"---\nentity: anthropic\ngenerated: 2026-08-01\nitems: 5\n---\ncite `{ITEM}`\n"
        write_page(instance, "anthropic", text, group="entities")
        write_index(instance, "[[anthropic]]\n")
        outcome = lint(instance)
        assert "anthropic: items: 5, members 2" in outcome.report

    def test_unresolvable_pages_skip_the_count_check(self, instance):
        write_taxonomy(instance)  # no topics at all
        write_page(instance, "loose-page", page_text(items=9, body="prose only\n"))
        write_index(instance, "[[loose-page]]\n")
        outcome = lint(instance)
        assert "item-count drift (frontmatter items: vs members) — none" in outcome.report

    def test_body_lines_are_not_frontmatter(self, instance):
        # A body line reading `items: 99` is prose — it must never be
        # flagged, let alone "repaired".
        write_corpus_stub(instance)
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM]}})
        body = f"cite `{ITEM}`\nThe config sets items: 99 for the demo.\n"
        write_page(instance, "brewing", page_text(items=1, body=body))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance, write=True)
        assert "item-count drift (frontmatter items: vs members) — none" in outcome.report
        assert "items: 99" in (instance.root / "wiki" / "topics" / "brewing.md").read_text()

    def test_restated_facts_flagged_within_a_page(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": []}})
        a = "The canonical starting recipe is sixty grams per litre with a swirl."
        b = "The canonical starting recipe is sixty grams per liter with a swirl."
        write_page(instance, "brewing", page_text(items=None, body=f"{a}\n\n{b}\n"))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert "possible restated facts (same page — merge?) — 1" in outcome.report

    def test_distinct_sentences_are_not_restatements(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": []}})
        body = (
            "The canonical starting recipe is sixty grams per litre of water.\n\n"
            "Grind size should move coarser whenever the brew turns bitter or harsh.\n"
        )
        write_page(instance, "brewing", page_text(items=None, body=body))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert "possible restated facts (same page — merge?) — none" in outcome.report


class TestWrite:
    def test_write_reconciles_items_to_the_member_count(self, instance):
        write_corpus_stub(instance)
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM]}})
        write_page(instance, "brewing", page_text(items=3, body=f"cite `{ITEM}`\n"))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance, write=True)
        assert "brewing: items: 3 -> 1" in outcome.report
        rewritten = (instance.root / "wiki" / "topics" / "brewing.md").read_text()
        assert "items: 1" in rewritten
        assert "items: 3" not in rewritten
        # A second run is clean — the reconcile converged.
        assert "item-count drift (frontmatter items: vs members) — none" in lint(
            instance
        ).report

    def test_missing_closing_fence_is_a_note_never_a_claimed_repair(self, instance):
        write_corpus_stub(instance)
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM]}})
        broken = f"---\ntopic: brewing\nitems: 3\ncite `{ITEM}`\n"  # no closing fence
        write_page(instance, "brewing", broken)
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance, write=True)
        assert "added" not in outcome.report
        assert "no complete frontmatter fence" in outcome.report
        assert (instance.root / "wiki" / "topics" / "brewing.md").read_text() == broken

    def test_write_adds_missing_generated_date(self, instance):
        write_corpus_stub(instance)
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM]}})
        write_page(
            instance, "brewing", page_text(items=1, generated=None, body=f"cite `{ITEM}`\n")
        )
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance, write=True)
        assert "generated: 2026-08-20 added" in outcome.report
        rewritten = (instance.root / "wiki" / "topics" / "brewing.md").read_text()
        assert "generated: 2026-08-20" in rewritten

    def test_without_write_missing_generated_is_a_note(self, instance):
        write_corpus_stub(instance)
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM]}})
        write_page(
            instance, "brewing", page_text(items=1, generated=None, body=f"cite `{ITEM}`\n")
        )
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert "no generated: date" in outcome.report
        assert "generated:" not in (
            instance.root / "wiki" / "topics" / "brewing.md"
        ).read_text().split("\n---\n")[0]

    def test_write_never_touches_existing_generated_dates(self, instance):
        write_corpus_stub(instance)
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM]}})
        write_page(instance, "brewing", page_text(items=1, body=f"cite `{ITEM}`\n"))
        write_index(instance, "[[brewing]]\n")
        lint(instance, write=True)
        text = (instance.root / "wiki" / "topics" / "brewing.md").read_text()
        assert "generated: 2026-08-01" in text  # the staleness reference stands


def stamped(entry: LedgerEntry) -> LedgerEntry:
    return ledger.stamp(entry, today=lambda: TODAY, engine_version="0.1.0")


def waiting_entry(needs: Need, unit_hash: str = "73bd784849") -> LedgerEntry:
    return LedgerEntry(
        hash=unit_hash,
        url="https://example.test/a",
        item=ITEM,
        kind=Kind.WEB,
        status=Status.WAITING,
        needs=needs,
        engine="0.1.0",
        date=TODAY,
    )


class TestStateChecks:
    def _bare_wiki(self, instance):
        write_taxonomy(instance)
        write_index(instance, "")

    def test_valid_ledger_summarized(self, instance):
        self._bare_wiki(instance)
        ledger.append(instance.ledger_path, stamped(waiting_entry(Need.TRANSCRIBE)))
        outcome = lint(instance)
        assert "ledger — 1 entry, schema valid" in outcome.report
        assert "waiting cohorts: transcribe 1" in outcome.report
        assert outcome.exit_code == 0

    def test_schema_failure_is_loud_and_fails(self, instance):
        self._bare_wiki(instance)
        instance.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        instance.ledger_path.write_text('{"kind": "tweet", "status": "done"}\n')
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert "LEDGER SCHEMA FAILURE" in outcome.report
        assert "migration 1" in outcome.report

    def test_cognitive_jobs_listed_via_the_seam(self, instance):
        self._bare_wiki(instance)
        ledger.append(instance.ledger_path, stamped(waiting_entry(Need.OCR)))
        outcome = lint(instance, is_cognitive=always_cognitive)
        assert "cognitive jobs (the session completes these with eyes) — 1" in outcome.report
        assert "ocr" in outcome.report
        clean = lint(instance, is_cognitive=never_cognitive)
        assert "cognitive jobs (the session completes these with eyes) — none" in clean.report

    def test_stale_harvest_passes_flagged(self, instance):
        self._bare_wiki(instance)
        records = [
            {"stage": "harvest", "item": ITEM, "rules": 0, "date": "2026-08-01"},
            {"stage": "harvest", "item": "2026-08-19-current-dddddd", "rules": 1,
             "date": "2026-08-19"},
            {"stage": "digest", "item": ITEM, "date": "2026-08-01"},
        ]
        instance.passes_path.write_text("".join(json.dumps(r) + "\n" for r in records))
        outcome = lint(instance)
        assert "harvest passes under old rules (re-judge) — 1" in outcome.report
        assert f"{ITEM} (rules v0)" in outcome.report

    def test_latest_pass_supersedes_older_ones(self, instance):
        self._bare_wiki(instance)
        records = [
            {"stage": "harvest", "item": ITEM, "rules": 0, "date": "2026-08-01"},
            {"stage": "harvest", "item": ITEM, "rules": 1, "date": "2026-08-19"},
        ]
        instance.passes_path.write_text("".join(json.dumps(r) + "\n" for r in records))
        outcome = lint(instance)
        assert "harvest passes under old rules (re-judge) — none" in outcome.report

    def test_torn_pass_record_is_loud(self, instance):
        self._bare_wiki(instance)
        instance.passes_path.write_text('{"stage": "har\n')
        with pytest.raises(ValueError, match=r"passes\.jsonl:1"):
            lint(instance)

    def test_nonempty_quarantine_is_flagged(self, instance):
        self._bare_wiki(instance)
        (instance.state_dir / "enrichment-ledger.unmigrated.jsonl").write_text(
            '{"old": "line"}\n{"older": "line"}\n'
        )
        outcome = lint(instance)
        assert "QUARANTINE NOT EMPTY — 2 lines" in outcome.report

    def test_empty_quarantine_is_silent(self, instance):
        self._bare_wiki(instance)
        (instance.state_dir / "enrichment-ledger.unmigrated.jsonl").write_text("\n")
        assert "QUARANTINE" not in lint(instance).report

    def test_digest_orphans_listed(self, instance):
        self._bare_wiki(instance)
        item_dir = instance.enrichment_dir / ITEM
        item_dir.mkdir(parents=True)
        (item_dir / "web-abc123.md").write_text("enriched, never digested")
        outcome = lint(instance)
        assert "enrichment newer than digest" in outcome.report
        assert ITEM in outcome.report


def done_entry(
    unit_hash: str, *, item: str = ITEM, path: str | None = None, url: str | None = None
) -> LedgerEntry:
    return LedgerEntry(
        hash=unit_hash,
        url=url or f"https://example.test/{unit_hash}",
        item=item,
        kind=Kind.WEB,
        status=Status.DONE,
        engine="0.1.0",
        date=TODAY,
        path=path,
    )


class TestReferentialIntegrity:
    """A schema-valid ledger can still point at nothing — lint says so."""

    def _bare_wiki(self, instance):
        write_taxonomy(instance)
        write_index(instance, "")

    def test_entry_naming_an_excluded_item_is_named_as_excluded(self, instance):
        self._bare_wiki(instance)
        (instance.state_dir / "exclusions.tsv").write_text(
            "2024-04-11-document-library-0a7569\tpensions reference docs\n"
        )
        ledger.append(
            instance.ledger_path,
            done_entry("73bd784849", item="2024-04-11-document-library-0a7569"),
        )
        outcome = lint(instance)
        assert "ledger items with no corpus file — 1" in outcome.report
        assert (
            "2024-04-11-document-library-0a7569 — 1 entry (excluded on record)"
            in outcome.report
        )

    def test_entry_naming_an_unclaimed_item_is_told_apart(self, instance):
        self._bare_wiki(instance)
        ledger.append(instance.ledger_path, done_entry("73bd784849"))
        outcome = lint(instance)
        assert "ledger items with no corpus file — 1" in outcome.report
        assert "no exclusions.tsv record, and no live corpus item lists this work" in (
            outcome.report
        )

    def test_no_cause_is_guessed_for_an_unclaimed_item(self, instance):
        # Lint establishes what it checked, never how the item went away.
        self._bare_wiki(instance)
        ledger.append(instance.ledger_path, done_entry("73bd784849"))
        outcome = lint(instance)
        assert "removed by hand" not in outcome.report
        assert "purge was interrupted" not in outcome.report

    def test_a_renamed_item_is_not_reported_as_a_ghost(self, instance):
        # Same shortid, new slug: the entry's item id is dead, but the work
        # is claimed by a live item — a rename, never a purge.
        self._bare_wiki(instance)
        renamed = "2026-08-19-example-renamed-55ad7b"
        url = "https://example.test/moved"
        write_corpus_item(instance, renamed, urls=[url])
        ledger.append(
            instance.ledger_path, done_entry(work_identity(url, DRIVERS), url=url)
        )
        outcome = lint(instance)
        assert f"{ITEM} — 1 entry (renamed — {renamed} lists this work)" in outcome.report
        assert "no live corpus item lists this work" not in outcome.report

    def test_one_row_per_item_carries_its_entry_count(self, instance):
        # Two entries of one item rendered as two rows a reader could not
        # tell apart; one row states the multiplicity instead.
        self._bare_wiki(instance)
        ledger.append(instance.ledger_path, done_entry("73bd784849"))
        ledger.append(instance.ledger_path, done_entry("aaaaaaaaaa"))
        outcome = lint(instance)
        assert "ledger items with no corpus file — 1" in outcome.report
        assert f"{ITEM} — 2 entries (" in outcome.report

    def test_a_live_item_is_never_flagged(self, instance):
        self._bare_wiki(instance)
        write_corpus_stub(instance)
        ledger.append(instance.ledger_path, done_entry("73bd784849"))
        outcome = lint(instance)
        assert "ledger items with no corpus file — none" in outcome.report

    def test_done_entry_whose_output_is_gone_is_flagged(self, instance):
        self._bare_wiki(instance)
        write_corpus_stub(instance)
        ledger.append(
            instance.ledger_path,
            done_entry("73bd784849", path=f"enrichment/{ITEM}/web-73bd78.md"),
        )
        outcome = lint(instance)
        assert "done entries whose output file is gone from disk — 1" in outcome.report
        assert f"{ITEM} -> enrichment/{ITEM}/web-73bd78.md" in outcome.report

    def test_an_output_that_exists_is_never_flagged(self, instance):
        self._bare_wiki(instance)
        write_corpus_stub(instance)
        output = instance.enrichment_dir / ITEM / "web-73bd78.md"
        output.parent.mkdir(parents=True)
        output.write_text("the fetched page\n")
        ledger.append(
            instance.ledger_path,
            done_entry("73bd784849", path=f"enrichment/{ITEM}/web-73bd78.md"),
        )
        outcome = lint(instance)
        assert "done entries whose output file is gone from disk — none" in outcome.report

    def test_a_purged_item_answers_both_checks(self, instance):
        # exclude deletes enrichment/<item>/ too; both findings are true of
        # it, and neither is suppressed by the other.
        self._bare_wiki(instance)
        ledger.append(
            instance.ledger_path,
            done_entry("73bd784849", path=f"enrichment/{ITEM}/web-73bd78.md"),
        )
        outcome = lint(instance)
        assert "ledger items with no corpus file — 1" in outcome.report
        assert "done entries whose output file is gone from disk — 1" in outcome.report

    def test_findings_are_reported_without_failing_the_check(self, instance):
        # `dex exclude` deliberately leaves ledger history standing, so a
        # ghost item is the designed steady state — loud, never exit 1.
        self._bare_wiki(instance)
        write_corpus_stub(instance)
        ledger.append(
            instance.ledger_path,
            done_entry("73bd784849", path=f"enrichment/{ITEM}/web-73bd78.md"),
        )
        ledger.append(instance.ledger_path, done_entry("aaaaaaaaaa", item="2026-08-19-gone-000000"))
        outcome = lint(instance)
        assert outcome.exit_code == 0
        assert "ledger items with no corpus file — 1" in outcome.report
        assert "done entries whose output file is gone from disk — 1" in outcome.report

    def test_the_superseded_line_is_not_the_one_checked(self, instance):
        # Latest-per-hash: an old done line whose output was cleaned up is
        # history, not a finding, once the unit moved on.
        self._bare_wiki(instance)
        write_corpus_stub(instance)
        ledger.append(
            instance.ledger_path,
            done_entry("73bd784849", path=f"enrichment/{ITEM}/web-73bd78.md"),
        )
        ledger.append(instance.ledger_path, stamped(waiting_entry(Need.TRANSCRIBE)))
        outcome = lint(instance)
        assert "done entries whose output file is gone from disk — none" in outcome.report


class TestCli:
    def test_write_flag_parses(self):
        assert build_parser().parse_args(["--write"]).write is True
        assert build_parser().parse_args([]).write is False

    def test_typoed_flags_are_loud(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--wirte"])

    def test_main_prints_the_fresh_instance_notice(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        main([])
        assert "fresh instance" in capsys.readouterr().out

    def test_main_exits_1_on_failures(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        write_corpus_stub(instance)  # stranded: no taxonomy
        with pytest.raises(SystemExit) as excinfo:
            main([])
        assert excinfo.value.code == 1
        assert "broken mid-ingest" in capsys.readouterr().out
