"""Tests for migration 13: repair the media files the unchecked download path kept."""

import datetime
from pathlib import Path

import pytest

from dex_engine import corpus
from dex_engine.migrations import migration_13
from dex_engine.migrations.migration_13 import build
from dex_engine.pipeline import ledger
from dex_engine.pipeline.digest import item_media
from dex_engine.pipeline.types import Instance, Job, Kind, LedgerEntry, Status
from dex_engine.pipeline.urls import work_hash

TODAY = datetime.date(2026, 9, 11)
NOW = datetime.datetime(2026, 9, 11, 9, 0, 0, 500000, tzinfo=datetime.UTC)
ENGINE = "0.2.0"

ITEM = "2026-08-31-carousel-abc123"
POST_URL = "https://www.instagram.com/p/DTestCode1/"
POST_HASH = work_hash(POST_URL)
MEDIA_URL = "https://uuinstagram.com/images/DTestCode1/1"
MEDIA_HASH = work_hash(MEDIA_URL)
FETCHED = datetime.date(2026, 8, 31)

# Real leads: the bytes the field files carry, not stand-ins.
HTML = (
    b'<!DOCTYPE html>\n<html lang="en"><head><title>Instagram</title></head>'
    b'<body><div id="app">Log in</div></body></html>\n'
)
XML = b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>AccessDenied</Code></Error>\n'
JSON = b'{"error": {"message": "media not found", "code": 404}}\n'
AVIF = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64
SVG = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>\n'
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 64
UNSIGNED = b"\x00\x01\x02\x03 nothing this engine knows" + b"\x7f" * 64
# A UTF-16-LE BOM: its first two bytes sit inside the MPEG sync-frame range,
# which is a byte-range guess the signatures-only sniff must not take.
BOM16 = b"\xff\xfe" + "not media at all".encode("utf-16-le")
LFS_POINTER = (
    b"version https://git-lfs.github.com/spec/v1\n"
    b"oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
    b"size 12345\n"
)


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version=ENGINE)


def write_ledger(root, *entries):
    path = root / "state" / "enrichment-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(ledger.to_line(e) + "\n" for e in entries))
    return path


def write_corpus_item(root, item_id=ITEM, urls=(POST_URL,)):
    path = root / "corpus" / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    listing = "".join(f"  - {url}\n" for url in urls)
    path.write_text(
        "---\n"
        f"id: {item_id}\n"
        "source: manual\nchannel: inbox\nshared_by: alex\ndate: 2026-08-31\n"
        f"urls:\n{listing}"
        "kinds: [instagram]\nstatus: raw\nenrichment: []\n---\n**alex**: note\n",
        encoding="utf-8",
    )
    return path


def write_media(root, data, name="media-1.png", item=ITEM):
    path = root / "enrichment" / item / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_description(root, slot=1, item=ITEM):
    path = root / "enrichment" / item / f"media-{slot}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"media-{slot}.png: this file is zero bytes\n", encoding="utf-8")
    return path


def write_digest(root, media, item=ITEM, *, flow=False, body="- a fact about the post\n"):
    """A standing digest: the verb's block form, or a legacy digest's flow form."""
    path = root / "state" / "digests" / f"{item}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    listing = (
        f"media: [{', '.join(media)}]\n"
        if flow
        else "media:\n" + "".join(f"  - {entry}\n" for entry in media)
    )
    head = f"---\nid: {item}\ndate: 2026-08-31\nsignal: keep\ntopics: [instagram]\n"
    path.write_text(f"{head}{listing}---\n{body}", encoding="utf-8")
    return path


def stated_media(path):
    """The block-form media listing a digest states."""
    block = path.read_text().split("\n---\n", 1)[0]
    return {line[4:] for line in block.split("\n") if line.startswith("  - ")}


def page_entry(*, url=POST_URL, item=ITEM, at=NOW, path=None):
    return LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=item,
        kind=Kind.INSTAGRAM,
        status=Status.DONE,
        engine="0.1.4",
        date=FETCHED,
        at=at,
        path=path,
    )


def media_entry(  # noqa: PLR0913 — a fixture builder mirrors the entry's own fields
    *,
    url=MEDIA_URL,
    parent=POST_HASH,
    item=ITEM,
    status=Status.DONE,
    path="__default__",
    at=NOW,
    engine="0.1.4",
    job=Job.MEDIA,
):
    if path == "__default__":
        path = f"enrichment/{item}/media-1.png" if status is Status.DONE else None
    return LedgerEntry(
        hash=work_hash(url),
        url=url,
        item=item,
        kind=Kind.INSTAGRAM,
        status=status,
        engine=engine,
        date=FETCHED,
        at=at,
        job=job,
        parent=parent,
        depth=1,
        attempts=1 if status is Status.BLOCKED else None,
        reason="media URL answered with HTML" if status is Status.BLOCKED else None,
        path=path,
    )


def lines_for(path, unit_hash=MEDIA_HASH):
    return [line for line in path.read_text().splitlines() if f'"hash": "{unit_hash}"' in line]


def seed_lines(path, unit_hash=MEDIA_HASH):
    return [line for line in lines_for(path, unit_hash) if '"queued"' in line]


class TestRequeue:
    def test_an_empty_file_requeues_and_goes(self, tmp_path, migration):
        # The field shape: engine 0.1.4 wrote a 200's empty body to disk
        # and recorded the unit done; the slot has charged a description
        # ever since.
        write_corpus_item(tmp_path)
        file = write_media(tmp_path, b"")
        path = write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert not file.exists()
        live = ledger.load(path)[MEDIA_HASH]
        assert live.status is Status.QUEUED
        assert live.rerun is True
        assert live.via == "migration-13"
        assert live.hash == MEDIA_HASH
        assert live.url == MEDIA_URL
        assert live.item == ITEM
        assert live.kind is Kind.INSTAGRAM
        assert live.job is Job.MEDIA
        assert live.parent == POST_HASH
        assert live.depth == 1
        assert live.path is None
        assert live.engine == ENGINE
        assert live.date == TODAY
        assert live.at == NOW
        assert len(lines_for(path)) == 2  # the done line and the one seed
        assert report.skipped == []
        assert report.anomalies == []
        assert report.actions[0] == (
            "requeued 1 media unit(s) whose file held no media, renamed 0 whose bytes named "
            "another format and re-pointed 0 digest(s) to the new name(s) — the pre-0.1.6 "
            "download path kept whatever a media URL answered with, unread"
        )
        assert report.actions[1].startswith(
            f"{ITEM}: enrichment/{ITEM}/media-1.png held no bytes at all (written by engine 0.1.4)"
        )
        assert "{queued, rerun, via: migration-13}" in report.actions[1]
        assert "re-fetches it under the fixed rules" in report.actions[1]
        assert "media-1.md" not in report.actions[1]
        assert len(report.actions) == 2

    def test_an_html_page_requeues_and_goes(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        file = write_media(tmp_path, HTML)
        path = write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert not file.exists()
        live = ledger.load(path)[MEDIA_HASH]
        assert live.status is Status.QUEUED
        assert live.via == "migration-13"
        assert report.actions[1].startswith(
            f"{ITEM}: enrichment/{ITEM}/media-1.png held HTML, not media bytes"
        )

    @pytest.mark.parametrize(
        ("data", "held"),
        [
            (b"", "no bytes at all"),
            (HTML, "HTML, not media bytes"),
            (XML, "XML, not media bytes"),
            (JSON, "JSON, not media bytes"),
        ],
    )
    def test_the_action_names_what_the_file_held(self, tmp_path, migration, data, held):
        write_corpus_item(tmp_path)
        write_media(tmp_path, data)
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert f"media-1.png held {held} (" in report.actions[1]

    def test_the_seed_lands_before_the_file_goes(self, tmp_path, migration, monkeypatch):
        # An apply killed between the two writes must leave a queued line
        # over a file the drain overwrites — never a done line over nothing.
        write_corpus_item(tmp_path)
        file = write_media(tmp_path, b"")
        path = write_ledger(tmp_path, page_entry(), media_entry())

        def killed(_self):
            raise KeyboardInterrupt

        monkeypatch.setattr(Path, "unlink", killed)
        with pytest.raises(KeyboardInterrupt):
            migration.apply(tmp_path)
        assert file.exists()
        assert ledger.load(path)[MEDIA_HASH].status is Status.QUEUED

    def test_a_standing_description_is_retired_with_the_bytes_it_describes(
        self, tmp_path, migration
    ):
        # A session closed the describe row by describing the empty file.
        # Left in the counted family it can be neither rewritten (the verb
        # takes only a carried file) nor seen (the row compares counts), and
        # a file landing in the slot would inherit it. Renamed, not deleted:
        # the text is the only record of what the URL answered with.
        write_corpus_item(tmp_path)
        write_media(tmp_path, b"")
        description = write_description(tmp_path)
        before = description.read_text()
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        retired = description.with_name("discarded-media-1.md")
        assert not description.exists()
        assert retired.read_text() == before
        line = report.actions[1]
        assert f"retired to enrichment/{ITEM}/discarded-media-1.md" in line
        assert "text untouched" in line

    def test_a_slot_with_no_description_says_nothing_about_one(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_media(tmp_path, b"")
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert "retired" not in report.actions[1]

    def test_a_taken_retired_name_leaves_the_description_alone(self, tmp_path, migration):
        # Two descriptions of one slot is a state this migration did not
        # create; overwriting either would destroy a session's writing.
        write_corpus_item(tmp_path)
        write_media(tmp_path, b"")
        description = write_description(tmp_path)
        standing = description.with_name("discarded-media-1.md")
        standing.write_text("an earlier retirement\n", encoding="utf-8")
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert description.exists()
        assert standing.read_text() == "an earlier retirement\n"
        assert "retired" not in report.actions[1]

    def test_a_requeue_keeps_the_units_lineage(self, tmp_path, migration):
        # parent and depth travel together — the seed carries both, or the
        # line cannot exist.
        write_corpus_item(tmp_path)
        write_media(tmp_path, b"")
        path = write_ledger(tmp_path, page_entry(), media_entry())
        migration.apply(tmp_path)
        live = ledger.load(path)[MEDIA_HASH]
        assert live.parent == POST_HASH
        assert live.depth == 1


class TestRename:
    def test_avif_bytes_under_a_png_name_are_renamed(self, tmp_path, migration):
        # The field shape: a CDN content-negotiated AVIF for a .png URL,
        # and the old path named the file from the URL.
        write_corpus_item(tmp_path)
        file = write_media(tmp_path, AVIF)
        path = write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        renamed = file.with_suffix(".avif")
        assert not file.exists()
        assert renamed.read_bytes() == AVIF
        live = ledger.load(path)[MEDIA_HASH]
        assert live.status is Status.DONE
        assert live.path == f"enrichment/{ITEM}/media-1.avif"
        assert live.via == "migration-13"
        assert live.rerun is False
        assert live.hash == MEDIA_HASH
        assert live.url == MEDIA_URL
        assert live.item == ITEM
        assert live.kind is Kind.INSTAGRAM
        assert live.job is Job.MEDIA
        assert live.parent == POST_HASH
        assert live.depth == 1
        assert live.engine == ENGINE
        assert live.at == NOW
        assert live.date == FETCHED  # the same landing under a new name, not a new one
        assert len(lines_for(path)) == 2  # the old done line and the re-pointed one
        assert report.skipped == []
        assert report.anomalies == []
        assert report.actions == [
            (
                "requeued 0 media unit(s) whose file held no media, renamed 1 whose bytes named "
                "another format and re-pointed 0 digest(s) to the new name(s) — the pre-0.1.6 "
                "download path kept whatever a media URL answered with, unread"
            )
        ]

    def test_svg_under_a_png_name_is_media_and_renamed(self, tmp_path, migration):
        # SVG is the one markup body that IS media: the document check
        # must not park it, and the bytes name its extension.
        write_corpus_item(tmp_path)
        file = write_media(tmp_path, SVG)
        path = write_ledger(tmp_path, page_entry(), media_entry())
        migration.apply(tmp_path)
        assert file.with_suffix(".svg").exists()
        assert ledger.load(path)[MEDIA_HASH].path == f"enrichment/{ITEM}/media-1.svg"

    @pytest.mark.parametrize(
        ("data", "name", "ext"),
        [
            (AVIF, "media-1.png", "avif"),  # a CDN's content-negotiated AVIF for a .png URL
            (PNG, "media-1.heif", "png"),  # a .heif URL that answered PNG
            (JPEG, "media-1.PNG", "jpg"),  # the suffix is read case-blind, then compared
        ],
    )
    def test_the_field_shapes_rename_to_what_their_bytes_are(
        self, tmp_path, migration, data, name, ext
    ):
        write_corpus_item(tmp_path)
        file = write_media(tmp_path, data, name=name)
        path = write_ledger(tmp_path, page_entry(), media_entry(path=f"enrichment/{ITEM}/{name}"))
        report = migration.apply(tmp_path)
        assert not file.exists()
        assert file.with_suffix(f".{ext}").read_bytes() == data
        assert ledger.load(path)[MEDIA_HASH].path == f"enrichment/{ITEM}/media-1.{ext}"
        assert "renamed 1 whose" in report.actions[0]

    def test_the_file_moves_before_the_line_is_written(self, tmp_path, migration, monkeypatch):
        # An apply killed between the two leaves a done line naming a file
        # that is gone — the health check's missing-output finding, healed
        # by `enrich mark` — and never a re-pointed line over a file still
        # under its old name. The digest moves last of all, so it is still
        # exactly as it was.
        write_corpus_item(tmp_path)
        file = write_media(tmp_path, AVIF)
        digest = write_digest(tmp_path, [f"enrichment/{ITEM}/media-1.png"])
        before = digest.read_text()
        path = write_ledger(tmp_path, page_entry(), media_entry())

        def killed(_path, _entry):
            raise KeyboardInterrupt

        monkeypatch.setattr(migration_13, "append", killed)
        with pytest.raises(KeyboardInterrupt):
            migration.apply(tmp_path)
        assert file.with_suffix(".avif").exists()
        assert not file.exists()
        assert ledger.load(path)[MEDIA_HASH].path == f"enrichment/{ITEM}/media-1.png"
        assert digest.read_text() == before

    def test_a_rename_owes_the_session_nothing_and_says_nothing(self, tmp_path, migration):
        # The ledger's via: migration-13 done line is the record; no per-unit
        # line asks anyone to look. A rename keeps the bytes, so a standing
        # description still covers them — only a requeue retires one.
        write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF)
        write_description(tmp_path)
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert not (tmp_path / "enrichment" / ITEM / "discarded-media-1.md").exists()
        assert len(report.actions) == 1
        assert "media-1" not in report.actions[0]

    def test_a_rename_moves_the_description_name_with_the_file(self, tmp_path, migration):
        # The name in the first line is the only tie between a description
        # and the file it covers. Moved the file and not the name, describe
        # stops finding it and a revised reading lands in a second slot
        # with the stale one still counted beside it.
        write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF)
        description = write_description(tmp_path)
        description.write_text(
            "Describes `media-1.png` — the diagram from `notes.md`.\n\nthe `media-1.png` shot\n",
            encoding="utf-8",
        )
        write_ledger(tmp_path, page_entry(), media_entry())
        migration.apply(tmp_path)
        # Only the name, and only on the first line: the rest is the
        # session's prose about the same bytes.
        assert description.read_text() == (
            "Describes `media-1.avif` — the diagram from `notes.md`.\n\nthe `media-1.png` shot\n"
        )

    def test_a_rename_leaves_a_description_naming_nothing_alone(self, tmp_path, migration):
        # No backticked name to move; rewriting the line would be a guess.
        write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF)
        description = write_description(tmp_path)
        before = description.read_text()
        write_ledger(tmp_path, page_entry(), media_entry())
        migration.apply(tmp_path)
        assert description.read_text() == before

    def test_a_renamed_line_keeps_what_the_live_line_carried(self, tmp_path, migration):
        # Identical but for path, item and via: a title, a rerun flag and
        # an http license all ride the re-pointed line unchanged.
        write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF)
        prior = LedgerEntry(
            hash=MEDIA_HASH,
            url=MEDIA_URL,
            item=ITEM,
            kind=Kind.INSTAGRAM,
            status=Status.DONE,
            http_shared=True,
            engine="0.1.4",
            date=FETCHED,
            at=NOW,
            job=Job.MEDIA,
            via="migration-9",
            parent=POST_HASH,
            depth=1,
            rerun=True,
            path=f"enrichment/{ITEM}/media-1.png",
            title="slide one",
        )
        path = write_ledger(tmp_path, page_entry(), prior)
        migration.apply(tmp_path)
        live = ledger.load(path)[MEDIA_HASH]
        assert live.http_shared is True
        assert live.rerun is True
        assert live.title == "slide one"
        assert live.via == "migration-13"
        assert live.path == f"enrichment/{ITEM}/media-1.avif"
        assert live.date == FETCHED
        assert live.at == NOW
        assert live.engine == ENGINE


class TestDigest:
    def test_a_block_form_listing_is_repointed_and_no_longer_drifts(self, tmp_path, migration):
        # The verb's own form. The path spelling is a derived fact, so the
        # rewrite is mechanical — and afterwards the digest states exactly
        # what the item carries, which is what the health check compares.
        corpus_path = write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF)
        write_media(tmp_path, JPEG, name="media-2.jpg")
        digest = write_digest(
            tmp_path, [f"enrichment/{ITEM}/media-1.png", f"enrichment/{ITEM}/media-2.jpg"]
        )
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert report.anomalies == []
        assert "re-pointed 1 digest(s)" in report.actions[0]
        assert stated_media(digest) == {
            f"enrichment/{ITEM}/media-1.avif",
            f"enrichment/{ITEM}/media-2.jpg",
        }
        carried = item_media(Instance(root=tmp_path), corpus.read_item(corpus_path))
        assert stated_media(digest) == set(carried)
        assert digest.read_text().endswith("---\n- a fact about the post\n")

    def test_a_flow_form_listing_is_repointed(self, tmp_path, migration):
        # Legacy digests wrote the flow form; the exact path is what moves.
        write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF)
        digest = write_digest(
            tmp_path,
            [f"enrichment/{ITEM}/media-1.png", f"enrichment/{ITEM}/media-2.jpg"],
            flow=True,
        )
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        text = digest.read_text()
        assert f"media: [enrichment/{ITEM}/media-1.avif, enrichment/{ITEM}/media-2.jpg]\n" in text
        assert "media-1.png" not in text
        assert "re-pointed 1 digest(s)" in report.actions[0]

    def test_the_body_is_never_touched(self, tmp_path, migration):
        # A fact naming the old path is prose the session wrote — and a
        # rule further down the body is not a second fence.
        write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF)
        body = f"- the still at enrichment/{ITEM}/media-1.png shows a receipt\n\n---\n\n- more\n"
        digest = write_digest(tmp_path, [f"enrichment/{ITEM}/media-1.png"], body=body)
        write_ledger(tmp_path, page_entry(), media_entry())
        migration.apply(tmp_path)
        assert digest.read_text().endswith(f"---\n{body}")
        assert stated_media(digest) == {f"enrichment/{ITEM}/media-1.avif"}

    def test_no_digest_is_nothing_to_do(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF)
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert "re-pointed 0 digest(s)" in report.actions[0]
        assert report.anomalies == []
        assert not (tmp_path / "state" / "digests").exists()

    def test_a_digest_naming_only_other_paths_is_untouched(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF)
        digest = write_digest(tmp_path, [f"enrichment/{ITEM}/media-2.jpg"])
        before = digest.read_text()
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert digest.read_text() == before
        assert "re-pointed 0 digest(s)" in report.actions[0]
        assert report.anomalies == []

    def test_a_digest_without_a_closing_fence_is_untouched(self, tmp_path, migration):
        # No block to rewrite inside: the file is left exactly as found —
        # the malformed digest is lint's finding, not this migration's.
        write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF)
        digest = write_digest(tmp_path, [f"enrichment/{ITEM}/media-1.png"])
        digest.write_text(f"---\nid: {ITEM}\nmedia:\n  - enrichment/{ITEM}/media-1.png\n")
        before = digest.read_text()
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert digest.read_text() == before
        assert report.anomalies == []
        assert "re-pointed 0 digest(s)" in report.actions[0]

    def test_a_repoint_that_does_not_read_back_is_an_anomaly(
        self, tmp_path, migration, monkeypatch
    ):
        # The write is verified, never assumed: a block still naming the
        # old path is the session's to re-emit, and it is told so — while
        # the file and the ledger line, already moved, stay moved.
        write_corpus_item(tmp_path)
        file = write_media(tmp_path, AVIF)
        digest = write_digest(tmp_path, [f"enrichment/{ITEM}/media-1.png"])
        path = write_ledger(tmp_path, page_entry(), media_entry())
        monkeypatch.setattr(migration_13.atomic, "write_text", lambda _path, _text: None)
        report = migration.apply(tmp_path)
        assert file.with_suffix(".avif").exists()
        assert ledger.load(path)[MEDIA_HASH].path == f"enrichment/{ITEM}/media-1.avif"
        assert stated_media(digest) == {f"enrichment/{ITEM}/media-1.png"}
        assert "re-pointed 0 digest(s)" in report.actions[0]
        assert len(report.anomalies) == 1
        assert report.anomalies[0].startswith(f"state/digests/{ITEM}.md: media: still names")
        assert f"enrichment/{ITEM}/media-1.png" in report.anomalies[0]
        assert "`enrich item digest`" in report.anomalies[0]

    @pytest.mark.parametrize(
        "corrupt",
        [
            # The listing lost outright: neither name survives the write.
            lambda text: text.replace(f"enrichment/{ITEM}/media-1.avif", "nothing"),
            # The old name still listed beside the new one.
            lambda text: text.replace("\n---\n", f"\n  - enrichment/{ITEM}/media-1.png\n---\n", 1),
        ],
    )
    def test_the_read_back_must_name_the_new_path_and_not_the_old(
        self, tmp_path, migration, monkeypatch, corrupt
    ):
        # Both halves of the verification stand on their own.
        write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF)
        write_digest(tmp_path, [f"enrichment/{ITEM}/media-1.png"])
        write_ledger(tmp_path, page_entry(), media_entry())
        monkeypatch.setattr(
            migration_13.atomic,
            "write_text",
            lambda path, text: Path.write_text(path, corrupt(text), encoding="utf-8"),
        )
        report = migration.apply(tmp_path)
        assert "re-pointed 0 digest(s)" in report.actions[0]
        assert len(report.anomalies) == 1

    def test_every_renamed_items_digest_is_repointed_and_counted(self, tmp_path, migration):
        other = "2026-08-30-second-def456"
        other_url = "https://www.instagram.com/p/DSecondOne/"
        other_media = "https://uuinstagram.com/images/DSecondOne/1"
        write_corpus_item(tmp_path)
        write_corpus_item(tmp_path, item_id=other, urls=(other_url,))
        write_media(tmp_path, AVIF)
        write_media(tmp_path, PNG, name="media-1.jpg", item=other)
        first = write_digest(tmp_path, [f"enrichment/{ITEM}/media-1.png"])
        second = write_digest(tmp_path, [f"enrichment/{other}/media-1.jpg"], item=other)
        write_ledger(
            tmp_path,
            page_entry(),
            media_entry(),
            page_entry(url=other_url, item=other),
            media_entry(
                url=other_media,
                parent=work_hash(other_url),
                item=other,
                path=f"enrichment/{other}/media-1.jpg",
            ),
        )
        report = migration.apply(tmp_path)
        assert (
            "renamed 2 whose bytes named another format and re-pointed 2 digest(s)"
            in (report.actions[0])
        )
        assert stated_media(first) == {f"enrichment/{ITEM}/media-1.avif"}
        assert stated_media(second) == {f"enrichment/{other}/media-1.png"}

    def test_a_renamed_items_digest_is_found_under_the_live_id(self, tmp_path, migration):
        renamed = "2026-08-31-carousel-renamed-abc123"
        write_corpus_item(tmp_path, item_id=renamed)
        write_media(tmp_path, AVIF)
        digest = write_digest(tmp_path, [f"enrichment/{ITEM}/media-1.png"], item=renamed)
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert stated_media(digest) == {f"enrichment/{ITEM}/media-1.avif"}
        assert "re-pointed 1 digest(s)" in report.actions[0]


class TestUntouched:
    @pytest.mark.parametrize(
        ("data", "name"),
        [
            (PNG, "media-1.png"),  # the suffix already matches the bytes
            (PNG, "media-1.PNG"),  # a spelling, not a wrong format
            (JPEG, "media-1.jpeg"),  # the field's one alias of the sniffer's `jpg`
            (JPEG, "media-1.jpg"),
            (UNSIGNED, "media-1.png"),  # no signature this engine knows
            (BOM16, "media-1.png"),  # the sync-frame guess is not a signature
            (LFS_POINTER, "media-1.png"),  # an unsmudged pointer: text, no signature
            (AVIF, "media-1.avif"),  # the fixed path's own naming
        ],
    )
    def test_a_file_needing_no_repair_is_left_alone(self, tmp_path, migration, data, name):
        write_corpus_item(tmp_path)
        file = write_media(tmp_path, data, name=name)
        path = write_ledger(tmp_path, page_entry(), media_entry(path=f"enrichment/{ITEM}/{name}"))
        lines_before = path.read_text()
        report = migration.apply(tmp_path)
        assert file.read_bytes() == data
        assert path.read_text() == lines_before
        assert report.actions == []
        assert report.skipped == []

    def test_a_done_line_over_a_missing_file_is_left_alone(self, tmp_path, migration):
        # Nothing to read, nothing to decide: that pointer is the health
        # check's finding, not this migration's.
        write_corpus_item(tmp_path)
        path = write_ledger(tmp_path, page_entry(), media_entry())
        lines_before = path.read_text()
        report = migration.apply(tmp_path)
        assert path.read_text() == lines_before
        assert report.actions == []
        assert report.skipped == []

    def test_a_path_escaping_the_root_is_never_a_member(self, tmp_path, migration):
        outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
        outside.write_bytes(b"")
        root = tmp_path / "instance"
        write_corpus_item(root)
        path = write_ledger(root, page_entry(), media_entry(path=f"../{outside.name}"))
        lines_before = path.read_text()
        report = migration.apply(root)
        assert outside.exists()
        assert path.read_text() == lines_before
        assert report.actions == []

    def test_other_lines_are_never_members(self, tmp_path, migration):
        # A page's done line (job unset) over a markdown file that opens
        # like HTML, a media line parked blocked, a media done line that
        # recorded no path, and an asset line: none is a done media file.
        write_corpus_item(tmp_path)
        enrichment = tmp_path / "enrichment" / ITEM / f"instagram-{POST_HASH[:6]}.md"
        enrichment.parent.mkdir(parents=True)
        enrichment.write_text("<!doctype html>\n", encoding="utf-8")
        write_media(tmp_path, b"", name="media-2.png")
        write_media(tmp_path, b"", name="feedfa-asset-1.png")
        blocked_url = "https://uuinstagram.com/images/DTestCode1/2"
        pathless_url = "https://uuinstagram.com/images/DTestCode1/3"
        asset_url = "file:enrichment/x/feedfa-asset-1.png"
        path = write_ledger(
            tmp_path,
            page_entry(path=str(enrichment.relative_to(tmp_path))),
            media_entry(url=blocked_url, status=Status.BLOCKED),
            media_entry(url=pathless_url, path=None),
            media_entry(
                url=asset_url,
                job=Job.ASSET,
                path=f"enrichment/{ITEM}/feedfa-asset-1.png",
            ),
        )
        lines_before = path.read_text()
        report = migration.apply(tmp_path)
        assert path.read_text() == lines_before
        assert report.actions == []
        assert report.skipped == []
        assert enrichment.exists()

    def test_a_superseded_done_line_is_not_the_live_one(self, tmp_path, migration):
        # The unit was re-queued by hand AFTER the done line — the newest
        # `at` wins whatever the file order, so the done line is history
        # and its file is not this migration's to touch.
        write_corpus_item(tmp_path)
        file = write_media(tmp_path, b"")
        later = NOW + datetime.timedelta(seconds=1)
        path = write_ledger(
            tmp_path,
            page_entry(),
            media_entry(status=Status.QUEUED, at=later),
            media_entry(),
        )
        lines_before = path.read_text()
        report = migration.apply(tmp_path)
        assert file.exists()
        assert path.read_text() == lines_before
        assert report.actions == []

    def test_a_healthy_file_of_a_purged_item_makes_no_skip(self, tmp_path, migration):
        # The bytes are read before the item is asked: a file needing no
        # repair is nobody's finding, whoever owns it.
        write_media(tmp_path, PNG)
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []


class TestOwnership:
    def test_a_renamed_item_resolves_through_the_parent_url(self, tmp_path, migration):
        # The stored item is gone but a live item lists the post: that is a
        # rename, and the seed writes under the live id. The file still
        # stands under the old directory, so it is still a member.
        renamed = "2026-08-31-carousel-renamed-abc123"
        write_corpus_item(tmp_path, item_id=renamed)
        file = write_media(tmp_path, b"")
        path = write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert not file.exists()
        live = ledger.load(path)[MEDIA_HASH]
        assert live.status is Status.QUEUED
        assert live.item == renamed
        assert report.skipped == []
        assert report.actions[1].startswith(f"{renamed}: enrichment/{ITEM}/media-1.png")

    def test_a_renamed_items_repointed_line_names_the_live_item(self, tmp_path, migration):
        renamed = "2026-08-31-carousel-renamed-abc123"
        write_corpus_item(tmp_path, item_id=renamed)
        write_media(tmp_path, AVIF)
        path = write_ledger(tmp_path, page_entry(), media_entry())
        migration.apply(tmp_path)
        live = ledger.load(path)[MEDIA_HASH]
        assert live.status is Status.DONE
        assert live.item == renamed
        assert live.path == f"enrichment/{ITEM}/media-1.avif"

    def test_an_unclaimed_member_is_skipped_with_why(self, tmp_path, migration):
        # No corpus file, no exclusion, nothing lists the post: nothing
        # claims the work, so the repair has no item to write under — and
        # the file stays exactly as it is.
        file = write_media(tmp_path, b"")
        path = write_ledger(tmp_path, page_entry(), media_entry())
        lines_before = path.read_text()
        report = migration.apply(tmp_path)
        assert file.exists()
        assert path.read_text() == lines_before
        assert report.actions == []
        assert len(report.skipped) == 1
        assert report.skipped[0].what == f"media repair for enrichment/{ITEM}/media-1.png"
        assert "nothing claims this work" in report.skipped[0].why
        assert f"corpus/2026/{ITEM}.md does not exist" in report.skipped[0].why
        assert MEDIA_URL in report.skipped[0].why
        assert "`enrich mark`" in report.skipped[0].why

    def test_a_purged_item_is_never_repaired(self, tmp_path, migration):
        # No corpus file and an exclusion on the record: the owner ruled
        # this content out, and a requeue would put the ruling back in the
        # queue. Blank line, an unrelated row, and a tab inside the reason:
        # the tolerant TSV read takes a row per line and splits on the
        # FIRST tab, so the reason keeps its own.
        exclusions = tmp_path / "state" / "exclusions.tsv"
        exclusions.parent.mkdir(parents=True, exist_ok=True)
        exclusions.write_text(
            f"\n2026-01-01-unrelated-000000\toff topic\n{ITEM}\towner purged it\tfor good\n",
            encoding="utf-8",
        )
        file = write_media(tmp_path, AVIF)
        path = write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert file.exists()
        assert ledger.load(path)[MEDIA_HASH].path == f"enrichment/{ITEM}/media-1.png"
        assert report.actions == []
        assert len(report.skipped) == 1
        assert "excluded on the record" in report.skipped[0].why
        assert "owner purged it\tfor good" in report.skipped[0].why

    def test_an_excluded_item_with_no_reason_recorded(self, tmp_path, migration):
        exclusions = tmp_path / "state" / "exclusions.tsv"
        exclusions.parent.mkdir(parents=True, exist_ok=True)
        exclusions.write_text(f"{ITEM}\t\n", encoding="utf-8")
        write_media(tmp_path, b"")
        write_ledger(tmp_path, page_entry(), media_entry())
        report = migration.apply(tmp_path)
        assert "state/exclusions.tsv: no reason recorded" in report.skipped[0].why

    def test_a_parentless_unit_of_a_gone_item_is_unclaimed(self, tmp_path, migration):
        # No stored item file and no parent to ask the corpus about: the
        # rule has nowhere left to look.
        write_corpus_item(tmp_path)
        orphan = LedgerEntry(
            hash=MEDIA_HASH,
            url=MEDIA_URL,
            item="2026-08-30-gone-def456",
            kind=Kind.INSTAGRAM,
            status=Status.DONE,
            engine="0.1.4",
            date=FETCHED,
            at=NOW,
            job=Job.MEDIA,
            path="enrichment/2026-08-30-gone-def456/media-1.png",
        )
        file = write_media(tmp_path, b"", item="2026-08-30-gone-def456")
        write_ledger(tmp_path, page_entry(), orphan)
        report = migration.apply(tmp_path)
        assert file.exists()
        assert len(report.skipped) == 1
        assert "nothing claims this work" in report.skipped[0].why

    def test_an_unclaimed_member_never_stops_the_rest(self, tmp_path, migration):
        # An unclaimed member sits BEFORE a healthy one: reported and
        # stepped over, never a reason to abandon the walk.
        orphan_item = "2026-08-30-orphaned-def456"
        orphan_post = "https://www.instagram.com/p/DOrphaned1/"
        orphan_media = "https://uuinstagram.com/images/DOrphaned1/1"
        write_corpus_item(tmp_path)
        write_media(tmp_path, b"", item=orphan_item)
        write_media(tmp_path, b"")
        path = write_ledger(
            tmp_path,
            page_entry(url=orphan_post, item=orphan_item),
            media_entry(url=orphan_media, parent=work_hash(orphan_post), item=orphan_item),
            page_entry(),
            media_entry(),
        )
        report = migration.apply(tmp_path)
        live = ledger.load(path)
        assert live[MEDIA_HASH].status is Status.QUEUED
        assert live[work_hash(orphan_media)].status is Status.DONE
        assert len(report.skipped) == 1
        assert len(report.actions) == 2


class TestIdempotency:
    def test_a_second_apply_is_a_noop(self, tmp_path, migration):
        # One requeued, one renamed: after the first apply neither is a
        # member — the one is no longer done, the other's suffix matches.
        write_corpus_item(tmp_path)
        write_media(tmp_path, b"", name="media-1.png")
        write_media(tmp_path, AVIF, name="media-2.png")
        second = "https://uuinstagram.com/images/DTestCode1/2"
        path = write_ledger(
            tmp_path,
            page_entry(),
            media_entry(),
            media_entry(url=second, path=f"enrichment/{ITEM}/media-2.png"),
        )
        first = migration.apply(tmp_path)
        assert first.actions[0].startswith("requeued 1 media unit(s)")
        assert "renamed 1 whose" in first.actions[0]
        lines_after_first = path.read_text()
        report = migration.apply(tmp_path)
        assert path.read_text() == lines_after_first
        assert report.actions == []
        assert report.skipped == []
        assert sorted(p.name for p in (tmp_path / "enrichment" / ITEM).iterdir()) == [
            "media-2.avif"
        ]

    def test_a_missing_ledger_is_a_noop(self, tmp_path, migration):
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []
        assert report.anomalies == []

    def test_the_summary_leads_and_only_requeues_follow(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        write_media(tmp_path, AVIF, name="media-1.png")
        write_media(tmp_path, HTML, name="media-2.png")
        second = "https://uuinstagram.com/images/DTestCode1/2"
        write_ledger(
            tmp_path,
            page_entry(),
            media_entry(),
            media_entry(url=second, path=f"enrichment/{ITEM}/media-2.png"),
        )
        report = migration.apply(tmp_path)
        assert len(report.actions) == 2
        assert report.actions[0].startswith("requeued 1 media unit(s)")
        assert "renamed 1 whose" in report.actions[0]
        assert "media-2.png held HTML" in report.actions[1]

    def test_the_summary_counts_every_unit_of_each_kind(self, tmp_path, migration):
        write_corpus_item(tmp_path)
        entries = [page_entry()]
        for slot, data in enumerate((b"", HTML, AVIF, AVIF), start=1):
            write_media(tmp_path, data, name=f"media-{slot}.png")
            entries.append(
                media_entry(
                    url=f"https://uuinstagram.com/images/DTestCode1/{slot}",
                    path=f"enrichment/{ITEM}/media-{slot}.png",
                )
            )
        write_ledger(tmp_path, *entries)
        report = migration.apply(tmp_path)
        assert report.actions[0].startswith(
            "requeued 2 media unit(s) whose file held no media, renamed 2 whose bytes named "
            "another format and re-pointed 0 digest(s)"
        )
        assert len(report.actions) == 3


class TestTolerance:
    def test_an_unparseable_ledger_line_never_stops_the_repair(self, tmp_path, migration):
        # The torn line sits AHEAD of the unit's: a union merge relocates
        # a half-written line anywhere, and the lines behind it are still
        # this migration's to heal.
        write_corpus_item(tmp_path)
        write_media(tmp_path, b"")
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"not json at all\n{ledger.to_line(page_entry())}\n{ledger.to_line(media_entry())}\n",
            encoding="utf-8",
        )
        report = migration.apply(tmp_path)
        assert len(seed_lines(path)) == 1
        assert '"via": "migration-13"' in seed_lines(path)[0]
        assert any("does not parse" in s.why for s in report.skipped)
        assert any(s.what == "ledger line 1" for s in report.skipped)

    def test_a_parse_skip_survives_a_memberless_ledger(self, tmp_path, migration):
        # No members at all, but a torn line: the early return still
        # carries the skip, or the session never hears the file needs
        # repair.
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all\n", encoding="utf-8")
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert len(report.skipped) == 1
        assert report.skipped[0].what == "ledger line 1"

    def test_the_live_line_wins_whatever_the_file_order(self, tmp_path, migration):
        # A union merge can leave a hash's newest line mid-file, ties are
        # broken by position, and blank or garbled lines sit anywhere — the
        # tolerant read walks the WHOLE file and resolves as `load` does.
        write_corpus_item(tmp_path)
        write_media(tmp_path, b"")
        earlier = NOW - datetime.timedelta(seconds=1)
        lines = [
            ledger.to_line(page_entry()),
            ledger.to_line(media_entry()),
            ledger.to_line(media_entry()),  # verbatim duplicate: same at, tie on position
            ledger.to_line(media_entry(status=Status.QUEUED, at=earlier)),  # superseded
            "",
            "not json at all",
        ]
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        report = migration.apply(tmp_path)
        assert len(lines_for(path)) == 4  # three prior lines and exactly one seed
        assert len(seed_lines(path)) == 2  # the superseded queued line, and the seed
        assert '"via": "migration-13"' in seed_lines(path)[-1]
        assert any(s.what == "ledger line 6" for s in report.skipped)

    def test_a_same_instant_tie_goes_to_the_later_line_as_load_resolves_it(
        self, tmp_path, migration
    ):
        # Two lines of one hash stamped at the same instant: `load` hands
        # the tie to file position, so the queued line that FOLLOWS the
        # done one is live, and the done line's file is not this
        # migration's to touch.
        write_corpus_item(tmp_path)
        file = write_media(tmp_path, b"")
        path = write_ledger(
            tmp_path, page_entry(), media_entry(), media_entry(status=Status.QUEUED)
        )
        lines_before = path.read_text()
        report = migration.apply(tmp_path)
        assert file.exists()
        assert path.read_text() == lines_before
        assert report.actions == []
