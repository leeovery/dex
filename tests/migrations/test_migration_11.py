"""Tests for migration 11: repair gspace attachments that collide on disk."""

import datetime
import hashlib
import json

import pytest

from dex_engine import corpus
from dex_engine.migrations.migration_11 import _WHY, _re_read, build

TODAY = datetime.date(2026, 9, 2)
NOW = datetime.datetime(2026, 9, 2, 9, 0, 0, 500000, tzinfo=datetime.UTC)

CHANNEL = "room"
IMAGE = "File-image.png"
BODY = "\n**Alex**: the original message text, verbatim\n"
# The bytes migration 8 copied everywhere: the first collision's file.
ZERO = b"zero-bytes"
ROOM_REWRITTEN = (
    "raw/gspace/room: 2 item(s) rewritten, 3 file(s) copied out of raw/, "
    "3 stale media file(s) deleted"
)


@pytest.fixture
def migration():
    return build(today=lambda: TODAY, now=lambda: NOW, engine_version="0.2.0")


def shortid(topic_id, channel=CHANNEL):
    return hashlib.sha1(f"{channel}/{topic_id}".encode()).hexdigest()[:6]  # noqa: S324 — mirrors the hand normalizer


def item_id(topic_id, channel=CHANNEL):
    return f"2026-05-01-{topic_id}-{shortid(topic_id, channel)}"


def raw(name, channel=CHANNEL):
    return f"raw/gspace/{channel}/{name}"


def message(topic_id, *export_names, text="a message"):
    record = {
        "created_date": "Friday, May 1, 2026 at 9:00:00 AM UTC",
        "creator": {"name": "Alex", "email": "alex@example.test"},
        "message_id": f"m-{topic_id}-{len(export_names)}",
        "text": text,
        "topic_id": topic_id,
    }
    if export_names:
        record["attached_files"] = [
            {"original_name": name.removeprefix("File-"), "export_name": name}
            for name in export_names
        ]
    return record


def write_manifest(root, messages, channel=CHANNEL):
    path = root / "raw" / "gspace" / channel / "messages.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = messages if isinstance(messages, str) else json.dumps({"messages": messages})
    path.write_text(payload, encoding="utf-8")
    return path


def write_file(root, repo_path, data):
    path = root / repo_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_item(  # noqa: PLR0913 — a fixture builder mirrors the item's fields
    root,
    topic_id,
    *,
    attachments=(),
    media=(),
    status="raw",
    enrichment=(),
    body=BODY,
):
    identifier = item_id(topic_id)
    path = root / "corpus" / identifier[:4] / f"{identifier}.md"
    corpus.write_item(
        path,
        corpus.CorpusItem(
            id=identifier,
            source="gspace",
            channel=CHANNEL,
            shared_by="Alex",
            date=datetime.date(2026, 5, 1),
            kinds=["text"],
            attachments=list(attachments),
            status=status,
            enrichment=list(enrichment),
            media=list(media),
            body=body,
        ),
    )
    return path


def fingerprint(path):
    stat = path.stat()
    return (path.read_bytes(), stat.st_ino, stat.st_mtime_ns)


def snapshot(root):
    return {
        path.relative_to(root).as_posix(): fingerprint(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def seed_export(root):
    """The real shape: one export_name attached four times across three topics.

    ``alpha`` takes the first occurrence (the bare name on disk), ``bravo``
    the second, and ``charlie`` the third and fourth in a single message.
    """
    write_manifest(
        root,
        [
            message("alpha", IMAGE),
            message("bravo", IMAGE, "notes.pdf"),
            message("chatter"),
            message("charlie", IMAGE, IMAGE),
        ],
    )
    for name, data in (
        (IMAGE, ZERO),
        ("File-image(1).png", b"one-bytes"),
        ("File-image(2).png", b"two-bytes"),
        ("File-image(3).png", b"three-bytes"),
        ("notes.pdf", b"%PDF-"),
    ):
        write_file(root, raw(name), data)


def seed_items(root):
    """The corpus as the hand normalizer and migration 8 left it: all wrong."""
    for topic_id, attachments in (
        ("alpha", [raw(IMAGE)]),
        ("bravo", [raw(IMAGE), raw("notes.pdf")]),
        ("charlie", [raw(IMAGE), raw(IMAGE)]),
        ("chatter", []),
    ):
        media = []
        taken = set()
        for attachment in attachments:
            name = attachment.rsplit("/", 1)[-1]
            if name in taken:
                name = f"{hashlib.sha1(attachment.encode()).hexdigest()[:6]}-{name}"  # noqa: S324 — mirrors normalize
            taken.add(name)
            media.append(f"media/{shortid(topic_id)}/{name}")
        for entry in media:
            write_file(root, entry, b"%PDF-" if entry.endswith(".pdf") else ZERO)
        write_item(root, topic_id, attachments=attachments, media=media)


class TestRewrite:
    def test_a_colliding_item_attaches_and_carries_its_own_file(self, tmp_path, migration):
        # The field defect: 21 of 24 items carried a copy of the first
        # collision's bytes, because the manifest named it for all of them.
        seed_export(tmp_path)
        seed_items(tmp_path)
        path = tmp_path / "corpus" / "2026" / f"{item_id('bravo')}.md"
        migration.apply(tmp_path)
        item = corpus.read_item(path)
        media = f"media/{shortid('bravo')}"
        assert item.attachments == [raw("File-image(1).png"), raw("notes.pdf")]
        assert item.media == [f"{media}/File-image(1).png", f"{media}/notes.pdf"]
        assert (tmp_path / media / "File-image(1).png").read_bytes() == b"one-bytes"
        assert (tmp_path / media / "notes.pdf").read_bytes() == b"%PDF-"
        assert not (tmp_path / media / IMAGE).exists()  # the wrong copy is gone

    def test_the_first_occurrence_is_left_exactly_as_it_is(self, tmp_path, migration):
        # Its bare name IS the name on disk, so its copy was never wrong.
        seed_export(tmp_path)
        seed_items(tmp_path)
        path = tmp_path / "corpus" / "2026" / f"{item_id('alpha')}.md"
        copy = tmp_path / "media" / shortid("alpha") / IMAGE
        before = (fingerprint(path), fingerprint(copy))
        migration.apply(tmp_path)
        assert (fingerprint(path), fingerprint(copy)) == before

    def test_two_same_named_attachments_in_one_message_land_as_two_files(self, tmp_path, migration):
        # Both took the same manifest name, so migration 8 gave the second a
        # hashed prefix over a second copy of the same wrong bytes.
        seed_export(tmp_path)
        seed_items(tmp_path)
        path = tmp_path / "corpus" / "2026" / f"{item_id('charlie')}.md"
        media = tmp_path / "media" / shortid("charlie")
        hashed = f"{hashlib.sha1(raw(IMAGE).encode()).hexdigest()[:6]}-{IMAGE}"  # noqa: S324 — mirrors normalize
        migration.apply(tmp_path)
        item = corpus.read_item(path)
        assert item.attachments == [raw("File-image(2).png"), raw("File-image(3).png")]
        assert item.media == [
            f"media/{shortid('charlie')}/File-image(2).png",
            f"media/{shortid('charlie')}/File-image(3).png",
        ]
        assert (media / "File-image(2).png").read_bytes() == b"two-bytes"
        assert (media / "File-image(3).png").read_bytes() == b"three-bytes"
        assert sorted(entry.name for entry in media.iterdir()) == [
            "File-image(2).png",
            "File-image(3).png",
        ]
        assert not (media / hashed).exists()

    def test_the_rest_of_the_item_survives_byte_for_byte(self, tmp_path, migration):
        seed_export(tmp_path)
        write_item(
            tmp_path,
            "bravo",
            attachments=[raw(IMAGE), raw("notes.pdf")],
            status="enriched",
            enrichment=["web-abc123.md"],
        )
        path = tmp_path / "corpus" / "2026" / f"{item_id('bravo')}.md"
        migration.apply(tmp_path)
        item = corpus.read_item(path)
        assert item.status == "enriched"
        assert item.enrichment == ["web-abc123.md"]
        assert item.body == BODY
        assert item.shared_by == "Alex"
        assert item.date == datetime.date(2026, 5, 1)

    def test_the_report_counts_the_channel_and_says_what_the_export_did(self, tmp_path, migration):
        seed_export(tmp_path)
        seed_items(tmp_path)
        report = migration.apply(tmp_path)
        assert report.actions == [
            ROOM_REWRITTEN,
            _re_read(item_id("bravo")),
            _re_read(item_id("charlie")),
            _WHY,
        ]
        assert report.skipped == []
        assert report.anomalies == []

    def test_each_rewritten_item_is_told_to_re_digest_from_a_fresh_reading(
        self, tmp_path, migration
    ):
        # The health check will keep naming these as media drift, but the
        # generic drift repair carries the stated facts forward — and these
        # were read from the wrong file. Only the rewritten items are named:
        # the first occurrence never carried the wrong bytes.
        seed_export(tmp_path)
        seed_items(tmp_path)
        report = migration.apply(tmp_path)
        named = [line for line in report.actions if line.startswith("re-digest ")]
        assert named == [_re_read(item_id("bravo")), _re_read(item_id("charlie"))]
        assert item_id("alpha") not in " ".join(report.actions)
        assert named[0] == (
            f"re-digest {item_id('bravo')} from a fresh reading — its standing digest was "
            "written against the wrong file, so carry none of its facts forward"
        )

    def test_the_closing_line_names_the_digest_follow_up(self, tmp_path, migration):
        # The rewrite lands under standing digests: they still list the old
        # media path, and what was described was described from the wrong file.
        seed_export(tmp_path)
        seed_items(tmp_path)
        report = migration.apply(tmp_path)
        assert report.actions[-1].endswith(
            "A standing digest still lists the old media path, so every rewritten item "
            "reads as media drift until `enrich item digest` re-emits it, and any media "
            "description written against the old copy describes the wrong file — re-read those"
        )

    def test_each_channel_counts_on_its_own(self, tmp_path, migration):
        seed_export(tmp_path)
        seed_items(tmp_path)
        other = "lounge"
        write_manifest(tmp_path, [message("delta", IMAGE), message("echo", IMAGE)], channel=other)
        write_file(tmp_path, raw(IMAGE, other), ZERO)
        write_file(tmp_path, raw("File-image(1).png", other), b"one-bytes")
        write_file(tmp_path, f"media/{shortid('echo', other)}/{IMAGE}", ZERO)
        identifier = f"2026-05-01-echo-{shortid('echo', other)}"
        corpus.write_item(
            tmp_path / "corpus" / "2026" / f"{identifier}.md",
            corpus.CorpusItem(
                id=identifier,
                source="gspace",
                channel=other,
                shared_by="Alex",
                date=datetime.date(2026, 5, 1),
                kinds=["text"],
                attachments=[raw(IMAGE, other)],
                status="raw",
                enrichment=[],
                media=[f"media/{shortid('echo', other)}/{IMAGE}"],
                body=BODY,
            ),
        )
        report = migration.apply(tmp_path)
        assert [line for line in report.actions if "item(s) rewritten" in line] == [
            (
                "raw/gspace/lounge: 1 item(s) rewritten, 1 file(s) copied out of raw/, "
                "1 stale media file(s) deleted"
            ),
            ROOM_REWRITTEN,
        ]
        assert report.actions[1] == _re_read(identifier)


class TestDiskNames:
    @pytest.mark.parametrize(
        ("export_name", "second"),
        [
            ("File-image.png", "File-image(1).png"),
            ("File-a.b.png", "File-a.b(1).png"),  # the LAST dot, not the first
            ("Screenshot", "Screenshot(1)"),  # no dot: the suffix appends
        ],
    )
    def test_the_second_occurrence_takes_the_name_the_export_wrote(
        self, tmp_path, migration, export_name, second
    ):
        write_manifest(tmp_path, [message("alpha", export_name), message("bravo", export_name)])
        write_file(tmp_path, raw(export_name), ZERO)
        write_file(tmp_path, raw(second), b"one-bytes")
        path = write_item(tmp_path, "bravo", attachments=[raw(export_name)])
        migration.apply(tmp_path)
        assert corpus.read_item(path).attachments == [raw(second)]
        assert (tmp_path / f"media/{shortid('bravo')}/{second}").read_bytes() == b"one-bytes"


class TestNothingToDo:
    def test_a_channel_without_collisions_writes_nothing(self, tmp_path, migration):
        write_manifest(tmp_path, [message("alpha", IMAGE), message("bravo", "notes.pdf")])
        write_file(tmp_path, raw(IMAGE), ZERO)
        write_file(tmp_path, raw("notes.pdf"), b"%PDF-")
        first = write_item(tmp_path, "alpha", attachments=[raw(IMAGE)])
        second = write_item(tmp_path, "bravo", attachments=[raw("notes.pdf")])
        before = (fingerprint(first), fingerprint(second))
        report = migration.apply(tmp_path)
        assert (fingerprint(first), fingerprint(second)) == before
        assert not (tmp_path / "media").exists()
        assert report.actions == []
        assert report.skipped == []
        assert report.anomalies == []

    def test_a_second_apply_writes_nothing(self, tmp_path, migration):
        seed_export(tmp_path)
        seed_items(tmp_path)
        migration.apply(tmp_path)
        before = snapshot(tmp_path)
        report = migration.apply(tmp_path)
        assert snapshot(tmp_path) == before
        assert report.actions == []
        assert report.skipped == []
        assert report.anomalies == []

    def test_no_gspace_export_is_an_empty_report(self, tmp_path, migration):
        write_item(tmp_path, "alpha", attachments=[raw(IMAGE)])
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.skipped == []
        assert report.anomalies == []

    def test_a_channel_directory_without_a_manifest_is_passed_over(self, tmp_path, migration):
        (tmp_path / "raw" / "gspace" / "empty").mkdir(parents=True)
        (tmp_path / "raw" / "gspace" / "loose.txt").write_text("not a channel")
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert report.anomalies == []


class TestRefusals:
    def test_a_missing_source_leaves_the_item_alone(self, tmp_path, migration):
        # A repair that cannot see the bytes it would copy never guesses.
        seed_export(tmp_path)
        seed_items(tmp_path)
        (tmp_path / raw("File-image(1).png")).unlink()
        path = tmp_path / "corpus" / "2026" / f"{item_id('bravo')}.md"
        before = fingerprint(path)
        report = migration.apply(tmp_path)
        assert fingerprint(path) == before
        assert (tmp_path / f"media/{shortid('bravo')}/{IMAGE}").read_bytes() == ZERO
        assert len(report.anomalies) == 1
        assert f"corpus/2026/{item_id('bravo')}.md" in report.anomalies[0]
        assert "raw/gspace/room/File-image(1).png" in report.anomalies[0]
        # The channel's other member is still repaired.
        assert report.actions[0].startswith("raw/gspace/room: 1 item(s) rewritten")

    def test_a_hand_edited_attachment_list_is_skipped_untouched(self, tmp_path, migration):
        seed_export(tmp_path)
        path = write_item(tmp_path, "bravo", attachments=[raw("notes.pdf"), raw(IMAGE)])
        before = fingerprint(path)
        report = migration.apply(tmp_path)
        assert fingerprint(path) == before
        assert report.actions == []
        assert report.skipped[0].what == f"corpus/2026/{item_id('bravo')}.md"
        assert "neither the export's spelling nor the corrected one" in report.skipped[0].why
        assert "File-image(1).png" in report.skipped[0].why  # what it should have been

    def test_a_topic_with_no_corpus_item_is_skipped(self, tmp_path, migration):
        seed_export(tmp_path)
        write_item(tmp_path, "alpha", attachments=[raw(IMAGE)])
        report = migration.apply(tmp_path)
        assert report.actions == []
        assert sorted(entry.what for entry in report.skipped) == [
            "raw/gspace/room topic bravo",
            "raw/gspace/room topic charlie",
        ]
        assert f"-{shortid('bravo')}" in report.skipped[0].why

    @pytest.mark.parametrize("content", [b"\xff\xfe not decodable", b"nothing like frontmatter"])
    def test_an_unreadable_item_is_named_not_fatal(self, tmp_path, migration, content):
        seed_export(tmp_path)
        broken = tmp_path / "corpus" / "2026" / f"{item_id('bravo')}.md"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_bytes(content)
        healthy = write_item(tmp_path, "charlie", attachments=[raw(IMAGE), raw(IMAGE)])
        report = migration.apply(tmp_path)
        assert corpus.read_item(healthy).attachments == [
            raw("File-image(2).png"),
            raw("File-image(3).png"),
        ]
        assert [entry.what for entry in report.skipped] == [f"corpus/2026/{item_id('bravo')}.md"]
        assert "could not be read" in report.skipped[0].why

    def test_only_the_item_s_own_superseded_copies_are_deleted(self, tmp_path, migration):
        # media: is data too — an entry pointing at an engine file or into
        # another item's directory must not turn the repair into a delete of
        # it, and neither refusal may end the sweep early.
        seed_export(tmp_path)
        write_file(tmp_path, "state/exclusions.tsv", b"an-item\tnot ours\n")
        media = f"media/{shortid('bravo')}"
        write_item(
            tmp_path,
            "bravo",
            attachments=[raw(IMAGE), raw("notes.pdf")],
            media=[
                f"{media}/notes.pdf",  # kept: the corrected list names it too
                "state/exclusions.tsv",
                f"{media}/../{shortid('alpha')}/{IMAGE}",
                f"{media}/{IMAGE}",  # the wrong copy, and the only deletion
            ],
        )
        write_file(tmp_path, f"{media}/notes.pdf", b"%PDF-")
        write_file(tmp_path, f"{media}/{IMAGE}", ZERO)
        write_file(tmp_path, f"media/{shortid('alpha')}/{IMAGE}", ZERO)
        report = migration.apply(tmp_path)
        assert (tmp_path / "state" / "exclusions.tsv").exists()
        assert (tmp_path / "media" / shortid("alpha") / IMAGE).exists()
        assert (tmp_path / media / "notes.pdf").exists()
        assert not (tmp_path / media / IMAGE).exists()
        assert report.actions[0].endswith("1 stale media file(s) deleted")

    def test_a_source_that_resolves_outside_raw_is_dropped_and_named(self, tmp_path, migration):
        # A channel file can be a symlink out of the repo; copying what it
        # points at would make an outside file this item's pipeline input.
        root = tmp_path / "instance"
        outside = tmp_path / "outside" / "File-image(1).png"
        outside.parent.mkdir(parents=True)
        outside.write_bytes(b"one-bytes")
        seed_export(root)
        path = write_item(root, "bravo", attachments=[raw(IMAGE), raw("notes.pdf")])
        link = root / raw("File-image(1).png")
        link.unlink()
        link.symlink_to(outside)
        report = migration.apply(root)
        item = corpus.read_item(path)
        assert item.attachments == [raw("File-image(1).png"), raw("notes.pdf")]
        assert item.media == [f"media/{shortid('bravo')}/notes.pdf"]  # the rest still lands
        assert report.skipped[0].what == f"corpus/2026/{item_id('bravo')}.md"
        assert report.skipped[0].why == (
            "attachment 'raw/gspace/room/File-image(1).png' resolves outside raw/ — no media entry"
        )


class TestManifest:
    @pytest.mark.parametrize(
        ("payload", "reason"),
        [
            ("{not json", "does not parse"),
            ('["a message"]', 'is not {"messages": [...]}'),
            ('{"messages": {}}', 'is not {"messages": [...]}'),
            ('{"messages": ["a message"]}', "is not an object"),
            ('{"messages": [{"topic_id": "a", "attached_files": {"n": 1}}]}', "is not a list"),
            ('{"messages": [{"attached_files": [{"export_name": "a.png"}]}]}', "no topic_id"),
            (
                '{"messages": [{"topic_id": "", "attached_files": [{"export_name": "a.png"}]}]}',
                "no topic_id",
            ),
            ('{"messages": [{"topic_id": "a", "attached_files": [{}]}]}', "plain file name"),
            (
                '{"messages": [{"topic_id": "a", "attached_files": [{"export_name": "../x"}]}]}',
                "plain file name",
            ),
            (
                '{"messages": [{"topic_id": "a", "attached_files": [{"export_name": ""}]}]}',
                "plain file name",
            ),
            (
                '{"messages": [{"topic_id": "a", "attached_files": [{"export_name": "."}]}]}',
                "plain file name",
            ),
            (
                '{"messages": [{"topic_id": "a", "attached_files": [{"export_name": ".."}]}]}',
                "plain file name",
            ),
        ],
    )
    def test_a_manifest_that_is_not_the_expected_shape_is_an_anomaly(
        self, tmp_path, migration, payload, reason
    ):
        # Ordinals are positions in one stream: a manifest this code cannot
        # read end to end would mis-count every file after the bad record.
        seed_export(tmp_path)
        seed_items(tmp_path)
        write_manifest(tmp_path, payload, channel="broken")
        report = migration.apply(tmp_path)
        assert len(report.anomalies) == 1
        assert "raw/gspace/broken/messages.json" in report.anomalies[0]
        assert reason in report.anomalies[0]
        # The healthy channel is still repaired.
        assert report.actions[0].startswith("raw/gspace/room: 2 item(s) rewritten")

    def test_the_anomaly_counts_messages_from_one(self, tmp_path, migration):
        # The position is how the owner finds the record to repair.
        write_manifest(tmp_path, json.dumps({"messages": [message("alpha", IMAGE), "loose text"]}))
        report = migration.apply(tmp_path)
        assert "position 2" in report.anomalies[0]

    def test_a_message_that_attaches_nothing_never_shifts_an_ordinal(self, tmp_path, migration):
        write_manifest(
            tmp_path,
            [
                message("alpha", IMAGE),
                {"topic_id": "quiet", "text": "no attached_files key at all"},
                {"topic_id": "quiet", "text": "an empty one", "attached_files": []},
                message("bravo", IMAGE),
            ],
        )
        write_file(tmp_path, raw(IMAGE), ZERO)
        write_file(tmp_path, raw("File-image(1).png"), b"one-bytes")
        path = write_item(tmp_path, "bravo", attachments=[raw(IMAGE)])
        migration.apply(tmp_path)
        assert corpus.read_item(path).attachments == [raw("File-image(1).png")]
