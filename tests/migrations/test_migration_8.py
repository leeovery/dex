"""Tests for migration 8: materialize backfill attachments into media/."""

import dataclasses
import datetime
import json
import shutil

import pytest

from dex_engine import corpus
from dex_engine.migrations.migration_8 import build
from dex_engine.normalize import run_normalize
from dex_engine.pipeline.types import Config, Instance

TODAY = datetime.date(2026, 8, 28)
NOW = datetime.datetime(2026, 8, 28, 9, 0, 0, 500000, tzinfo=datetime.UTC)

SHORTID = "55ad7b"
ITEM = f"2026-08-27-example-{SHORTID}"
REL = f"corpus/2026/{ITEM}.md"
# Two more ids, either side of ITEM in the walk's sort order.
QUIET = "2026-08-27-aaa-quiet-111111"
SECOND = "2026-08-27-second-99cc11"
DISCORD = "raw/discord/general/assets/photo.png"
BODY = "\nthe original message text, verbatim\n"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.0")


def write_item(  # noqa: PLR0913 — a fixture builder mirrors the item's fields
    root,
    *,
    item_id=ITEM,
    source="discord",
    attachments=(),
    media=(),
    status="raw",
    enrichment=(),
    body=BODY,
):
    path = root / "corpus" / item_id[:4] / f"{item_id}.md"
    corpus.write_item(
        path,
        corpus.CorpusItem(
            id=item_id,
            source=source,
            channel="general",
            shared_by="Alex",
            date=datetime.date(2026, 8, 27),
            kinds=["text"],
            attachments=list(attachments),
            status=status,
            enrichment=list(enrichment),
            media=list(media),
            body=body,
        ),
    )
    return path


def write_raw(root, repo_path, data=b"png-bytes"):
    path = root / repo_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def fingerprint(path):
    stat = path.stat()
    return (stat.st_ino, stat.st_mtime_ns)


class TestApply:
    def test_a_backfill_item_is_materialized_and_listed(self, tmp_path, migration):
        # The field defect: attachments: was write-only, so the pipeline
        # never saw the files — never extracted, never in a digest.
        notes = "raw/discord/general/assets/notes.pdf"
        write_raw(tmp_path, DISCORD)
        write_raw(tmp_path, notes, b"%PDF-")
        path = write_item(tmp_path, attachments=[DISCORD, notes])
        report = migration.apply(tmp_path)
        media = [f"media/{SHORTID}/photo.png", f"media/{SHORTID}/notes.pdf"]
        assert corpus.read_item(path).media == media
        assert (tmp_path / media[0]).read_bytes() == b"png-bytes"
        assert (tmp_path / media[1]).read_bytes() == b"%PDF-"
        assert report.actions == [
            "media: 2 attachment(s) copied out of raw/",
            "corpus: 1 item(s) rewritten — media: derived from attachments:",
        ]

    def test_an_item_with_nothing_to_do_never_ends_the_walk(self, tmp_path, migration):
        # The attachment-free item sorts first: skipping it must move to the
        # next item, not abandon every item behind it.
        write_raw(tmp_path, DISCORD)
        write_item(tmp_path, item_id=QUIET)
        first = write_item(tmp_path, attachments=[DISCORD])
        second = write_item(tmp_path, item_id=SECOND, attachments=[DISCORD])
        report = migration.apply(tmp_path)
        assert corpus.read_item(first).media == [f"media/{SHORTID}/photo.png"]
        assert corpus.read_item(second).media == ["media/99cc11/photo.png"]
        assert report.actions == [
            "media: 2 attachment(s) copied out of raw/",
            "corpus: 2 item(s) rewritten — media: derived from attachments:",
        ]

    def test_a_gspace_item_no_normalize_walk_reaches_is_healed(self, tmp_path, migration):
        # Keying off the frontmatter, not off raw/discord/ on disk, is what
        # heals these: gspace has no export for normalize to walk at all.
        write_raw(tmp_path, "raw/gspace/room/notes.pdf", b"%PDF-")
        path = write_item(tmp_path, source="gspace", attachments=["raw/gspace/room/notes.pdf"])
        migration.apply(tmp_path)
        rel = f"media/{SHORTID}/notes.pdf"
        assert corpus.read_item(path).media == [rel]
        assert (tmp_path / rel).read_bytes() == b"%PDF-"

    def test_an_already_converged_item_is_not_rewritten(self, tmp_path, migration):
        rel = f"media/{SHORTID}/photo.png"
        write_raw(tmp_path, DISCORD)
        write_raw(tmp_path, rel)
        path = write_item(tmp_path, attachments=[DISCORD], media=[rel])
        before = fingerprint(path)
        report = migration.apply(tmp_path)
        assert fingerprint(path) == before
        assert report.actions == []

    def test_a_second_apply_copies_nothing_and_writes_nothing(self, tmp_path, migration):
        write_raw(tmp_path, DISCORD)
        path = write_item(tmp_path, attachments=[DISCORD])
        migration.apply(tmp_path)
        media = tmp_path / f"media/{SHORTID}/photo.png"
        before = (fingerprint(path), fingerprint(media))
        report = migration.apply(tmp_path)
        assert (fingerprint(path), fingerprint(media)) == before
        assert report.actions == []
        assert report.skipped == []

    def test_a_missing_source_still_lists_the_entry(self, tmp_path, migration):
        # raw/ is not guaranteed to outlive the corpus it produced; the
        # entries stand and the pipeline parks the units that need the bytes.
        gone = ["raw/discord/general/assets/gone.png", "raw/gspace/room/gone.pdf"]
        path = write_item(tmp_path, attachments=gone)
        report = migration.apply(tmp_path)
        media = [f"media/{SHORTID}/gone.png", f"media/{SHORTID}/gone.pdf"]
        assert corpus.read_item(path).media == media
        assert not (tmp_path / "media").exists()
        assert report.actions == [
            "corpus: 1 item(s) rewritten — media: derived from attachments:",
            (
                "media: 2 listed file(s) have no source under raw/ — the entries stand and "
                "the pipeline parks the units that need the bytes"
            ),
        ]

    @pytest.mark.parametrize(
        "escape", ["../outside.png", "state/enrichment-ledger.jsonl", "loose.png"]
    )
    def test_an_attachment_outside_raw_is_dropped_and_named(self, tmp_path, migration, escape):
        # A hand-written backfill path is data — a climb out of the instance,
        # a landing on an engine file, a bare name with no directory at all.
        # Copying the second would make an engine file LFS-tracked pipeline
        # input for the item.
        write_raw(tmp_path, DISCORD)
        path = write_item(tmp_path, attachments=[escape, DISCORD])
        report = migration.apply(tmp_path)
        item = corpus.read_item(path)
        assert item.media == [f"media/{SHORTID}/photo.png"]  # the rest of the item still lands
        assert item.attachments == [escape, DISCORD]  # provenance, verbatim
        assert len(report.skipped) == 1
        assert report.skipped[0].what == REL
        assert report.skipped[0].why == (
            f"attachment {escape!r} resolves outside raw/ — no media entry"
        )

    def test_an_item_without_attachments_is_untouched(self, tmp_path, migration):
        path = write_item(tmp_path)
        before = (path.read_bytes(), fingerprint(path))
        report = migration.apply(tmp_path)
        assert (path.read_bytes(), fingerprint(path)) == before
        assert report.actions == []
        assert not (tmp_path / "media").exists()

    def test_enricher_owned_fields_and_the_body_survive(self, tmp_path, migration):
        write_raw(tmp_path, DISCORD)
        path = write_item(
            tmp_path,
            attachments=[DISCORD],
            status="enriched",
            enrichment=["web-abc123.md"],
        )
        migration.apply(tmp_path)
        after = corpus.read_item(path)
        assert after.status == "enriched"
        assert after.enrichment == ["web-abc123.md"]
        assert after.body == BODY
        assert after.media == [f"media/{SHORTID}/photo.png"]

    @pytest.mark.parametrize("content", [b"\xff\xfe not decodable", b"nothing like frontmatter"])
    def test_an_unreadable_item_is_named_not_fatal(self, tmp_path, migration, content):
        # It sorts ahead of the healthy item: an item this code cannot read
        # is skipped-with-why, never the end of the walk.
        bad = tmp_path / "corpus" / "2025" / "2025-01-01-garbled-aaaaaa.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(content)
        write_raw(tmp_path, DISCORD)
        path = write_item(tmp_path, attachments=[DISCORD])
        report = migration.apply(tmp_path)
        assert corpus.read_item(path).media == [f"media/{SHORTID}/photo.png"]  # the readable one
        assert [entry.what for entry in report.skipped] == [
            "corpus/2025/2025-01-01-garbled-aaaaaa.md"
        ]
        assert "could not be read" in report.skipped[0].why

    def test_no_corpus_directory_is_an_empty_report(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []


class TestConvergence:
    """One derivation, one writer: normalize finds nothing left to change."""

    def test_normalize_over_the_export_rewrites_nothing_after_the_migration(
        self, tmp_path, migration
    ):
        instance = Instance(root=tmp_path)
        write_raw(tmp_path, DISCORD)
        (tmp_path / "raw/discord/general/messages.json").write_text(
            json.dumps(
                {
                    "messages": [
                        {
                            "id": "m1",
                            "type": "Default",
                            "content": "https://example.test/post",
                            "timestamp": "2026-08-19T10:00:00+00:00",
                            "author": {"id": "u1", "name": "alex", "nickname": "Alex"},
                            "reference": None,
                            "attachments": [{"url": "assets/photo.png", "fileName": "photo.png"}],
                            "embeds": [],
                            "reactions": [],
                        }
                    ]
                }
            )
        )
        run_normalize(instance, Config())
        (path,) = tmp_path.glob("corpus/*/*.md")
        # Rewind to the state the migration exists to heal: the item as the
        # engine wrote it before normalize derived media: at all.
        corpus.write_item(path, dataclasses.replace(corpus.read_item(path), media=[]))
        shutil.rmtree(tmp_path / "media")

        migration.apply(tmp_path)
        migrated = path.read_bytes()
        run_normalize(instance, Config())
        assert path.read_bytes() == migrated
