"""Tests for migration 14: re-point or retire the descriptions migration 13 unmoored."""

import datetime

import pytest

from dex_engine.migrations.migration_14 import build
from dex_engine.pipeline.enrichment import described_file

TODAY = datetime.date(2026, 9, 11)
NOW = datetime.datetime(2026, 9, 11, 9, 0, 0, 500000, tzinfo=datetime.UTC)
ENGINE = "0.2.0"

ITEM = "2026-08-31-a-post-abc123"


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version=ENGINE)


def write_corpus_item(root, item_id=ITEM, media=()):
    path = root / "corpus" / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    listing = "media:\n" + "".join(f"  - {entry}\n" for entry in media) if media else ""
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: manual\nchannel: inbox\nshared_by: alex\ndate: 2026-08-31\n"
        "urls:\n  - https://example.com/post\n"
        f"kinds: [web]\nstatus: raw\nenrichment: []\n{listing}---\n**alex**: note\n",
        encoding="utf-8",
    )
    return path


def write_description(root, names, slot=0, item=ITEM, tail=""):
    path = root / "enrichment" / item / f"media-{slot}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"Describes `{names}`{tail}\n\nthe reading\n", encoding="utf-8")
    return path


def write_download(root, name="media-0.png", item=ITEM):
    path = root / "enrichment" / item / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return path


class TestMembership:
    def test_a_description_whose_file_is_gone_is_retired(self, tmp_path, migration):
        # Nothing in the slot: the bytes were deleted, not renamed.
        write_corpus_item(tmp_path)
        description = write_description(tmp_path, "media-0.png")
        before = description.read_text()
        report = migration.apply(tmp_path)
        retired = description.with_name("discarded-media-0.md")
        assert not description.exists()
        assert retired.read_text() == before
        assert "retired 1 left standing" in report.actions[0]
        assert f"{ITEM}: enrichment/{ITEM}/media-0.md described `media-0.png`" in report.actions[1]
        assert f"retired to enrichment/{ITEM}/discarded-media-0.md" in report.actions[1]

    def test_a_description_whose_download_is_there_is_left_alone(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_download(tmp_path)
        description = write_description(tmp_path, "media-0.png")
        assert migration.apply(tmp_path).actions == []
        assert description.exists()

    def test_a_description_of_carried_capture_media_is_left_alone(self, tmp_path, migration):
        stated = "media/abc123/photo.jpg"
        write_corpus_item(tmp_path, media=(stated,))
        carried = tmp_path / stated
        carried.parent.mkdir(parents=True, exist_ok=True)
        carried.write_bytes(b"\xff\xd8\xff\xe0")
        description = write_description(tmp_path, stated)
        assert migration.apply(tmp_path).actions == []
        assert description.exists()

    def test_a_description_of_capture_media_that_is_gone_is_retired(self, tmp_path, migration):
        stated = "media/abc123/photo.jpg"
        write_corpus_item(tmp_path, media=(stated,))
        description = write_description(tmp_path, stated)
        migration.apply(tmp_path)
        assert not description.exists()
        assert description.with_name("discarded-media-0.md").exists()

    def test_a_pre_verb_description_naming_a_repo_path_is_read(self, tmp_path, migration):
        # Written before the verb existed: the verb's opening, then its own
        # prose, and the full repo path where the verb writes a bare name.
        write_corpus_item(tmp_path)
        description = write_description(
            tmp_path, f"enrichment/{ITEM}/media-0.jpg", tail=" — the file is zero bytes."
        )
        migration.apply(tmp_path)
        assert not description.exists()
        assert description.with_name("discarded-media-0.md").exists()

    def test_a_description_of_capture_media_named_bare_is_left_alone(self, tmp_path, migration):
        # A pre-verb description of a capture's media names the file alone,
        # where the verb writes the path the item states — resolved through
        # the item's own media: listing, or it reads as unmoored.
        stated = "media/abc123/Screenshot.png"
        write_corpus_item(tmp_path, media=(stated,))
        carried = tmp_path / stated
        carried.parent.mkdir(parents=True, exist_ok=True)
        carried.write_bytes(b"\x89PNG\r\n\x1a\n")
        description = write_description(tmp_path, "Screenshot.png")
        assert migration.apply(tmp_path).actions == []
        assert description.exists()

    def test_a_pre_verb_description_whose_repo_path_exists_is_left_alone(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_download(tmp_path, name="media-0.jpg")
        description = write_description(tmp_path, f"enrichment/{ITEM}/media-0.jpg")
        assert migration.apply(tmp_path).actions == []
        assert description.exists()

    def test_a_bare_name_matches_stated_media_by_basename_not_by_existence(
        self, tmp_path, migration
    ):
        # The item carries media, but not the file this description names.
        stated = "media/abc123/other.png"
        write_corpus_item(tmp_path, media=(stated,))
        carried = tmp_path / stated
        carried.parent.mkdir(parents=True, exist_ok=True)
        carried.write_bytes(b"\x89PNG\r\n\x1a\n")
        description = write_description(tmp_path, "missing.png")
        migration.apply(tmp_path)
        assert not description.exists()
        assert description.with_name("discarded-media-0.md").exists()

    def test_a_bare_name_matching_stated_media_that_is_gone_is_retired(self, tmp_path, migration):
        # The basename matches what the item states, and the stated path
        # resolves inside the root — but no file is there.
        write_corpus_item(tmp_path, media=("media/abc123/gone.png",))
        description = write_description(tmp_path, "gone.png")
        migration.apply(tmp_path)
        assert not description.exists()
        assert description.with_name("discarded-media-0.md").exists()

    def test_a_description_naming_nothing_is_left_alone(self, tmp_path, migration):
        # What it covers is not derivable; guessing would destroy a
        # session's writing on a hunch.
        write_corpus_item(tmp_path)
        path = tmp_path / "enrichment" / ITEM / "media-0.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("media-0.png: this file is zero bytes\n", encoding="utf-8")
        assert migration.apply(tmp_path).actions == []
        assert path.exists()

    def test_a_path_escaping_the_root_is_not_carried(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        description = write_description(tmp_path, "../../etc/hosts")
        migration.apply(tmp_path)
        assert not description.exists()

    def test_an_enrichment_dir_with_no_live_item_is_left_alone(self, tmp_path, migration):
        description = write_description(tmp_path, "media-0.png")
        assert migration.apply(tmp_path).actions == []
        assert description.exists()

    def test_a_dead_dir_does_not_stop_the_walk(self, tmp_path, migration):
        # Sorted first, and skipped — the items after it must still be read.
        dead = "2026-08-01-no-such-item-000000"
        write_description(tmp_path, "media-0.png", item=dead)
        write_corpus_item(tmp_path)
        orphan = write_description(tmp_path, "media-0.png")
        migration.apply(tmp_path)
        assert (tmp_path / "enrichment" / dead / "media-0.md").exists()
        assert not orphan.exists()
        assert orphan.with_name("discarded-media-0.md").exists()

    def test_a_carried_description_does_not_stop_the_walk(self, tmp_path, migration):
        # Slot 0 is left alone; slot 1 is the orphan behind it.
        write_corpus_item(tmp_path)
        write_download(tmp_path)
        carried = write_description(tmp_path, "media-0.png", slot=0)
        orphan = write_description(tmp_path, "media-1.png", slot=1)
        migration.apply(tmp_path)
        assert carried.exists()
        assert not orphan.exists()
        assert orphan.with_name("discarded-media-1.md").exists()

    def test_other_enrichment_files_are_never_touched(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        page = tmp_path / "enrichment" / ITEM / "web-abc123.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\nurl: https://example.com/post\n---\nbody\n", encoding="utf-8")
        write_description(tmp_path, "media-0.png")
        migration.apply(tmp_path)
        assert page.exists()


class TestSafety:
    def test_a_taken_retired_name_is_an_anomaly_not_a_guess(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        description = write_description(tmp_path, "media-0.png")
        standing = description.with_name("discarded-media-0.md")
        standing.write_text("an earlier retirement\n", encoding="utf-8")
        report = migration.apply(tmp_path)
        assert description.exists()
        assert standing.read_text() == "an earlier retirement\n"
        assert report.actions == []
        assert f"enrichment/{ITEM}/discarded-media-0.md already exists" in report.anomalies[0]

    def test_an_anomaly_and_a_retirement_travel_together(self, tmp_path, migration):
        # One slot blocked does not swallow the report of the one that moved.
        write_corpus_item(tmp_path)
        blocked = write_description(tmp_path, "media-0.png", slot=0)
        blocked.with_name("discarded-media-0.md").write_text("standing\n", encoding="utf-8")
        moved = write_description(tmp_path, "media-1.png", slot=1)
        report = migration.apply(tmp_path)
        assert "retired 1 left standing" in report.actions[0]
        assert not moved.exists()
        assert blocked.exists()
        assert len(report.anomalies) == 1
        assert f"enrichment/{ITEM}/discarded-media-0.md already exists" in report.anomalies[0]

    def test_idempotent(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_description(tmp_path, "media-0.png")
        first = migration.apply(tmp_path)
        second = migration.apply(tmp_path)
        assert len(first.actions) == 2
        assert second.actions == []
        assert second.anomalies == []

    def test_an_instance_with_no_enrichment_tree_is_a_no_op(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.anomalies == []

    def test_every_orphan_across_items_and_slots_is_retired(self, tmp_path, migration):
        other = "2026-08-30-another-def456"
        write_corpus_item(tmp_path)
        write_corpus_item(tmp_path, item_id=other)
        write_description(tmp_path, "media-0.png", slot=0)
        write_description(tmp_path, "media-1.png", slot=1)
        write_description(tmp_path, "media-0.png", slot=0, item=other)
        report = migration.apply(tmp_path)
        assert "retired 3 left standing" in report.actions[0]
        for item, slot in ((ITEM, 0), (ITEM, 1), (other, 0)):
            assert (tmp_path / "enrichment" / item / f"discarded-media-{slot}.md").exists()


class TestRepoint:
    """A renamed file keeps its reading; only the name has to move."""

    def test_a_description_of_a_renamed_file_is_re_pointed(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_download(tmp_path, name="media-0.png")
        description = write_description(tmp_path, "media-0.jpg", tail=" (12 KB) — a diagram.")
        report = migration.apply(tmp_path)
        assert description.read_text() == (
            "Describes `media-0.png` (12 KB) — a diagram.\n\nthe reading\n"
        )
        assert "re-pointed 1 description(s)" in report.actions[0]
        assert "which migration 13 renamed to media-0.png" in report.actions[1]

    def test_a_repo_path_description_is_re_pointed_to_the_bare_name(self, tmp_path, migration):
        # The spelling the verb writes and --of takes, so describe finds it.
        write_corpus_item(tmp_path)
        write_download(tmp_path, name="media-0.png")
        description = write_description(tmp_path, f"enrichment/{ITEM}/media-0.jpg")
        migration.apply(tmp_path)
        assert description.read_text().startswith("Describes `media-0.png`\n")

    def test_only_the_name_moves_not_every_backtick_on_the_line(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_download(tmp_path, name="media-0.png")
        path = tmp_path / "enrichment" / ITEM / "media-0.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "Describes `media-0.jpg` — the shot referenced in `notes.md`.\n\nthe reading\n",
            encoding="utf-8",
        )
        migration.apply(tmp_path)
        assert path.read_text() == (
            "Describes `media-0.png` — the shot referenced in `notes.md`.\n\nthe reading\n"
        )

    def test_only_the_first_line_moves(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_download(tmp_path, name="media-0.png")
        path = tmp_path / "enrichment" / ITEM / "media-0.md"
        path.write_text(
            "Describes `media-0.jpg`\n\nA body mentioning `media-0.jpg` twice: `media-0.jpg`.\n",
            encoding="utf-8",
        )
        migration.apply(tmp_path)
        assert path.read_text() == (
            "Describes `media-0.png`\n\nA body mentioning `media-0.jpg` twice: `media-0.jpg`.\n"
        )

    def test_a_slot_holding_more_than_one_media_file_is_an_anomaly(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_download(tmp_path, name="media-0.png")
        write_download(tmp_path, name="media-0.webp")
        description = write_description(tmp_path, "media-0.jpg")
        before = description.read_text()
        report = migration.apply(tmp_path)
        assert description.read_text() == before
        assert report.actions == []
        assert "holds 2 media files (media-0.png, media-0.webp)" in report.anomalies[0]

    def test_a_re_pointed_description_is_found_by_the_describe_verb(self, tmp_path, migration):
        # The whole point of the re-point: the tie is restored.
        write_corpus_item(tmp_path)
        write_download(tmp_path, name="media-0.png")
        description = write_description(tmp_path, "media-0.jpg")
        migration.apply(tmp_path)
        assert described_file(description) == "media-0.png"

    def test_re_points_and_retirements_travel_together(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_download(tmp_path, name="media-0.png")
        write_description(tmp_path, "media-0.jpg", slot=0)
        retiring = write_description(tmp_path, "media-1.png", slot=1)
        report = migration.apply(tmp_path)
        assert "re-pointed 1 description(s)" in report.actions[0]
        assert "retired 1 left standing" in report.actions[0]
        assert not retiring.exists()

    def test_every_re_point_is_counted(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_download(tmp_path, name="media-0.png")
        write_download(tmp_path, name="media-1.webp")
        write_description(tmp_path, "media-0.jpg", slot=0)
        write_description(tmp_path, "media-1.png", slot=1)
        report = migration.apply(tmp_path)
        assert "re-pointed 2 description(s)" in report.actions[0]
        assert len(report.actions) == 3

    def test_idempotent_after_a_re_point(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_download(tmp_path, name="media-0.png")
        write_description(tmp_path, "media-0.jpg")
        migration.apply(tmp_path)
        assert migration.apply(tmp_path).actions == []
