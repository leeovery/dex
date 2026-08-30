"""Tests for pipeline/capture.py: `enrich item new` — both id paths — and the writer."""

import datetime
import hashlib

import pytest

from dex_engine import corpus, inbox
from dex_engine.pipeline.capture import item_new, parse_capture, write_capture
from dex_engine.pipeline.registry import default_drivers
from tests.capabilities.conftest import fixture_bytes

DRIVERS = default_drivers()

TODAY = datetime.date(2026, 8, 20)
NOW = datetime.datetime(2026, 8, 20, 10, 15, 30, tzinfo=datetime.UTC)


def shortid(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:6]  # noqa: S324 — mirrors the id rules


def stage_capture(instance, name: str, text: str):
    path = instance.root / "inbox" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def new(instance, path, **kwargs) -> str:
    return item_new(path, instance=instance, drivers=DRIVERS, today=lambda: TODAY, **kwargs)


def only_item(instance) -> corpus.CorpusItem:
    (path,) = instance.corpus_dir.glob("*/*.md")
    return corpus.read_item(path)


class TestUrlPath:
    def test_link_capture_gets_canonical_url_hash_id(self, instance):
        path = stage_capture(
            instance,
            "20260818-101530.md",
            "https://www.example.test/post?utm_source=x\n\nwhy I saved it\n",
        )
        out = new(instance, path)
        assert out.startswith("created corpus/2026/")
        item = only_item(instance)
        # The id keys on the CANONICAL url (utm stripped, www dropped) but
        # urls: keeps what was shared, verbatim.
        expected = shortid("manual/https://example.test/post")
        assert item.id.endswith(expected)
        assert item.urls == ["https://www.example.test/post?utm_source=x"]
        assert item.kinds == ["web"]
        assert item.source == "inbox"
        assert item.channel == "inbox"
        assert item.status == "raw"
        assert item.date == datetime.date(2026, 8, 18)  # from the capture filename
        assert "why I saved it" in item.body
        assert item.body.startswith("https://")  # the capture body, verbatim

    def test_slug_derives_from_the_note(self, instance):
        path = stage_capture(
            instance, "20260818-101530.md", "https://example.test/p\n\nGreat RAG evals\n"
        )
        new(instance, path)
        assert "-great-rag-evals-" in only_item(instance).id

    def test_slug_falls_back_to_the_url_tail(self, instance):
        path = stage_capture(
            instance, "20260818-101530.md", "https://example.test/blog/my-great-post\n"
        )
        new(instance, path)
        assert "-my-great-post-" in only_item(instance).id

    def test_judgment_slug_wins(self, instance):
        path = stage_capture(instance, "20260818-101530.md", "https://example.test/p\n")
        new(instance, path, slug="The Chosen Name!")
        assert "-the-chosen-name-" in only_item(instance).id

    def test_trailing_punctuation_never_creates_duplicate_urls(self, instance):
        # Punctuation strips BEFORE dedupe: sentence-final "…/post." and a
        # bare "…/post" in one note are one URL, recorded once.
        path = stage_capture(
            instance,
            "20260818-101530.md",
            "See https://example.test/post.\nAlso https://example.test/post\n",
        )
        new(instance, path)
        assert only_item(instance).urls == ["https://example.test/post"]

    def test_markdown_delimiters_never_ride_into_the_url(self, instance):
        # Notes carry URLs as `[text](url)` links and `<url>` autolinks; the
        # closing delimiter is the note's markup, not the address, and a
        # captured "…/post)" would seed an unfetchable unit.
        path = stage_capture(
            instance,
            "20260818-101530.md",
            "A [link](https://example.test/post) and <https://example.test/other>\n",
        )
        new(instance, path)
        assert only_item(instance).urls == [
            "https://example.test/post",
            "https://example.test/other",
        ]

    def test_multiple_urls_all_recorded_first_keys_the_id(self, instance):
        path = stage_capture(
            instance,
            "20260818-101530.md",
            "https://youtube.com/watch?v=abc and https://github.com/a/b\n",
        )
        new(instance, path)
        item = only_item(instance)
        assert len(item.urls) == 2
        assert item.kinds == ["github", "youtube"]
        assert item.id.endswith(shortid("manual/https://youtube.com/watch?v=abc"))

    def test_shared_by_is_stamped(self, instance):
        path = stage_capture(instance, "20260818-101530.md", "https://example.test/p\n")
        new(instance, path, shared_by="Alex")
        assert only_item(instance).shared_by == "Alex"


class TestNoteOnly:
    def test_note_only_capture_keys_on_the_filename(self, instance):
        path = stage_capture(
            instance, "20260818-101530.md", "a standalone observation worth keeping\n"
        )
        out = new(instance, path)
        # Nothing to fetch — the confirmation must not promise a fetch.
        assert "fetches its sources" not in out
        assert "describe/digest" in out
        item = only_item(instance)
        assert item.kinds == ["text"]
        assert item.urls == []
        assert item.id.endswith(shortid("manual/20260818-101530.md"))
        assert item.body == "a standalone observation worth keeping\n"

    def test_unstamped_filename_dates_today(self, instance):
        path = stage_capture(instance, "loose-note.md", "an observation\n")
        new(instance, path)
        assert only_item(instance).date == TODAY


class TestMediaPath:
    def test_media_capture_uses_the_materialized_directory_id(self, instance):
        media_rel = "media/1ba269/20260818-101530.jpg"
        dest = instance.root / media_rel
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"\xff\xd8\xff\xe0 jpeg")
        path = stage_capture(
            instance,
            "20260818-101530.md",
            f"---\nmedia: {media_rel}\n---\n\nthe note\n",
        )
        new(instance, path)
        item = only_item(instance)
        assert item.id.endswith("1ba269")
        assert item.media == [media_rel]
        assert item.kinds == ["image"]
        assert item.body == "the note\n"

    def test_document_media_is_kind_file(self, instance):
        media_rel = "media/abc123/report.docx"
        dest = instance.root / media_rel
        dest.parent.mkdir(parents=True)
        dest.write_bytes(fixture_bytes("report.docx"))
        path = stage_capture(
            instance, "20260818-101530.md", f"---\nmedia: {media_rel}\n---\n\nnote\n"
        )
        out = new(instance, path)
        assert "fetches its sources" in out  # document media DOES seed a file unit
        assert only_item(instance).kinds == ["file"]

    def test_unmaterialized_pointer_is_loud(self, instance):
        path = stage_capture(
            instance,
            "20260818-101530.md",
            "---\nmedia: somewhere/else.jpg\n---\n\nnote\n",
        )
        with pytest.raises(ValueError, match="dex inbox"):
            new(instance, path)

    def test_a_still_staged_asset_is_refused_never_silently_dropped(self, instance):
        # asset:/name: frontmatter means `dex inbox` has not run yet.
        # Creating a text item here loses the binary's provenance at exit 0.
        path = stage_capture(
            instance,
            "20260818-101530.md",
            "---\nasset: https://api.github.com/repos/o/r/releases/assets/123\n"
            "name: 20260818-101530.jpg\n---\n\nnote\n",
        )
        with pytest.raises(ValueError, match="staged asset"):
            new(instance, path)
        assert not list(instance.corpus_dir.glob("*/*.md"))

    def test_a_name_only_pointer_is_refused_too(self, instance):
        path = stage_capture(
            instance, "20260818-101530.md", "---\nname: 20260818-101530.jpg\n---\n\nnote\n"
        )
        with pytest.raises(ValueError, match="dex inbox"):
            new(instance, path)


class TestGuards:
    def test_existing_item_is_refused(self, instance):
        path = stage_capture(instance, "20260818-101530.md", "https://example.test/p\n")
        new(instance, path)
        with pytest.raises(ValueError, match="already exists"):
            new(instance, path)

    def test_created_item_round_trips_through_corpus(self, instance):
        path = stage_capture(instance, "20260818-101530.md", "https://example.test/p\n\nnote\n")
        new(instance, path)
        item = only_item(instance)  # read_item validates the schema
        assert item.enrichment == []


class TestParseCapture:
    def test_shared_with_inbox(self):
        # inbox.py imports THIS parse_capture — one reader for one format.
        assert inbox.parse_capture is parse_capture


class TestWriteCapture:
    def test_writes_the_url_and_the_note_at_a_stamped_filename(self, instance):
        path = write_capture(instance, url="https://example.test/p", note="why I saved it", now=NOW)
        assert path == instance.root / "inbox" / "20260820-101530.md"
        assert path.read_text(encoding="utf-8") == "https://example.test/p\n\nwhy I saved it\n"

    def test_the_inbox_is_created_when_a_young_instance_has_none(self, instance):
        assert not (instance.root / "inbox").exists()
        write_capture(instance, url="https://example.test/post", note="", now=NOW)
        assert (instance.root / "inbox").is_dir()

    def test_a_note_only_capture_is_the_note_alone(self, instance):
        path = write_capture(instance, url="", note="a standalone observation", now=NOW)
        assert path.read_text(encoding="utf-8") == "a standalone observation\n"

    def test_a_link_with_no_note_is_the_url_alone(self, instance):
        path = write_capture(instance, url="https://example.test/post", note="", now=NOW)
        assert path.read_text(encoding="utf-8") == "https://example.test/post\n"

    def test_surrounding_whitespace_never_reaches_the_file(self, instance):
        path = write_capture(instance, url="  https://example.test/p\n", note="\n note \n", now=NOW)
        assert path.read_text(encoding="utf-8") == "https://example.test/p\n\nnote\n"

    @pytest.mark.parametrize(("url", "note"), [("", ""), ("   ", "\n\t ")])
    def test_neither_a_url_nor_a_note_is_refused(self, instance, url, note):
        with pytest.raises(ValueError, match="a URL, a note, or both"):
            write_capture(instance, url=url, note=note, now=NOW)
        assert list((instance.root / "inbox").glob("*.md")) == []

    def test_a_taken_stamp_walks_forward_a_second(self, instance):
        first = write_capture(instance, url="", note="first", now=NOW)
        second = write_capture(instance, url="", note="second", now=NOW)
        assert first.name == "20260820-101530.md"
        assert second.name == "20260820-101531.md"
        assert first.read_text(encoding="utf-8") == "first\n"
        assert second.read_text(encoding="utf-8") == "second\n"

    def test_the_walk_only_steps_over_what_is_taken(self, instance):
        (instance.root / "inbox").mkdir()
        (instance.root / "inbox" / "20260820-101531.md").write_text("someone else's\n")
        write_capture(instance, url="", note="mine", now=NOW)
        second = write_capture(instance, url="", note="also mine", now=NOW)
        assert second.name == "20260820-101532.md"

    def test_a_full_minute_of_taken_stamps_is_refused(self, instance):
        for second in range(60):
            write_capture(instance, url="", note=f"note {second}", now=NOW)
        with pytest.raises(ValueError, match="every second from 20260820-101530"):
            write_capture(instance, url="", note="one too many", now=NOW)
        assert len(list((instance.root / "inbox").glob("*.md"))) == 60

    def test_what_is_written_is_what_item_new_reads_back(self, instance):
        # The round trip that matters: the writer's file, through the reader
        # the processor actually uses, is the item the owner meant.
        path = write_capture(
            instance,
            url="https://www.example.test/post?utm_source=x",
            note="why I saved it",
            now=NOW,
        )
        new(instance, path)
        item = only_item(instance)
        assert item.urls == ["https://www.example.test/post?utm_source=x"]
        assert item.kinds == ["web"]
        assert item.body == "https://www.example.test/post?utm_source=x\n\nwhy I saved it\n"
        assert item.date == NOW.date()  # the filename stamp, not the ambient clock
        assert item.id.endswith(shortid("manual/https://example.test/post"))

    def test_a_note_only_capture_round_trips_as_a_text_item(self, instance):
        path = write_capture(instance, url="", note="a standalone observation", now=NOW)
        out = new(instance, path)
        assert "describe/digest" in out
        item = only_item(instance)
        assert item.kinds == ["text"]
        assert item.urls == []
        assert item.body == "a standalone observation\n"
