"""Tests for pipeline/describe.py: `enrich item describe` — the description writer."""

import datetime

import pytest

from dex_engine import corpus
from dex_engine.pipeline import run as run_mod
from dex_engine.pipeline.describe import DescribeError, item_describe
from dex_engine.pipeline.types import Need, NeedsCapability
from tests.conftest import FakeDriver
from tests.pipeline.test_run import make_ctx

ITEM = "2026-08-19-example-55ad7b"
URL = "https://example.test/post"
PHOTO = "media/55ad7b/photo.jpg"
TEXT = "A whiteboard sketch of the agent loop; legible text: 'plan → act → observe'."


def write_item(instance, item_id=ITEM, *, urls=None, media=None) -> None:
    corpus.write_item(
        instance.corpus_dir / item_id[:4] / f"{item_id}.md",
        corpus.CorpusItem(
            id=item_id,
            source="inbox",
            channel="inbox",
            shared_by="owner",
            date=datetime.date(2026, 8, 19),
            urls=urls or [],
            kinds=["image"],
            media=media or [],
            body="why I saved it\n",
        ),
    )


def read_item(instance, item_id=ITEM) -> corpus.CorpusItem:
    return corpus.read_item(instance.corpus_dir / item_id[:4] / f"{item_id}.md")


def write_media(instance, repo_path: str = PHOTO) -> None:
    path = instance.root / repo_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"jpg")


def write_enrichment(instance, *names: str, item_id: str = ITEM, text: str = "on disk\n") -> None:
    item_dir = instance.enrichment_dir / item_id
    item_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (item_dir / name).write_text(text)


def describe(instance, of: str, text: str = TEXT, *, item_id: str = ITEM) -> str:
    path = instance.cache_dir / "description.md"
    path.write_text(text, encoding="utf-8")
    return item_describe(item_id, of=of, text_path=path, ctx=make_ctx(instance, FakeDriver()))


def description(instance, name: str = "media-0.md", *, item_id: str = ITEM) -> str:
    return (instance.enrichment_dir / item_id / name).read_text(encoding="utf-8")


def descriptions(instance, item_id: str = ITEM) -> list[str]:
    return sorted(path.name for path in (instance.enrichment_dir / item_id).glob("media-*.md"))


class TestWrite:
    def test_writes_the_header_then_the_text(self, instance):
        write_item(instance)
        write_enrichment(instance, "media-0.png")
        assert describe(instance, "media-0.png") == (
            f"wrote enrichment/{ITEM}/media-0.md · describes media-0.png"
        )
        assert description(instance) == f"Describes `media-0.png`\n\n{TEXT}\n"

    def test_surrounding_whitespace_is_not_part_of_the_text(self, instance):
        write_item(instance)
        write_enrichment(instance, "media-0.png")
        describe(instance, "media-0.png", f"\n\n  {TEXT}  \n\n")
        assert description(instance) == f"Describes `media-0.png`\n\n{TEXT}\n"

    def test_takes_the_next_free_slot(self, instance):
        # Slots are counted, never paired: the description of media-0.png
        # lands wherever the numbering has room.
        write_item(instance)
        write_enrichment(instance, "media-0.png", "media-1.png", "media-0.md")
        assert describe(instance, "media-1.png").startswith(f"wrote enrichment/{ITEM}/media-1.md")
        assert descriptions(instance) == ["media-0.md", "media-1.md"]
        assert description(instance, "media-0.md") == "on disk\n"  # the other slot is untouched

    def test_the_lowest_free_slot_wins_over_the_highest_taken(self, instance):
        write_item(instance)
        write_enrichment(instance, "media-0.png", "media-1.png", "media-1.md")
        describe(instance, "media-0.png")
        assert descriptions(instance) == ["media-0.md", "media-1.md"]

    def test_an_item_carrying_three_files_gets_three_slots(self, instance):
        write_item(instance)
        write_enrichment(instance, "media-0.png", "media-1.png", "media-2.png")
        describe(instance, "media-0.png")
        describe(instance, "media-1.png")
        assert describe(instance, "media-2.png").startswith(f"wrote enrichment/{ITEM}/media-2.md")
        assert descriptions(instance) == ["media-0.md", "media-1.md", "media-2.md"]

    def test_accepts_a_stated_capture_path(self, instance):
        # A capture's media has no slot and no enrichment directory: the
        # verb creates the directory and the description takes slot 0.
        write_item(instance, media=[PHOTO])
        write_media(instance)
        assert not (instance.enrichment_dir / ITEM).exists()
        assert (
            describe(instance, PHOTO) == f"wrote enrichment/{ITEM}/media-0.md · describes {PHOTO}"
        )
        assert description(instance) == f"Describes `{PHOTO}`\n\n{TEXT}\n"

    def test_the_write_creates_the_enrichment_tree(self, instance):
        # `dex-new` seeds `enrichment/` with a .gitkeep and nothing after
        # that guards it: the verb creates whatever is missing, as every
        # other state write does.
        write_item(instance, media=[PHOTO])
        write_media(instance)
        instance.enrichment_dir.rmdir()
        describe(instance, PHOTO)
        assert description(instance) == f"Describes `{PHOTO}`\n\n{TEXT}\n"

    def test_accepts_a_stated_markdown_file(self, instance):
        # Nothing transcribes or extracts a markdown file the item carries,
        # so its description is the summary that proves it was read.
        write_item(instance, media=["media/55ad7b/notes.md"])
        write_media(instance, "media/55ad7b/notes.md")
        describe(instance, "media/55ad7b/notes.md")
        assert description(instance).startswith("Describes `media/55ad7b/notes.md`\n")

    def test_resolves_a_pre_rename_id_to_the_live_item(self, instance):
        # The rename moved the enrichment directory with the corpus file;
        # the description lands in it, and the live item is the one refreshed.
        write_item(instance, "2026-08-19-new-slug-55ad7b")
        write_enrichment(instance, "media-0.png", item_id="2026-08-19-new-slug-55ad7b")
        assert describe(instance, "media-0.png", item_id="2026-08-19-old-slug-55ad7b") == (
            "wrote enrichment/2026-08-19-new-slug-55ad7b/media-0.md · describes media-0.png"
        )
        assert read_item(instance, "2026-08-19-new-slug-55ad7b").enrichment == ["media-0.md"]
        assert not (instance.enrichment_dir / "2026-08-19-old-slug-55ad7b").exists()


class TestRewrite:
    """A session revising its reading describes the file again."""

    def test_the_description_of_the_same_file_is_rewritten_in_place(self, instance):
        write_item(instance)
        write_enrichment(instance, "media-0.png", "media-1.png")
        describe(instance, "media-0.png")
        describe(instance, "media-1.png")
        assert describe(instance, "media-0.png", "On a second look, a wiring diagram.") == (
            f"rewrote enrichment/{ITEM}/media-0.md · describes media-0.png"
        )
        assert descriptions(instance) == ["media-0.md", "media-1.md"]
        assert (
            description(instance)
            == "Describes `media-0.png`\n\nOn a second look, a wiring diagram.\n"
        )
        assert description(instance, "media-1.md").endswith(f"\n\n{TEXT}\n")

    def test_a_first_line_naming_no_file_takes_a_fresh_slot(self, instance):
        # A hand-written description names the file in its own words, with
        # nothing to read the name out of; a slot number ties a description
        # to nothing, so the standing reading is left where it is.
        write_item(instance)
        write_enrichment(instance, "media-0.png")
        write_enrichment(instance, "media-0.md", text="Description of media-0.png:\nbold lines\n")
        describe(instance, "media-0.png")
        assert descriptions(instance) == ["media-0.md", "media-1.md"]
        assert description(instance, "media-1.md").startswith("Describes `media-0.png`\n")

    def test_a_pre_verb_description_is_found_by_the_name_its_first_line_carries(self, instance):
        # Written before this verb existed: the same opening, then its own
        # prose. Matched whole it would be passed over, leaving the stale
        # reading beside the new one — both counted, neither marked.
        write_item(instance)
        write_enrichment(instance, "media-0.png")
        write_enrichment(
            instance,
            "media-0.md",
            text="Describes `media-0.png` (23 KB) — which is not an image.\n\nthe old reading\n",
        )
        assert describe(instance, "media-0.png", "A wiring diagram.") == (
            f"rewrote enrichment/{ITEM}/media-0.md · describes media-0.png"
        )
        assert descriptions(instance) == ["media-0.md"]
        assert description(instance) == "Describes `media-0.png`\n\nA wiring diagram.\n"

    def test_a_sibling_that_is_not_text_does_not_block_the_write(self, instance):
        write_item(instance)
        write_enrichment(instance, "media-0.png")
        (instance.enrichment_dir / ITEM / "media-0.md").write_bytes(b"\xff\xfe not utf-8")
        describe(instance, "media-0.png")
        assert descriptions(instance) == ["media-0.md", "media-1.md"]

    def test_a_refused_rewrite_leaves_the_standing_description_alone(self, instance):
        write_item(instance)
        write_enrichment(instance, "media-0.png")
        describe(instance, "media-0.png")
        with pytest.raises(DescribeError):
            describe(instance, "media-0.png", "   ")
        assert description(instance) == f"Describes `media-0.png`\n\n{TEXT}\n"


class TestRefusals:
    """Nothing is written unless the item, the file and the text all hold."""

    def test_an_unknown_item_is_refused(self, instance):
        with pytest.raises(ValueError, match="no corpus item '2026-08-19-typo-ffffff'"):
            describe(instance, "media-0.png", item_id="2026-08-19-typo-ffffff")
        assert not (instance.enrichment_dir / "2026-08-19-typo-ffffff").exists()

    def test_a_whitespace_bearing_id_is_refused_before_it_resolves(self, instance):
        # A pasted list of ids ends in a real one, whose shortid would
        # resolve the whole blob to a live item.
        write_item(instance)
        write_enrichment(instance, "media-0.png")
        with pytest.raises(ValueError, match="no corpus item"):
            describe(instance, "media-0.png", item_id=f"2026-08-19-other-aaaaaa\n{ITEM}")
        assert descriptions(instance) == []

    def test_an_unparseable_item_is_refused(self, instance):
        path = instance.corpus_dir / ITEM[:4] / f"{ITEM}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("no frontmatter here\n")
        write_enrichment(instance, "media-0.png")
        with pytest.raises(DescribeError, match="does not parse"):
            describe(instance, "media-0.png")
        assert descriptions(instance) == []

    @pytest.mark.parametrize(
        "of",
        [
            "media-1.png",  # a download the item does not hold
            "a1b2c3-asset-0.png",  # an extraction asset owes no description
            "media-0.md",  # a description is not media
            "media-0.png.a1b2c3d4.tmp",  # an interrupted download's temp
            f"enrichment/{ITEM}/media-0.png",  # a download is named bare, never by path
            "media/55ad7b/photo.jpg",  # a path the item's media: does not state
            "",
        ],
    )
    def test_a_file_the_item_does_not_carry_is_refused(self, instance, of):
        write_item(instance)
        write_enrichment(instance, "media-0.png", "a1b2c3-asset-0.png", "media-0.png.a1b2c3d4.tmp")
        write_media(instance)
        with pytest.raises(DescribeError, match=f"is not a file {ITEM} carries"):
            describe(instance, of)
        assert descriptions(instance) == []

    def test_a_stated_path_naming_no_file_is_refused(self, instance):
        # The describe queue counts nothing for it either: the gap is the
        # manual unit seeding parks, not describe work.
        write_item(instance, media=[PHOTO])
        with pytest.raises(DescribeError, match="is not a file"):
            describe(instance, PHOTO)

    def test_a_stated_path_escaping_the_root_is_refused(self, instance):
        outside = instance.root.parent / "outside.jpg"
        outside.write_bytes(b"jpg")
        write_item(instance, media=["../outside.jpg"])
        with pytest.raises(DescribeError, match="is not a file"):
            describe(instance, "../outside.jpg")

    @pytest.mark.parametrize("text", ["", "   \n\n"])
    def test_an_empty_description_is_refused(self, instance, text):
        write_item(instance)
        write_enrichment(instance, "media-0.png")
        with pytest.raises(DescribeError, match="the description is empty"):
            describe(instance, "media-0.png", text)
        assert descriptions(instance) == []


class TestFrontmatter:
    """The write is followed by the refresh the hand-written file never got."""

    def test_the_listing_and_status_refresh_in_the_same_call(self, instance):
        # A media capture with no units: raw while its directory is empty,
        # enriched the moment its description lands.
        write_item(instance, media=[PHOTO])
        write_media(instance)
        assert (read_item(instance).status, read_item(instance).enrichment) == ("raw", [])
        describe(instance, PHOTO)
        assert (read_item(instance).status, read_item(instance).enrichment) == (
            "enriched",
            ["media-0.md"],
        )

    def test_an_outstanding_unit_keeps_the_item_raw(self, instance):
        # The status is the ledger's answer, not the listing's: a unit still
        # waiting on a capability holds the whole item out of digest.
        write_item(instance, urls=[URL], media=[PHOTO])
        write_media(instance)
        waiting = NeedsCapability(need=Need.TRANSCRIBE, reason="no captions available")
        run_mod.run(make_ctx(instance, FakeDriver(fetch_fn=lambda _unit: waiting)))
        describe(instance, PHOTO)
        item = read_item(instance)
        assert item.status == "raw"
        assert "media-0.md" in item.enrichment

    def test_the_describe_row_clears(self, instance):
        # The one reading every surface shares: the run report's queue, the
        # status listing and the health check all stop naming the item.
        write_item(instance, media=[PHOTO])
        write_media(instance)
        write_enrichment(instance, "media-0.png")
        assert run_mod.items_owing_descriptions(instance) == [
            {"item": ITEM, "binaries": 2, "described": 0}
        ]
        describe(instance, PHOTO)
        assert run_mod.items_owing_descriptions(instance) == [
            {"item": ITEM, "binaries": 2, "described": 1}
        ]
        describe(instance, "media-0.png")
        assert run_mod.items_owing_descriptions(instance) == []

    def test_a_refresh_the_listing_refuses_is_stated_on_the_confirmation(self, instance):
        # A sibling's hand-written name the corpus schema cannot hold: the
        # description landed, the frontmatter did not, and the line says so
        # rather than reading as a refresh that happened.
        write_item(instance)
        write_enrichment(instance, "media-0.png", "web-abc, def.md")
        line = describe(instance, "media-0.png")
        assert line.startswith(f"wrote enrichment/{ITEM}/media-0.md · describes media-0.png")
        assert "frontmatter NOT refreshed: enrichment entries may not contain" in line
        assert read_item(instance).enrichment == []
