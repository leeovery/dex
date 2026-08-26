"""Tests for migration 4: requeue x posts stored as a bare link to their article."""

import datetime
import json

import pytest

from dex_engine import corpus
from dex_engine.migrations.migration_4 import build
from dex_engine.pipeline.ledger import LedgerSchemaError, from_line, to_line
from dex_engine.pipeline.types import Kind, LedgerEntry, Status
from dex_engine.pipeline.urls import work_hash

TODAY = datetime.date(2026, 8, 26)
NOW = datetime.datetime(2026, 8, 26, 9, 0, 0, 500000, tzinfo=datetime.UTC)

ITEM = "2026-08-26-2092243372929151135-bca608"
POST_URL = "https://x.com/i/status/2092243372929151135"
ARTICLE_URL = "https://x.com/i/article/2092243366759235586"
POST_HASH = work_hash(POST_URL)

# The stub the shipped driver wrote: attribution, then the article's URL.
STUB_BODY = f"@VibeMarketer_ — Tue Aug 25 13:31:06 +0000 2026\n\n{ARTICLE_URL}"
REAL_BODY = "A thread about failure classification, with actual prose in it."


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.0")


def write_enrichment(root, item, name, body):
    path = root / "enrichment" / item / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nurl: "{POST_URL}"\nfetched: 2026-08-26\n---\n\n{body}\n')
    return f"enrichment/{item}/{name}"


def write_item(root, item=ITEM):
    """A real corpus file — corpus_owners reads these, so a hand-rolled stub will not do."""
    path = root / "corpus" / item[:4] / f"{item}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    corpus.write_item(
        path,
        corpus.CorpusItem(
            id=item,
            source="discord",
            channel="general",
            shared_by="Lee",
            date=TODAY,
            urls=[POST_URL],
            kinds=["x"],
            media=[],
            body="the owner's note\n",
        ),
    )


def ledger_entry(
    *,
    item: str = ITEM,
    path: str | None = None,
    kind: Kind = Kind.X,
    title: str | None = None,
) -> LedgerEntry:
    """A done x page line as 0.1.0 wrote it. Only done lines can carry a path."""
    return LedgerEntry(
        hash=POST_HASH,
        url=POST_URL,
        item=item,
        kind=kind,
        status=Status.DONE,
        engine="0.1.0",
        date=TODAY,
        at=NOW,
        path=path,
        title=title,
    )


def write_ledger(root, entries):
    path = root / "state" / "enrichment-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(to_line(e) + "\n" for e in entries))
    return path


def lines(path):
    """Parsed ledger lines, skipping any the tests deliberately tore."""
    out = []
    for line in path.read_text().split("\n"):
        if not line.strip():
            continue
        try:
            out.append(from_line(line))
        except (LedgerSchemaError, ValueError):
            continue
    return out


class TestCohort:
    def test_a_stub_whose_body_is_the_article_link_is_requeued(self, tmp_path, migration):
        write_item(tmp_path)
        rel = write_enrichment(tmp_path, ITEM, "x-179456.md", STUB_BODY)
        path = write_ledger(tmp_path, [ledger_entry(path=rel)])
        report = migration.apply(tmp_path)
        seeded = lines(path)[-1]
        assert seeded.status is Status.QUEUED
        assert seeded.via == "migration-4"
        assert seeded.rerun is True
        assert seeded.hash == POST_HASH
        assert seeded.engine == "0.2.0"  # stamped with the running engine, not the old line's
        assert "seeded 1 rerun" in report.actions[0]

    def test_a_post_with_real_prose_is_left_alone(self, tmp_path, migration):
        write_item(tmp_path)
        rel = write_enrichment(tmp_path, ITEM, "x-179456.md", REAL_BODY)
        path = write_ledger(tmp_path, [ledger_entry(path=rel)])
        report = migration.apply(tmp_path)
        assert len(lines(path)) == 1
        assert report.actions == []

    def test_the_username_article_spelling_is_a_stub_too(self, tmp_path, migration):
        write_item(tmp_path)
        body = "@hana — Tue Aug 25\n\nhttps://x.com/hana/article/1900000000000000702"
        rel = write_enrichment(tmp_path, ITEM, "x-179456.md", body)
        path = write_ledger(tmp_path, [ledger_entry(path=rel)])
        migration.apply(tmp_path)
        assert lines(path)[-1].status is Status.QUEUED

    def test_a_repaired_body_never_requalifies(self, tmp_path, migration):
        # THE idempotency that matters: the rerun's repaired body keeps the
        # article link at its tail (harvest reads links out of stored
        # bodies). If "contains an article URL" were the test, a log-race
        # re-application would re-seed repaired work forever; the stub
        # SHAPE test fails on the repaired body's first prose line.
        repaired = (
            "# How to Build a Company Brain\n\n"
            "Right now the smartest AI in your company works for one person.\n\n"
            f"{ARTICLE_URL}"
        )
        write_item(tmp_path)
        rel = write_enrichment(tmp_path, ITEM, "x-179456.md", repaired)
        path = write_ledger(tmp_path, [ledger_entry(path=rel)])
        report = migration.apply(tmp_path)
        assert len(lines(path)) == 1
        assert report.actions == []

    def test_a_prose_post_linking_an_article_is_left_alone(self, tmp_path, migration):
        # A post whose text discusses someone else's article lost nothing —
        # its prose was stored. Re-fetching it repairs nothing.
        body = f"@carol — Tue Aug 25\n\nWorth reading this one closely: {ARTICLE_URL}"
        write_item(tmp_path)
        rel = write_enrichment(tmp_path, ITEM, "x-179456.md", body)
        path = write_ledger(tmp_path, [ledger_entry(path=rel)])
        migration.apply(tmp_path)
        assert len(lines(path)) == 1

    def test_a_media_note_stub_still_qualifies(self, tmp_path, migration):
        # An announcement carrying a cover image: the broken driver wrote
        # attribution, the media note, and the link — still a stub.
        body = f"@hana — Tue Aug 25\n\n(photo post)\n\n{ARTICLE_URL}"
        write_item(tmp_path)
        rel = write_enrichment(tmp_path, ITEM, "x-179456.md", body)
        path = write_ledger(tmp_path, [ledger_entry(path=rel)])
        migration.apply(tmp_path)
        assert lines(path)[-1].status is Status.QUEUED

    def test_another_kinds_page_is_never_a_member(self, tmp_path, migration):
        # A web page quoting an x article URL in its prose is not an x stub.
        write_item(tmp_path)
        rel = write_enrichment(tmp_path, ITEM, "web-179456.md", STUB_BODY)
        path = write_ledger(tmp_path, [ledger_entry(path=rel, kind=Kind.WEB)])
        migration.apply(tmp_path)
        assert len(lines(path)) == 1

    def test_an_entry_with_no_stored_path_is_not_a_member(self, tmp_path, migration):
        # This is also how every non-done status stays out: outputs are
        # success-only, so a park or a skip has no stored file to judge.
        write_item(tmp_path)
        path = write_ledger(tmp_path, [ledger_entry(path=None)])
        migration.apply(tmp_path)
        assert len(lines(path)) == 1


class TestSafety:
    def test_work_no_live_item_claims_is_refused_with_why(self, tmp_path, migration):
        # No corpus file was written: the item was purged or renamed away.
        rel = write_enrichment(tmp_path, ITEM, "x-179456.md", STUB_BODY)
        path = write_ledger(tmp_path, [ledger_entry(path=rel)])
        report = migration.apply(tmp_path)
        assert len(lines(path)) == 1  # nothing seeded
        assert any("nothing claims this work" in s.why for s in report.skipped)

    def test_a_renamed_items_stub_is_reattributed_not_refused(self, tmp_path, migration):
        renamed = "2026-08-26-company-brain-bca608"
        write_item(tmp_path, item=renamed)  # lists POST_URL under its new id
        rel = write_enrichment(tmp_path, ITEM, "x-179456.md", STUB_BODY)
        path = write_ledger(tmp_path, [ledger_entry(path=rel)])
        report = migration.apply(tmp_path)
        assert lines(path)[-1].item == renamed
        assert "re-attributed" in report.actions[0]

    def test_an_unparseable_line_never_stops_the_sweep(self, tmp_path, migration):
        write_item(tmp_path)
        rel = write_enrichment(tmp_path, ITEM, "x-179456.md", STUB_BODY)
        path = write_ledger(tmp_path, [ledger_entry(path=rel)])
        with path.open("a") as f:
            f.write('{"hash": "torn"}\n')
        report = migration.apply(tmp_path)
        assert lines(path)[-1].status is Status.QUEUED  # the good one was still repaired
        assert any("does not parse" in s.why for s in report.skipped)

    def test_a_missing_ledger_is_nothing_to_do(self, tmp_path, migration):
        assert migration.apply(tmp_path) == migration.apply(tmp_path)


class TestIdempotence:
    def test_a_second_apply_seeds_nothing_further(self, tmp_path, migration):
        write_item(tmp_path)
        rel = write_enrichment(tmp_path, ITEM, "x-179456.md", STUB_BODY)
        path = write_ledger(tmp_path, [ledger_entry(path=rel)])
        migration.apply(tmp_path)
        after_first = path.read_text()
        report = migration.apply(tmp_path)
        assert path.read_text() == after_first
        assert report.actions == []

    def test_the_seed_carries_no_stale_outcome_fields(self, tmp_path, migration):
        # A queued seed that inherited `path`/`title` would claim an output
        # the rerun has not written yet.
        write_item(tmp_path)
        rel = write_enrichment(tmp_path, ITEM, "x-179456.md", STUB_BODY)
        path = write_ledger(
            tmp_path, [ledger_entry(path=rel, title="How to Build a Company Brain")]
        )
        migration.apply(tmp_path)
        seeded = json.loads(path.read_text().strip().split("\n")[-1])
        assert "path" not in seeded
        assert "title" not in seeded
