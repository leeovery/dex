"""Tests for lint.py: the mechanical health check."""

import datetime
import json
from pathlib import Path

import pytest

from dex_engine.drivers.x import MAX_HOPS
from dex_engine.lint import LintOutcome, build_parser, main, run_lint
from dex_engine.pipeline import ledger
from dex_engine.pipeline.ownership import work_identity
from dex_engine.pipeline.registry import DRIVERS
from dex_engine.pipeline.run import CAP_BOUNDS
from dex_engine.pipeline.types import Cap, Instance, Kind, LedgerEntry, Need, Status

TODAY = datetime.date(2026, 8, 20)
NOW = datetime.datetime(2026, 8, 20, 8, 0, 0, 500000, tzinfo=datetime.UTC)
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
        # Rendered through the health-report surface, never hand-drawn:
        # the heading opens the report and names its scale.
        assert outcome.report.startswith("## Health check — fresh instance, 0 corpus items\n")
        assert "nothing to lint" in outcome.report

    def test_stranded_corpus_without_taxonomy_is_broken_mid_ingest(self, instance):
        write_corpus_stub(instance)
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert outcome.report.startswith("## Health check — broken mid-ingest, 1 corpus item\n")
        assert "BROKEN MID-INGEST" in outcome.report
        assert f"**{ITEM}**" in outcome.report

    def test_a_long_stranded_listing_says_it_is_capped(self, instance):
        for day in range(1, 26):
            write_corpus_stub(instance, f"2026-01-{day:02d}-stranded-{day:06d}")
        outcome = lint(instance)
        assert "broken mid-ingest, 25 corpus items" in outcome.report
        assert "… and 5 more, not listed" in outcome.report


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
        assert "**brewing** — 1 newer item" in outcome.report


class TestWikiChecks:
    def test_clean_instance_passes(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM]}})
        write_corpus_stub(instance)
        write_page(instance, "brewing", page_text(body=f"A fact `{ITEM}`.\n"))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert outcome.exit_code == 0
        assert "1 corpus item · 1 page · 1 cited" in outcome.report
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
        assert "**brewing** → `[[no-such-page]]`" in outcome.report
        assert "reserved/unbuilt links (informational) — **1**" in outcome.report

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
        assert "**brewing** → **2026-01-01-vanished-abc123**" in outcome.report

    def test_shortid_citations_flagged_everywhere_including_index(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": []}})
        write_page(instance, "brewing", page_text(items=None, body="A fact `a1b2c3`.\n"))
        write_index(instance, "[[brewing]] and a stray `d4e5f6`\n")
        outcome = lint(instance)
        assert "**brewing** → `a1b2c3`" in outcome.report
        assert "**index** → `d4e5f6`" in outcome.report

    def test_orphans_respect_the_uncategorized_ledger(self, instance):
        ledgered = "2026-08-19-ledgered-aaaaaa"
        orphaned = "2026-08-19-orphaned-bbbbbb"
        write_corpus_stub(instance, ledgered)
        write_corpus_stub(instance, orphaned)
        write_taxonomy(instance, topics={"uncategorized-shares": {"items": [ledgered]}})
        write_index(instance, "")
        outcome = lint(instance)
        assert orphaned in outcome.report
        assert "items no page cites and the taxonomy does not record — **1**" in outcome.report

    def test_index_consistency(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": []}})
        write_page(instance, "brewing", page_text(items=None, body="text\n"))
        write_index(instance, "[[ghost-page]]\n")  # brewing missing, ghost present
        outcome = lint(instance)
        assert "pages missing from index — **1**" in outcome.report
        assert "ghost index entries — **1**" in outcome.report
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
        assert "stale pages (members newer than the page) — **1**" in outcome.report
        assert "**brewing** — 1 newer item" in outcome.report

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
        assert "**brewing** — `items: 3`, 2 members" in outcome.report
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
        assert "item-count drift (frontmatter `items:` vs members) — none" in outcome.report

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
        assert "**anthropic** — `items: 5`, 2 members" in outcome.report

    def test_unresolvable_pages_skip_the_count_check(self, instance):
        write_taxonomy(instance)  # no topics at all
        write_page(instance, "loose-page", page_text(items=9, body="prose only\n"))
        write_index(instance, "[[loose-page]]\n")
        outcome = lint(instance)
        assert "item-count drift (frontmatter `items:` vs members) — none" in outcome.report

    def test_body_lines_are_not_frontmatter(self, instance):
        # A body line reading `items: 99` is prose — it must never be
        # flagged, let alone "repaired".
        write_corpus_stub(instance)
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM]}})
        body = f"cite `{ITEM}`\nThe config sets items: 99 for the demo.\n"
        write_page(instance, "brewing", page_text(items=1, body=body))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance, write=True)
        assert "item-count drift (frontmatter `items:` vs members) — none" in outcome.report
        assert "items: 99" in (instance.root / "wiki" / "topics" / "brewing.md").read_text()

    def test_restated_facts_flagged_within_a_page(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": []}})
        a = "The canonical starting recipe is sixty grams per litre with a swirl."
        b = "The canonical starting recipe is sixty grams per liter with a swirl."
        write_page(instance, "brewing", page_text(items=None, body=f"{a}\n\n{b}\n"))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert "possible restated facts (same page, merge?) — **1**" in outcome.report

    def test_distinct_sentences_are_not_restatements(self, instance):
        write_taxonomy(instance, topics={"brewing": {"items": []}})
        body = (
            "The canonical starting recipe is sixty grams per litre of water.\n\n"
            "Grind size should move coarser whenever the brew turns bitter or harsh.\n"
        )
        write_page(instance, "brewing", page_text(items=None, body=body))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert "possible restated facts (same page, merge?) — none" in outcome.report


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
        assert "item-count drift (frontmatter `items:` vs members) — none" in lint(instance).report

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
        write_page(instance, "brewing", page_text(items=1, generated=None, body=f"cite `{ITEM}`\n"))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance, write=True)
        assert "generated: 2026-08-20 added" in outcome.report
        rewritten = (instance.root / "wiki" / "topics" / "brewing.md").read_text()
        assert "generated: 2026-08-20" in rewritten

    def test_without_write_missing_generated_is_a_note(self, instance):
        write_corpus_stub(instance)
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM]}})
        write_page(instance, "brewing", page_text(items=1, generated=None, body=f"cite `{ITEM}`\n"))
        write_index(instance, "[[brewing]]\n")
        outcome = lint(instance)
        assert "no generated: date" in outcome.report
        assert (
            "generated:"
            not in (instance.root / "wiki" / "topics" / "brewing.md")
            .read_text()
            .split("\n---\n")[0]
        )

    def test_write_never_touches_existing_generated_dates(self, instance):
        write_corpus_stub(instance)
        write_taxonomy(instance, topics={"brewing": {"items": [ITEM]}})
        write_page(instance, "brewing", page_text(items=1, body=f"cite `{ITEM}`\n"))
        write_index(instance, "[[brewing]]\n")
        lint(instance, write=True)
        text = (instance.root / "wiki" / "topics" / "brewing.md").read_text()
        assert "generated: 2026-08-01" in text  # the staleness reference stands


def stamped(entry: LedgerEntry) -> LedgerEntry:
    return ledger.stamp(entry, today=lambda: TODAY, now=lambda: NOW, engine_version="0.1.0")


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
        assert "ledger — **1 entry**, schema valid" in outcome.report
        assert "waiting on a capability — `transcribe` 1" in outcome.report
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
        assert "read these yourself (the engine cannot do them) — **1**" in outcome.report
        assert "ocr" in outcome.report
        clean = lint(instance, is_cognitive=never_cognitive)
        assert "read these yourself (the engine cannot do them) — none" in clean.report

    def test_stale_harvest_passes_flagged(self, instance):
        self._bare_wiki(instance)
        records = [
            {"stage": "harvest", "item": ITEM, "rules": 0, "date": "2026-08-01"},
            {
                "stage": "harvest",
                "item": "2026-08-19-current-dddddd",
                "rules": 1,
                "date": "2026-08-19",
            },
            {"stage": "digest", "item": ITEM, "date": "2026-08-01"},
        ]
        instance.passes_path.write_text("".join(json.dumps(r) + "\n" for r in records))
        outcome = lint(instance)
        assert "harvest passes under old rules (re-judge) — **1**" in outcome.report
        assert f"**{ITEM}** — rules v0" in outcome.report

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
        assert "ledger items with no corpus file — **1**" in outcome.report
        assert (
            "**2024-04-11-document-library-0a7569** — 1 entry (excluded on record)"
            in outcome.report
        )

    def test_entry_naming_an_unclaimed_item_is_told_apart(self, instance):
        self._bare_wiki(instance)
        ledger.append(instance.ledger_path, done_entry("73bd784849"))
        outcome = lint(instance)
        assert "ledger items with no corpus file — **1**" in outcome.report
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
        ledger.append(instance.ledger_path, done_entry(work_identity(url, DRIVERS), url=url))
        outcome = lint(instance)
        assert f"**{ITEM}** — 1 entry (renamed — {renamed} lists this work)" in outcome.report
        assert "no live corpus item lists this work" not in outcome.report

    def test_a_deliberate_exclusion_is_never_reported_as_a_rename(self, instance):
        # `exclude` KEEPS a line whose work a survivor shares, so a purged
        # item always has another claimant — asking the claim first made
        # every on-the-record exclusion read as a rename, asserting a cause
        # lint never established over the one the owner wrote down.
        self._bare_wiki(instance)
        url = "https://example.test/shared"
        other = "2026-08-19-bravo-222222"
        write_corpus_item(instance, other, urls=[url])
        (instance.state_dir / "exclusions.tsv").write_text(f"{ITEM}\tout of scope\n")
        ledger.append(instance.ledger_path, done_entry(work_identity(url, DRIVERS), url=url))
        outcome = lint(instance)
        assert f"**{ITEM}** — 1 entry (excluded on record — {other} shares this work)" in (
            outcome.report
        )
        assert "renamed" not in outcome.report

    def test_one_row_per_item_carries_its_entry_count(self, instance):
        # Two entries of one item rendered as two rows a reader could not
        # tell apart; one row states the multiplicity instead.
        self._bare_wiki(instance)
        ledger.append(instance.ledger_path, done_entry("73bd784849"))
        ledger.append(instance.ledger_path, done_entry("aaaaaaaaaa"))
        outcome = lint(instance)
        assert "ledger items with no corpus file — **1**" in outcome.report
        assert f"**{ITEM}** — 2 entries (" in outcome.report

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
        assert "done entries whose output file is gone from disk — **1**" in outcome.report
        assert f"**{ITEM}** → `enrichment/{ITEM}/web-73bd78.md`" in outcome.report

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

    def _output(self, instance, item_id: str, name: str = "web-73bd78.md") -> None:
        path = instance.enrichment_dir / item_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("the fetched page\n")

    def test_an_output_under_another_items_directory_is_flagged(self, instance):
        # The output is on disk and the item's derived `enrichment:` listing
        # — the markdown in ITS directory — is empty, so it shows nothing
        # while the unit is `done` and never drainable again.
        self._bare_wiki(instance)
        write_corpus_stub(instance)
        self._output(instance, "2026-08-19-old-slug-55ad7b")
        ledger.append(
            instance.ledger_path,
            done_entry("73bd784849", path="enrichment/2026-08-19-old-slug-55ad7b/web-73bd78.md"),
        )
        outcome = lint(instance)
        assert "done entries whose output file is gone from disk — none" in outcome.report
        assert (
            "done entries whose output sits under another item's directory — **1**"
            in outcome.report
        )
        assert (
            f"**{ITEM}** → `enrichment/2026-08-19-old-slug-55ad7b/web-73bd78.md`" in outcome.report
        )

    def _renamed(self, instance, *, move_output: bool) -> tuple[str, str]:
        """A rename: the corpus file moved, the output either followed or not.

        The ledger line is untouched by design — its `item` is the
        attribution as of the day it was written and nothing heals it — so
        both checks have to resolve the owner off the corpus to say
        anything true about the path it records.
        """
        self._bare_wiki(instance)
        url = "https://example.test/moved"
        renamed = "2026-08-19-example-renamed-55ad7b"
        write_corpus_item(instance, renamed, urls=[url])
        unit_hash = work_identity(url, DRIVERS)
        name = f"web-{unit_hash[:6]}.md"
        self._output(instance, renamed if move_output else ITEM, name)
        ledger.append(
            instance.ledger_path,
            done_entry(unit_hash, url=url, path=f"enrichment/{ITEM}/{name}"),
        )
        return renamed, name

    def test_a_shared_units_output_answers_to_the_item_its_line_names(self, instance):
        # Two live items claim one URL, and the output is under the one the
        # line names — which is where the drain put it. Reading ownership
        # off the corpus alone would pick the first claimant by id and
        # report a misfiled output that is exactly where it belongs.
        self._bare_wiki(instance)
        url = "https://example.test/shared"
        first = "2026-08-19-alpha-111111"
        write_corpus_item(instance, first, urls=[url])
        write_corpus_item(instance, ITEM, urls=[url])
        unit_hash = work_identity(url, DRIVERS)
        name = f"web-{unit_hash[:6]}.md"
        self._output(instance, ITEM, name)
        ledger.append(
            instance.ledger_path,
            done_entry(unit_hash, url=url, path=f"enrichment/{ITEM}/{name}"),
        )
        outcome = lint(instance)
        assert "done entries whose output sits under another item's directory — none" in (
            outcome.report
        )

    def test_a_rename_carried_through_answers_neither_output_check(self, instance):
        # The directory moved with the corpus file, so the recorded path
        # names a directory that is gone while the file sits under the new
        # id, under the same name. Nothing is missing and nothing is
        # misfiled; the ghost row alone says the id on the line is history.
        renamed, _name = self._renamed(instance, move_output=True)
        outcome = lint(instance)
        assert f"**{ITEM}** — 1 entry (renamed — {renamed} lists this work)" in outcome.report
        assert "done entries whose output file is gone from disk — none" in outcome.report
        assert "done entries whose output sits under another item's directory — none" in (
            outcome.report
        )

    def test_a_rename_interrupted_before_the_directory_is_flagged_misfiled(self, instance):
        # The other half: the file stayed behind, so the live item's own
        # directory is empty and the row names the live item, which is the
        # one that cannot see its output.
        renamed, name = self._renamed(instance, move_output=False)
        outcome = lint(instance)
        assert (
            "done entries whose output sits under another item's directory — **1**"
            in outcome.report
        )
        assert f"**{renamed}** → `enrichment/{ITEM}/{name}`" in outcome.report
        assert "done entries whose output file is gone from disk — none" in outcome.report

    def test_an_output_under_its_own_item_is_never_flagged(self, instance):
        self._bare_wiki(instance)
        write_corpus_stub(instance)
        self._output(instance, ITEM)
        ledger.append(
            instance.ledger_path,
            done_entry("73bd784849", path=f"enrichment/{ITEM}/web-73bd78.md"),
        )
        outcome = lint(instance)
        assert "done entries whose output sits under another item's directory — none" in (
            outcome.report
        )

    def test_a_gone_output_is_the_other_finding_not_this_one(self, instance):
        # "not there" and "there, but where its item cannot see it" have
        # different repairs, so a path that is simply gone answers one check.
        self._bare_wiki(instance)
        write_corpus_stub(instance)
        ledger.append(
            instance.ledger_path,
            done_entry("73bd784849", path="enrichment/2026-08-19-old-slug-55ad7b/web-73bd78.md"),
        )
        outcome = lint(instance)
        assert "done entries whose output file is gone from disk — **1**" in outcome.report
        assert "done entries whose output sits under another item's directory — none" in (
            outcome.report
        )

    def test_a_misfiled_output_is_a_finding_not_a_failure(self, instance):
        # Same rule as its two siblings: the repair (move the directory?
        # re-fetch? finish the rename?) is judgment, not the exit code's.
        self._bare_wiki(instance)
        write_corpus_stub(instance)
        self._output(instance, "2026-08-19-old-slug-55ad7b")
        ledger.append(
            instance.ledger_path,
            done_entry("73bd784849", path="enrichment/2026-08-19-old-slug-55ad7b/web-73bd78.md"),
        )
        assert lint(instance).exit_code == 0

    def test_a_purged_item_answers_both_checks(self, instance):
        # exclude deletes enrichment/<item>/ too; both findings are true of
        # it, and neither is suppressed by the other.
        self._bare_wiki(instance)
        ledger.append(
            instance.ledger_path,
            done_entry("73bd784849", path=f"enrichment/{ITEM}/web-73bd78.md"),
        )
        outcome = lint(instance)
        assert "ledger items with no corpus file — **1**" in outcome.report
        assert "done entries whose output file is gone from disk — **1**" in outcome.report

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
        assert "ledger items with no corpus file — **1**" in outcome.report
        assert "done entries whose output file is gone from disk — **1**" in outcome.report

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


def capped_entry(
    unit_hash: str, *, item: str = ITEM, cap: Cap, url: str, forced: bool = False
) -> LedgerEntry:
    return LedgerEntry(
        hash=unit_hash,
        url=url,
        item=item,
        kind=Kind.WEB,
        status=Status.SKIPPED,
        cap=cap,
        forced=forced,
        engine="0.1.0",
        date=TODAY,
        via="harvest",
        parent="0000000000",
        depth=5,
        # The engine words the same bound differently per writer; the check
        # reads the typed cap and the typed override, never this.
        reason=f"{CAP_BOUNDS[cap]} {'exceeded by --force' if forced else 'reached'}",
    )


DEPTH_CAP = CAP_BOUNDS[Cap.DEPTH]
URL_CAP = CAP_BOUNDS[Cap.URL_REQUESTED]


def _cap_fire_block(report: str) -> str:
    """The cap-fire bullet and its rows, cut from the sections either side.

    Item ids repeat under the ghost-item listing, so an ordering read on the
    whole report answers about the wrong section.
    """
    return report.split("re-entry cap fires", maxsplit=1)[1].split("stored threads", maxsplit=1)[0]


class TestCapFires:
    """The ledger records cap fires for this check and no other surface."""

    def _bare_wiki(self, instance):
        write_taxonomy(instance)
        write_index(instance, "")

    def test_no_cap_fires_reads_as_none(self, instance):
        self._bare_wiki(instance)
        ledger.append(instance.ledger_path, stamped(waiting_entry(Need.TRANSCRIBE)))
        outcome = lint(instance)
        assert "re-entry cap fires (tuning signal, not an alarm) — none" in outcome.report

    def test_fires_are_counted_by_bound_and_by_item(self, instance):
        self._bare_wiki(instance)
        other = "2026-08-19-other-bbbbbb"
        fires = [(ITEM, Cap.DEPTH), (ITEM, Cap.URL_REQUESTED), (other, Cap.URL_REQUESTED)]
        for i, (item, cap) in enumerate(fires):
            ledger.append(
                instance.ledger_path,
                capped_entry(f"{i:010x}", item=item, cap=cap, url=f"https://example.test/{i}"),
            )
        outcome = lint(instance)
        assert "re-entry cap fires (tuning signal, not an alarm) — **3** across 2 items" in (
            outcome.report
        )
        assert f"`{DEPTH_CAP}` 1 · `{URL_CAP}` 2" in outcome.report
        assert f"most often: **{ITEM}** 2 · **{other}** 1" in outcome.report
        assert "↳ https://example.test/0" in outcome.report

    def test_the_bound_is_read_off_its_wording_not_the_marker_spelling(self, instance):
        # The URL bound's marker is spelled `url-requested` (what an
        # `enrich fetch` refusal writes), but the reading aggregates on the
        # bound's one wording — the marker spelling never leaks as a bound
        # of its own.
        self._bare_wiki(instance)
        other = "2026-08-19-other-bbbbbb"
        fires = [
            (ITEM, Cap.DEPTH),
            (ITEM, Cap.URL_REQUESTED),
            (other, Cap.URL_REQUESTED),
        ]
        for i, (item, cap) in enumerate(fires):
            ledger.append(
                instance.ledger_path,
                capped_entry(f"{i:010x}", item=item, cap=cap, url=f"https://example.test/{i}"),
            )
        flat = " ".join(lint(instance).report.split())
        assert "re-entry cap fires (tuning signal, not an alarm) — **3** across 2 items" in flat
        assert "by bound: `depth cap (4)` 1 · `url cap (12 per item)` 2" in flat
        assert "url-requested" not in flat

    def test_a_refusal_that_stood_is_read_whoever_asked_for_the_url(self, instance):
        # Every promotion is a URL a session named, so "who typed it"
        # separates nothing: a refusal nobody overrode is drift to read.
        self._bare_wiki(instance)
        ledger.append(
            instance.ledger_path,
            capped_entry("73bd784849", cap=Cap.URL_REQUESTED, url="https://example.test/refused"),
        )
        flat = " ".join(lint(instance).report.split())
        assert "re-entry cap fires (tuning signal, not an alarm) — **1** across 1 item" in flat
        assert "https://example.test/refused" in flat

    def test_a_fire_the_owner_waived_leaves_the_reading(self, instance):
        # `--force` is a decision already taken; reading it back as drift
        # would tell the owner their own override is the signal.
        self._bare_wiki(instance)
        ledger.append(
            instance.ledger_path,
            capped_entry(
                "73bd784849",
                cap=Cap.URL_REQUESTED,
                url="https://example.test/deepened",
                forced=True,
            ),
        )
        flat = " ".join(lint(instance).report.split())
        assert "re-entry cap fires (tuning signal, not an alarm) — none" in flat
        assert "https://example.test/deepened" not in flat

    def test_the_newest_fire_is_listed_first(self, instance):
        # Nothing supersedes a cap line, so the listing only grows: oldest
        # first would bury the fires a session can still act on.
        self._bare_wiki(instance)
        old_item = "2026-01-02-old-aaaaaa"
        new_item = "2026-08-20-new-bbbbbb"
        for i, item in enumerate((old_item, new_item)):
            ledger.append(
                instance.ledger_path,
                capped_entry(
                    f"{i:010x}", item=item, cap=Cap.URL_REQUESTED, url=f"https://example.test/{i}"
                ),
            )
        # The counts above the listing are ordered their own way, and bare ids
        # also appear under the ghost-item listing, so the ordering is read
        # inside the cap-fire block on the URLs the rows name.
        block = _cap_fire_block(lint(instance).report)
        assert block.index("https://example.test/1") < block.index("https://example.test/0")
        assert new_item in block
        assert old_item in block

    def test_a_fire_today_is_listed_even_once_the_listing_is_full(self, instance):
        # The surface caps the listing; the fire stamped today must not be
        # the one it drops.
        self._bare_wiki(instance)
        older = [f"2026-01-{day:02d}-old-{day:06d}" for day in range(1, 26)]
        newest = "2026-08-20-brand-new-ffffff"
        for i, item in enumerate([*older, newest]):
            ledger.append(
                instance.ledger_path,
                capped_entry(
                    f"{i:010x}",
                    item=item,
                    cap=Cap.URL_REQUESTED,
                    url=f"https://example.test/{i:02d}",
                ),
            )
        report = lint(instance).report
        assert "re-entry cap fires (tuning signal, not an alarm) — **26** across 26 items" in report
        block = _cap_fire_block(report)
        assert newest in block
        assert "https://example.test/25" in block
        # the oldest is what the cap drops (bare ids also appear under the
        # ghost-item listing, so the URL is what this asserts on)
        assert "https://example.test/00" not in report

    def test_a_tuning_signal_never_fails_the_check(self, instance):
        self._bare_wiki(instance)
        ledger.append(
            instance.ledger_path,
            capped_entry("73bd784849", cap=Cap.URL_REQUESTED, url="https://example.test/refused"),
        )
        assert lint(instance).exit_code == 0

    def test_an_ordinary_skip_is_not_a_cap_fire(self, instance):
        # `cap` is the marker, not the status: a skipped entry parked for
        # any other stated reason is not the cap firing.
        self._bare_wiki(instance)
        ledger.append(
            instance.ledger_path,
            LedgerEntry(
                hash="73bd784849",
                url="https://example.test/paywalled",
                item=ITEM,
                kind=Kind.WEB,
                status=Status.SKIPPED,
                engine="0.1.0",
                date=TODAY,
                reason="paywalled",
            ),
        )
        outcome = lint(instance)
        assert "re-entry cap fires (tuning signal, not an alarm) — none" in outcome.report


def write_enrichment(
    instance: Instance, name: str, frontmatter: str, body: str = "body", item: str = ITEM
) -> None:
    path = instance.enrichment_dir / item / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nurl: https://x.com/i/status/1\n{frontmatter}---\n\n{body}\n")


SKILL = Path(__file__).resolve().parents[1] / "instance" / "skills" / "dex-lint" / "SKILL.md"


class TestThreadCapText:
    """The shipped guidance names the driver's bound, or it misdiagnoses.

    At 100 hops a cap hit means a cycle or a self-referencing parent, not a
    truncated thread — a session reading a stale number writes a digest note
    about a root that is not missing.
    """

    def test_the_skill_names_the_drivers_walk_up_bound(self):
        text = SKILL.read_text(encoding="utf-8")
        assert f"{MAX_HOPS}-hop" in text
        assert "20-hop" not in text


class TestThreadCompleteness:
    """A half-thread reads exactly like a whole one unless something says so."""

    def _bare_wiki(self, instance):
        write_taxonomy(instance)
        write_index(instance, "")

    def test_chain_incomplete_reports_the_drivers_note(self, instance):
        self._bare_wiki(instance)
        write_enrichment(
            instance,
            "x-abc123.md",
            'chain_incomplete: "true"\nchain_note: "parent fetch failed after 3 post(s): 404"\n',
        )
        outcome = lint(instance)
        assert (
            "stored threads recorded incomplete (never cite one as whole) — **1**"
        ) in outcome.report
        assert (
            f"`enrichment/{ITEM}/x-abc123.md` — parent fetch failed after 3 post(s): 404"
        ) in outcome.report

    def test_thread_cap_hit_names_itself_without_a_note(self, instance):
        self._bare_wiki(instance)
        write_enrichment(instance, "x-abc123.md", 'thread_cap_hit: "true"\n')
        outcome = lint(instance)
        assert f"`enrichment/{ITEM}/x-abc123.md` — thread_cap_hit" in outcome.report

    def test_both_markers_report_together(self, instance):
        self._bare_wiki(instance)
        write_enrichment(
            instance, "x-abc123.md", 'thread_cap_hit: "true"\nchain_incomplete: "true"\n'
        )
        outcome = lint(instance)
        assert "thread_cap_hit + chain_incomplete" in outcome.report

    def test_a_complete_thread_is_never_flagged(self, instance):
        self._bare_wiki(instance)
        write_enrichment(instance, "x-abc123.md", "author: someone (@someone)\n")
        outcome = lint(instance)
        assert (
            "stored threads recorded incomplete (never cite one as whole) — none"
        ) in outcome.report

    def test_the_body_is_never_read(self, instance):
        # Enrichment bodies are whole transcripts; a body line that looks
        # like a marker is content, and the scan must not have seen it.
        self._bare_wiki(instance)
        write_enrichment(
            instance, "x-abc123.md", "author: someone\n", body="chain_incomplete: true"
        )
        outcome = lint(instance)
        assert (
            "stored threads recorded incomplete (never cite one as whole) — none"
        ) in outcome.report

    def test_incomplete_threads_never_fail_the_check(self, instance):
        self._bare_wiki(instance)
        write_enrichment(instance, "x-abc123.md", 'chain_incomplete: "true"\n')
        assert lint(instance).exit_code == 0

    def test_the_newest_thread_is_listed_first(self, instance):
        # Nothing clears these markers, so the listing only grows: oldest
        # first would bury the threads a session is about to cite.
        self._bare_wiki(instance)
        old_item = "2026-01-02-old-aaaaaa"
        new_item = "2026-08-19-new-bbbbbb"
        for item in (old_item, new_item):
            write_enrichment(instance, "x-abc123.md", 'chain_incomplete: "true"\n', item=item)
        report = lint(instance).report
        assert report.index(new_item) < report.index(old_item)

    def test_a_new_marker_is_listed_even_once_the_listing_is_full(self, instance):
        # The surface caps the listing; the marker stamped today must not be
        # the one it drops.
        self._bare_wiki(instance)
        older = [f"2026-01-{day:02d}-old-{day:06d}" for day in range(1, 26)]
        newest = "2026-08-19-new-bbbbbb"
        for item in [*older, newest]:
            write_enrichment(instance, "x-abc123.md", 'chain_incomplete: "true"\n', item=item)
        report = lint(instance).report
        assert "stored threads recorded incomplete (never cite one as whole) — **26**" in report
        assert f"enrichment/{newest}/x-abc123.md" in report
        # the oldest is what the cap drops (bare ids also appear under the
        # digest-orphan listing, so the path is what this asserts on)
        assert f"enrichment/{older[0]}/x-abc123.md" not in report

    def test_a_marker_set_to_false_is_not_a_marker(self, instance):
        # The driver stamps the string "true"; the field's presence says
        # nothing on its own, and a walk that completed can say so.
        self._bare_wiki(instance)
        write_enrichment(instance, "x-abc123.md", 'thread_cap_hit: "false"\nchain_incomplete: ""\n')
        outcome = lint(instance)
        assert (
            "stored threads recorded incomplete (never cite one as whole) — none"
        ) in outcome.report

    def test_a_body_that_is_not_utf8_never_hides_the_frontmatter(self, instance):
        # The scan stops at the closing fence, so a transcript full of bytes
        # no decoder accepts costs nothing and hides nothing. Slurping the
        # file would lose the marker to a decode error instead.
        self._bare_wiki(instance)
        path = instance.enrichment_dir / ITEM / "x-abc123.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            b'---\nchain_incomplete: "true"\n---\n\n' + b"pad\n" * 40000 + b"\xff\xfe\n"
        )
        outcome = lint(instance)
        assert (
            "stored threads recorded incomplete (never cite one as whole) — **1**"
        ) in outcome.report
        assert "unreadable" not in outcome.report

    def test_an_unreadable_enrichment_file_is_reported(self, instance):
        # Nothing else in lint reads enrichment files, so a file this scan
        # cannot decode is invisible everywhere unless it says so here.
        self._bare_wiki(instance)
        path = instance.enrichment_dir / ITEM / "x-abc123.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"---\nchain_incomplete: \xff\xfe true\n---\n\nbody\n")
        flat = " ".join(lint(instance).report.split())  # the surface wraps notes
        assert f"enrichment/{ITEM}/x-abc123.md: unreadable (UnicodeDecodeError)" in flat
        assert "thread completeness unknown" in flat

    def test_an_unclosed_frontmatter_fence_is_reported(self, instance):
        # What an interrupted write leaves: a marker stamped inside a
        # frontmatter block that never closes. Read as no fields at all, it
        # was indistinguishable from a clean file and vanished from the
        # report — the half-thread with it.
        self._bare_wiki(instance)
        path = instance.enrichment_dir / ITEM / "x-abc123.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '---\nurl: https://x.com/i/status/1\nchain_incomplete: "true"\n'
            'chain_note: "parent fetch failed after 3 post(s): 404"\n'
        )
        outcome = lint(instance)
        flat = " ".join(outcome.report.split())  # the surface wraps notes
        assert f"enrichment/{ITEM}/x-abc123.md: unterminated frontmatter" in flat
        assert "thread completeness unknown" in flat
        assert outcome.exit_code == 0

    def test_an_unreadable_enrichment_file_never_fails_the_check(self, instance):
        self._bare_wiki(instance)
        path = instance.enrichment_dir / ITEM / "x-abc123.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"---\n\xff\xfe\n---\n")
        assert lint(instance).exit_code == 0


def write_digest(instance: Instance, item: str, text: str) -> None:
    instance.digests_dir.mkdir(parents=True, exist_ok=True)
    (instance.digests_dir / f"{item}.md").write_text(text)


def digest_text(
    *,
    item: str = ITEM,
    signal: str = "high",
    topics: str = "[brewing]",
    bullets: int = 1,
    omit: str = "",
) -> str:
    fields = {"id": item, "date": "2026-08-19", "signal": signal, "topics": topics}
    fm = ["---", *(f"{key}: {value}" for key, value in fields.items() if key != omit), "---"]
    body = "\n".join(f"- fact number {i} with concrete specifics." for i in range(bullets))
    return "\n".join(fm) + "\n" + body + "\n"


class TestDigestShape:
    """No verb writes digests — lint is the only thing that verifies them."""

    def _bare_wiki(self, instance):
        write_taxonomy(instance)
        write_index(instance, "")

    @pytest.mark.parametrize("bullets", [1, 2, 3, 40])
    def test_any_number_of_facts_conforms(self, instance, bullets):
        # The count measures the source, not the digest: two lines of
        # tweet yield two facts and a paper yields forty.
        self._bare_wiki(instance)
        write_digest(instance, ITEM, digest_text(bullets=bullets))
        outcome = lint(instance)
        assert outcome.exit_code == 0
        assert "MALFORMED DIGESTS (the wiki layer reads these) — none" in outcome.report

    def test_no_frontmatter_fence_fails(self, instance):
        self._bare_wiki(instance)
        write_digest(instance, ITEM, "- a bare bullet list, no frontmatter\n")
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert f"**{ITEM}** — no complete frontmatter fence" in outcome.report

    def test_unclosed_fence_fails(self, instance):
        self._bare_wiki(instance)
        write_digest(instance, ITEM, f"---\nid: {ITEM}\nsignal: high\n- a fact\n")
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert "no complete frontmatter fence" in outcome.report

    @pytest.mark.parametrize("field", ["id", "date", "signal", "topics"])
    def test_missing_required_field_fails(self, instance, field):
        self._bare_wiki(instance)
        write_digest(instance, ITEM, digest_text(omit=field))
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert f"**{ITEM}** — frontmatter missing {field}" in outcome.report

    def test_bogus_signal_fails(self, instance):
        self._bare_wiki(instance)
        write_digest(instance, ITEM, digest_text(signal="urgent"))
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert "signal must be one of high, medium, low, got 'urgent'" in outcome.report

    @pytest.mark.parametrize("topics", ["[]", "[ ]", "[  ]", "[\t]"])
    def test_empty_topics_fails_however_it_is_spaced(self, instance, topics):
        # The emptiness of a list is not a spelling of its brackets.
        self._bare_wiki(instance)
        write_digest(instance, ITEM, digest_text(topics=topics))
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert "topics is empty" in outcome.report

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_quoted_scalars_conform(self, instance, quote):
        # Digests are freehand and session-written; nothing tells the
        # session to leave a scalar bare, and to the only other reader they
        # have — a session reading YAML — a quoted scalar is the same
        # scalar. Quoted was a hard failure on a correct digest.
        self._bare_wiki(instance)
        q = quote
        write_digest(
            instance,
            ITEM,
            f"---\nid: {q}{ITEM}{q}\ndate: {q}2026-08-19{q}\nsignal: {q}high{q}\n"
            f"topics: [{q}brewing{q}]\n---\n- fact number 0 with concrete specifics.\n",
        )
        outcome = lint(instance)
        assert outcome.exit_code == 0
        assert "MALFORMED DIGESTS (the wiki layer reads these) — none" in outcome.report

    def test_a_quoted_signal_is_still_read_against_the_vocabulary(self, instance):
        # Unquoting is not laxness: the value inside the quotes is checked.
        self._bare_wiki(instance)
        write_digest(instance, ITEM, digest_text(signal='"urgent"'))
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert "signal must be one of high, medium, low, got 'urgent'" in outcome.report

    def test_block_style_topics_conform(self, instance):
        # YAML's other list form, and the one a session writes by hand.
        self._bare_wiki(instance)
        write_digest(
            instance,
            ITEM,
            f"---\nid: {ITEM}\ndate: 2026-08-19\nsignal: high\ntopics:\n  - brewing\n"
            "  - coffee\n---\n- fact number 0 with concrete specifics.\n",
        )
        outcome = lint(instance)
        assert outcome.exit_code == 0
        assert "MALFORMED DIGESTS (the wiki layer reads these) — none" in outcome.report

    def test_a_topics_key_with_nothing_under_it_is_missing(self, instance):
        # A block list opened and never written is the key with no value.
        self._bare_wiki(instance)
        write_digest(instance, ITEM, digest_text(topics=""))
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert f"**{ITEM}** — frontmatter missing topics" in outcome.report

    def test_id_must_match_the_filename(self, instance):
        self._bare_wiki(instance)
        write_digest(instance, ITEM, digest_text(item="2026-08-19-elsewhere-999999"))
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert f"id is '2026-08-19-elsewhere-999999' but the file is {ITEM}.md" in outcome.report

    def test_a_digest_stating_no_facts_fails(self, instance):
        # Empty is different in kind from brief: the file holds none of
        # the one thing it exists for.
        self._bare_wiki(instance)
        write_digest(instance, ITEM, digest_text(bullets=0))
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert f"**{ITEM}** — states no facts" in outcome.report

    def test_prose_without_bullets_still_states_no_facts(self, instance):
        self._bare_wiki(instance)
        write_digest(instance, ITEM, digest_text(bullets=0) + "some loose prose\n")
        outcome = lint(instance)
        assert outcome.exit_code == 1
        assert "states no facts" in outcome.report

    def test_the_frontmatter_fault_is_reported_before_the_body(self, instance):
        # One digest, one finding — and the frontmatter is what to fix first.
        self._bare_wiki(instance)
        write_digest(instance, ITEM, digest_text(signal="urgent", bullets=0))
        outcome = lint(instance)
        assert "signal must be one of" in outcome.report
        assert "states no facts" not in outcome.report

    def test_no_digests_directory_is_clean(self, instance):
        self._bare_wiki(instance)
        outcome = lint(instance)
        assert outcome.exit_code == 0
        assert "MALFORMED DIGESTS (the wiki layer reads these) — none" in outcome.report
        assert "### Digests — 0 digests" in outcome.report

    def test_the_digest_section_states_how_many_digests_there_are(self, instance):
        # Every heading states its scale; this one had none, so a reader who
        # stopped at it could not tell an instance with no digests from one
        # whose digests all conform.
        self._bare_wiki(instance)
        write_digest(instance, ITEM, digest_text(bullets=2))
        second = "2026-08-19-second-bbbbbb"
        write_digest(instance, second, digest_text(item=second, bullets=1))
        outcome = lint(instance)
        assert "### Digests — 2 digests" in outcome.report
        assert "MALFORMED DIGESTS (the wiki layer reads these) — none" in outcome.report


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
