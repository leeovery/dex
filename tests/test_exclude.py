"""Tests for exclude.py: purge + record, surviving re-normalization."""

import datetime
import json
import os
from pathlib import Path

import pytest

from dex_engine.exclude import main, run_exclude
from dex_engine.normalize import load_exclusions
from dex_engine.pipeline import ledger
from dex_engine.pipeline.ownership import work_identity
from dex_engine.pipeline.registry import DRIVERS
from dex_engine.pipeline.types import Instance, Kind, LedgerEntry, Status

ITEM = "2026-08-19-example-55ad7b"
OTHER = "2026-08-19-other-11ff22"
SHARED_URL = "https://example.test/shared-by-two"


def write_item_stub(instance: Instance, item_id: str = ITEM, *, urls: tuple[str, ...] = ()) -> None:
    path = instance.corpus_dir / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    url_lines = "".join(f"  - {url}\n" for url in urls)
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: manual\nchannel: inbox\nshared_by: alex\ndate: 2026-08-19\n"
        + (f"urls:\n{url_lines}" if url_lines else "")
        + "kinds: [web]\nstatus: raw\nenrichment: []\n---\n**alex**: note\n",
        encoding="utf-8",
    )


def ledger_entry(
    instance: Instance,
    unit_hash: str,
    item_id: str,
    *,
    url: str | None = None,
    at: datetime.datetime = datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.UTC),
) -> None:
    ledger.append(
        instance.ledger_path,
        LedgerEntry(
            hash=unit_hash,
            url=url or f"https://example.test/{unit_hash}",
            item=item_id,
            kind=Kind.WEB,
            status=Status.QUEUED,
            engine="0.2.1",
            date=datetime.date(2026, 8, 20),
            at=at,
        ),
    )


class TestRunExclude:
    def test_removes_item_enrichment_and_records_reason(self, instance):
        write_item_stub(instance)
        enrichment = instance.enrichment_dir / ITEM
        enrichment.mkdir(parents=True)
        (enrichment / "web-abc123.md").write_text("fetched")
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert summary == (
            "excluded 1: removed 1 items (0 already gone), 0 digests, 0 ledger entries "
            "dropped, 0 kept (work another live corpus item still claims)"
        )
        assert not (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert not enrichment.exists()
        recorded = (instance.state_dir / "exclusions.tsv").read_text()
        assert recorded == f"{ITEM}\tmeme thread\n"

    def test_the_digest_goes_with_the_item(self, instance):
        # An excluded item that had been digested left `state/digests/<id>.md`
        # behind: a permanent fact index over content ruled permanently out
        # of scope, still feeding query and wiki, and lint's digest check is
        # shape-only so nothing ever named it.
        write_item_stub(instance)
        digest = instance.digests_dir / f"{ITEM}.md"
        digest.parent.mkdir(parents=True, exist_ok=True)
        digest.write_text("# facts\n", encoding="utf-8")
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert not digest.exists()
        assert "1 digests" in summary

    def test_a_digest_symlinked_out_of_the_instance_is_refused(self, instance, tmp_path_factory):
        # The digest deletion obeys the same containment check as the other
        # two: the id is well formed, the file it names is not inside.
        outside = tmp_path_factory.mktemp("outside") / "keepsake.md"
        outside.write_text("not this instance's to delete")
        write_item_stub(instance)
        instance.digests_dir.mkdir(parents=True, exist_ok=True)
        (instance.digests_dir / f"{ITEM}.md").symlink_to(outside)
        with pytest.raises(ValueError, match="outside the instance"):
            run_exclude(instance, [{"id": ITEM, "reason": "out of scope"}])
        assert outside.exists()
        assert (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()

    def test_missing_reason_defaults(self, instance):
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM}])
        assert "\tout of scope\n" in (instance.state_dir / "exclusions.tsv").read_text()

    def test_already_gone_items_counted_not_fatal(self, instance):
        summary = run_exclude(instance, [{"id": ITEM}])
        assert summary == (
            "excluded 1: removed 0 items (1 already gone), 0 digests, 0 ledger entries "
            "dropped, 0 kept (work another live corpus item still claims)"
        )

    def test_re_excluding_never_duplicates_the_record(self, instance):
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        lines = (instance.state_dir / "exclusions.tsv").read_text().splitlines()
        assert lines == [f"{ITEM}\tmeme"]

    def test_entry_without_id_is_loud(self, instance):
        with pytest.raises(ValueError, match="no id"):
            run_exclude(instance, [{"reason": "meme"}])

    def test_normalize_reads_the_record_back(self, instance):
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM}])
        assert load_exclusions(instance.state_dir / "exclusions.tsv") == {"55ad7b"}

    def test_reason_whitespace_collapses_so_the_tsv_stays_parseable(self, instance):
        # A tab or newline in the LLM-authored reason would shear the TSV:
        # a stray line computes a bogus shortid and can silently suppress
        # an unrelated cluster's regeneration.
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM, "reason": "meme\tthread —\nno technical content"}])
        recorded = (instance.state_dir / "exclusions.tsv").read_text()
        assert recorded == f"{ITEM}\tmeme thread — no technical content\n"
        assert load_exclusions(instance.state_dir / "exclusions.tsv") == {"55ad7b"}

    def test_whitespace_only_reason_falls_back_to_the_default(self, instance):
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM, "reason": " \n\t "}])
        assert f"{ITEM}\tout of scope\n" in (instance.state_dir / "exclusions.tsv").read_text()


class TestLedgerPurge:
    def test_only_the_excluded_items_entries_go(self, instance):
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        ledger_entry(instance, "bbbbbbbbbb", ITEM)
        ledger_entry(instance, "cccccccccc", OTHER)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert "2 ledger entries dropped" in summary
        assert set(ledger.load(instance.ledger_path)) == {"cccccccccc"}

    def test_the_exclusions_record_is_still_written(self, instance):
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        recorded = (instance.state_dir / "exclusions.tsv").read_text()
        assert recorded == f"{ITEM}\tmeme thread\n"

    def test_re_running_is_idempotent(self, instance):
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        ledger_entry(instance, "cccccccccc", OTHER)
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        after = instance.ledger_path.read_text()
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert "0 ledger entries dropped" in summary
        assert instance.ledger_path.read_text() == after

    def test_a_missing_ledger_is_not_an_error(self, instance):
        write_item_stub(instance)
        assert "0 ledger entries dropped" in run_exclude(instance, [{"id": ITEM}])
        assert not instance.ledger_path.exists()

    def test_work_another_live_item_still_claims_survives(self, instance):
        # A work unit is keyed by URL hash, not by item: two corpus items
        # listing one URL share one entry, and it names only one of them.
        # Purging on the name deletes the other item's history too.
        shared = work_identity(SHARED_URL, DRIVERS)
        write_item_stub(instance, ITEM, urls=(SHARED_URL,))
        write_item_stub(instance, OTHER, urls=(SHARED_URL,))
        ledger_entry(instance, shared, ITEM, url=SHARED_URL)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert set(ledger.load(instance.ledger_path)) == {shared}
        assert "0 ledger entries dropped" in summary
        assert "1 kept" in summary

    def test_the_purged_item_cannot_claim_its_own_work(self, instance):
        # The corpus is scanned after the deletions: reading it first would
        # find the item about to go still listing the URL, and the purge
        # would veto itself.
        url = "https://example.test/only-this-item"
        write_item_stub(instance, ITEM, urls=(url,))
        ledger_entry(instance, work_identity(url, DRIVERS), ITEM, url=url)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert ledger.load(instance.ledger_path) == {}
        assert "1 ledger entries dropped, 0 kept" in summary

    def test_a_superseded_line_naming_another_item_goes_with_its_hash(self, instance):
        # The unit going is the hash, audit trail included — leaving an older
        # line behind keeps the ghost visible to every raw grep of the file.
        write_item_stub(instance, OTHER)
        ledger_entry(instance, "aaaaaaaaaa", OTHER)
        ledger_entry(
            instance,
            "aaaaaaaaaa",
            ITEM,
            at=datetime.datetime(2026, 8, 20, 10, 0, tzinfo=datetime.UTC),
        )
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert instance.ledger_path.read_text() == ""

    def test_the_whole_hashs_history_goes_when_nothing_claims_it(self, instance):
        # The audit trail belongs to the hash: leaving superseded lines keeps
        # the ghost visible to every raw grep of the file.
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        ledger_entry(
            instance,
            "aaaaaaaaaa",
            ITEM,
            at=datetime.datetime(2026, 8, 20, 10, 0, tzinfo=datetime.UTC),
        )
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert instance.ledger_path.read_text() == ""

    def test_a_hash_re_parented_to_a_live_item_is_kept_whole(self, instance):
        # The live line decides: an older line naming the excluded item is a
        # re-parenting's history, and it retires at `compact` like any
        # superseded line.
        write_item_stub(instance)
        write_item_stub(instance, OTHER)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        ledger_entry(
            instance,
            "aaaaaaaaaa",
            OTHER,
            at=datetime.datetime(2026, 8, 20, 10, 0, tzinfo=datetime.UTC),
        )
        before = instance.ledger_path.read_text()
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}])
        assert instance.ledger_path.read_text() == before

    def test_an_unreadable_line_is_kept_never_purged_by_accident(self, instance):
        # A purge must not become incidental data loss: a line this code
        # cannot parse is not provably the excluded item's.
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        with instance.ledger_path.open("a", encoding="utf-8") as f:
            f.write("{torn\n")
        run_exclude(instance, [{"id": ITEM}])
        assert instance.ledger_path.read_text() == "{torn\n"


class TestBadIdsAreRefused:
    """An id is a corpus item id, never a path — `exclude` deletes recursively."""

    def victim(self, tmp_path_factory) -> Path:
        outside = tmp_path_factory.mktemp("outside") / "VICTIM"
        outside.mkdir()
        (outside / "keepsake.txt").write_text("not this instance's to delete")
        return outside

    def test_an_absolute_path_id_is_refused_and_removes_nothing(self, instance, tmp_path_factory):
        # `Path / "/abs/path"` discards the left operand: the id would have
        # been rmtree'd where it points, and recorded verbatim in the TSV.
        victim = self.victim(tmp_path_factory)
        with pytest.raises(ValueError, match="not a corpus item id"):
            run_exclude(instance, [{"id": str(victim), "reason": "out of scope"}])
        assert (victim / "keepsake.txt").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()

    def test_a_traversal_id_is_refused_and_removes_nothing(self, instance, tmp_path_factory):
        victim = self.victim(tmp_path_factory)
        climb = os.path.relpath(victim, instance.enrichment_dir)
        with pytest.raises(ValueError, match="not a corpus item id"):
            run_exclude(instance, [{"id": climb, "reason": "out of scope"}])
        assert (victim / "keepsake.txt").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()

    def test_an_enrichment_symlink_out_of_the_instance_is_refused(self, instance, tmp_path_factory):
        # The id is a perfectly well-formed one; the directory it names is
        # not inside the instance. Resolving both sides is what sees that.
        victim = self.victim(tmp_path_factory)
        write_item_stub(instance)
        (instance.enrichment_dir / ITEM).symlink_to(victim, target_is_directory=True)
        with pytest.raises(ValueError, match="outside the instance"):
            run_exclude(instance, [{"id": ITEM, "reason": "out of scope"}])
        assert (victim / "keepsake.txt").exists()
        assert (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()

    def test_a_well_formed_id_still_removes_its_enrichment(self, instance):
        write_item_stub(instance)
        enrichment = instance.enrichment_dir / ITEM
        enrichment.mkdir(parents=True)
        (enrichment / "web-abc123.md").write_text("fetched")
        run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert not enrichment.exists()
        assert not (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()


class TestADuplicateIdInsideOneBatch:
    """The same id twice is one exclusion, and the counts say so."""

    def test_the_permanent_record_is_written_once(self, instance):
        # `state/exclusions.tsv` is a permanent record: the second copy of
        # an id appended an identical row because the existing-ids set was
        # read before the loop and never updated inside it.
        write_item_stub(instance)
        run_exclude(instance, [{"id": ITEM, "reason": "meme"}, {"id": ITEM, "reason": "meme"}])
        lines = (instance.state_dir / "exclusions.tsv").read_text().splitlines()
        assert lines == [f"{ITEM}\tmeme"]

    def test_the_counts_tell_the_truth_about_what_was_gone(self, instance):
        # The summary is the batch's only signal, and it read "1 already
        # gone" for an item the same batch had just removed: the second
        # copy found the file the first copy had deleted.
        write_item_stub(instance)
        summary = run_exclude(instance, [{"id": ITEM}, {"id": ITEM}])
        assert summary == (
            "excluded 1 (1 duplicate id(s) collapsed): removed 1 items (0 already gone), "
            "0 digests, 0 ledger entries dropped, 0 kept (work another live corpus item "
            "still claims)"
        )

    def test_a_genuinely_absent_item_is_still_counted_gone(self, instance):
        # The other half: collapsing copies must not stop `already gone`
        # meaning what it says for an id whose file really is missing.
        write_item_stub(instance)
        summary = run_exclude(instance, [{"id": ITEM}, {"id": OTHER}, {"id": OTHER}])
        assert summary.startswith(
            "excluded 2 (1 duplicate id(s) collapsed): removed 1 items (1 already gone), "
        )


class TestTheBatchIsRefusedWhole:
    """One bad entry refuses the batch: a half-applied one is worse than none."""

    def test_a_wrongly_typed_id_mid_batch_leaves_no_trace(self, instance):
        # Entry 1's TSV record was permanent while its ledger purge never
        # ran (that comes after the loop), and entry 3 was lost silently.
        write_item_stub(instance)
        write_item_stub(instance, OTHER)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        before = instance.ledger_path.read_text()
        with pytest.raises(ValueError, match="999"):
            run_exclude(
                instance,
                [
                    {"id": ITEM, "reason": "meme thread"},
                    {"id": 999, "reason": "meme thread"},
                    {"id": OTHER, "reason": "meme thread"},
                ],
            )
        assert (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert (instance.corpus_dir / "2026" / f"{OTHER}.md").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()
        assert instance.ledger_path.read_text() == before

    @pytest.mark.parametrize("reason", [None, 7, ["meme thread"]])
    def test_a_non_string_reason_refuses_the_batch(self, instance, reason):
        write_item_stub(instance)
        with pytest.raises(ValueError, match="must be a string"):
            run_exclude(instance, [{"id": ITEM, "reason": reason}])
        assert (instance.corpus_dir / "2026" / f"{ITEM}.md").exists()
        assert not (instance.state_dir / "exclusions.tsv").exists()

    def test_an_entry_that_is_not_an_object_is_refused(self, instance):
        # Unreachable through the CLI, which parses the file first, but
        # `run_exclude` is called directly too and deletes either way.
        with pytest.raises(ValueError, match="not an object"):
            run_exclude(instance, [ITEM])  # ty: ignore[invalid-argument-type]
        assert not (instance.state_dir / "exclusions.tsv").exists()


class TestUnreadableCorpusFiles:
    def test_an_unreadable_corpus_file_holds_the_purge_and_says_so(self, instance):
        # The claimed-hash veto is asked of the corpus, and a file that
        # will not parse claims nothing: the shared hash's whole history
        # would go, reported as a clean drop with no mention of the file.
        shared = work_identity(SHARED_URL, DRIVERS)
        write_item_stub(instance, ITEM, urls=(SHARED_URL,))
        write_item_stub(instance, OTHER, urls=(SHARED_URL,))
        (instance.corpus_dir / "2026" / f"{OTHER}.md").write_text("no frontmatter here\n")
        ledger_entry(instance, shared, ITEM, url=SHARED_URL)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert set(ledger.load(instance.ledger_path)) == {shared}
        assert "0 ledger entries dropped" in summary
        assert "1 corpus file(s) could not be read" in summary

    def test_a_readable_corpus_still_purges(self, instance):
        write_item_stub(instance)
        ledger_entry(instance, "aaaaaaaaaa", ITEM)
        summary = run_exclude(instance, [{"id": ITEM, "reason": "meme thread"}])
        assert "1 ledger entries dropped" in summary
        assert "could not be read" not in summary


class TestCli:
    def test_main_excludes_from_a_file(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        write_item_stub(instance)
        payload = instance.root / "exclusions.json"
        payload.write_text(json.dumps([{"id": ITEM, "reason": "meme"}]))
        main([str(payload)])
        assert "excluded 1: removed 1 items" in capsys.readouterr().out

    def test_missing_file_is_loud(self, instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        with pytest.raises(SystemExit) as excinfo:
            main(["absent.json"])
        assert "dex-exclude" in str(excinfo.value)

    def test_non_list_payload_is_loud(self, instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        payload = instance.root / "exclusions.json"
        payload.write_text('{"id": "x"}')
        with pytest.raises(SystemExit) as excinfo:
            main([str(payload)])
        assert "JSON list" in str(excinfo.value)

    def test_file_argument_is_required(self):
        with pytest.raises(SystemExit):
            main([])
