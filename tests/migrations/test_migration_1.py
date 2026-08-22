"""Migration 1 against synthetic fixtures reproducing the wild shapes.

Fixtures are constructed here from the pre-rewrite engine's writer code —
never copied from a real instance (this repo is public; instances are
private). Wild shapes covered: old vocabulary (tweet/blog kinds,
nocaptions/toolong statuses), error-as-reason on non-error statuses, the
``note`` field, ``via: whisper``, missing item/engine fields, and the old
``normalize-config.json``.
"""

import datetime
import json
from typing import ClassVar

import pytest

from dex_engine.corpus import read_item
from dex_engine.migrations.migration_1 import _legacy_uhash, build
from dex_engine.pipeline import ledger
from dex_engine.pipeline.types import Config, Kind, LedgerEntry, Need, Status

ENGINE = "0.5.0"


def fixed_today() -> datetime.date:
    return datetime.date(2026, 8, 20)


def fixed_now() -> datetime.datetime:
    return datetime.datetime(2026, 8, 20, 8, 0, 0, 500000, tzinfo=datetime.UTC)


@pytest.fixture
def migration():
    return build(today=fixed_today, now=fixed_now, engine_version=ENGINE)


def corpus_text(item_id, *, urls=(), kinds=("web",), enrichment=(), body="**alex**: note\n"):
    lines = [
        "---",
        f"id: {item_id}",
        "source: manual",
        "channel: inbox",
        "shared_by: alex",
        "date: 2026-05-01",
    ]
    if urls:
        lines.append("urls:")
        lines.extend(f"  - {url}" for url in urls)
    lines.append(f"kinds: [{', '.join(kinds)}]")
    lines.append("status: raw")
    lines.append(f"enrichment: [{', '.join(enrichment)}]")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def write_ledger(root, records):
    path = root / "state" / "enrichment-ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def write_corpus(root, item_id, **kwargs):
    path = root / "corpus" / item_id[:4] / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(corpus_text(item_id, **kwargs))
    return path


class TestCorpusRenames:
    def test_kinds_and_enrichment_listing_renamed_body_byte_exact(self, tmp_path, migration):
        body = "**alex** (2026-05-01):\ntrailing spaces  \n\nunicode — em\n"
        path = tmp_path / "corpus" / "2026" / "2026-05-01-item-a1b2c3.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            corpus_text(
                "2026-05-01-item-a1b2c3",
                kinds=("tweet", "blog", "youtube"),
                enrichment=("tweet-abc123.md", "blog-def456.md", "youtube-aaa111.md"),
                body=body,
            )
        )
        report = migration.apply(tmp_path)
        item = read_item(path)
        assert item.kinds == ["web", "x", "youtube"]  # normalize's order
        assert item.enrichment == ["x-abc123.md", "web-def456.md", "youtube-aaa111.md"]
        assert item.body == body
        assert any("corpus: 1 item(s) rewritten" in action for action in report.actions)

    def test_kinds_land_in_the_order_normalize_derives_them(self, tmp_path, migration):
        # normalize regenerates kinds as sorted({kind_of(url) …}); leaving
        # them in file order here means the first regeneration after the
        # migration rewrites the item again for ordering alone.
        path = tmp_path / "corpus" / "2026" / "2026-05-01-item-a1b2c3.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            corpus_text("2026-05-01-item-a1b2c3", kinds=("youtube", "tweet", "blog", "tweet"))
        )
        migration.apply(tmp_path)
        assert read_item(path).kinds == ["web", "x", "youtube"]  # sorted, deduped

    def test_already_migrated_item_untouched(self, tmp_path, migration):
        path = tmp_path / "corpus" / "2026" / "2026-05-01-item-a1b2c3.md"
        path.parent.mkdir(parents=True)
        original = corpus_text("2026-05-01-item-a1b2c3", kinds=("x",), enrichment=("x-abc123.md",))
        path.write_text(original)
        report = migration.apply(tmp_path)
        assert path.read_text() == original
        assert report.actions == []

    def test_unparseable_frontmatter_skipped_with_why(self, tmp_path, migration):
        path = tmp_path / "corpus" / "2026" / "broken.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nnot: [valid\n---\nbody\n")
        report = migration.apply(tmp_path)
        assert path.read_text() == "---\nnot: [valid\n---\nbody\n"
        assert any("frontmatter does not parse" in s.why for s in report.skipped)


class TestEnrichmentRenames:
    def test_prefixes_renamed_contents_kept(self, tmp_path, migration):
        item_dir = tmp_path / "enrichment" / "2026-05-01-item-a1b2c3"
        item_dir.mkdir(parents=True)
        (item_dir / "tweet-abc123.md").write_text("tweet content\n")
        (item_dir / "blog-def456.md").write_text("blog content\n")
        (item_dir / "youtube-aaa111.md").write_text("yt content\n")
        report = migration.apply(tmp_path)
        assert (item_dir / "x-abc123.md").read_text() == "tweet content\n"
        assert (item_dir / "web-def456.md").read_text() == "blog content\n"
        assert (item_dir / "youtube-aaa111.md").exists()
        assert not (item_dir / "tweet-abc123.md").exists()
        assert any("2 file(s) renamed" in action for action in report.actions)

    def test_collision_skipped_never_overwrites(self, tmp_path, migration):
        item_dir = tmp_path / "enrichment" / "2026-05-01-item-a1b2c3"
        item_dir.mkdir(parents=True)
        (item_dir / "tweet-abc123.md").write_text("old\n")
        (item_dir / "x-abc123.md").write_text("healed by hand\n")
        report = migration.apply(tmp_path)
        assert (item_dir / "tweet-abc123.md").read_text() == "old\n"
        assert (item_dir / "x-abc123.md").read_text() == "healed by hand\n"
        assert any("refusing to overwrite" in s.why for s in report.skipped)

    def test_collision_keeps_listing_and_ledger_on_the_old_name(self, tmp_path, migration):
        # A skipped disk rename must skip the reference rewrites too:
        # renaming the listing would list x-abc123.md twice, and renaming
        # the ledger path would point the line at the OTHER file's content,
        # stranding tweet-abc123.md on disk unlisted.
        item_id = "2026-05-01-item-a1b2c3"
        other_id = "2026-05-02-other-d4e5f6"
        item_dir = tmp_path / "enrichment" / item_id
        item_dir.mkdir(parents=True)
        (item_dir / "tweet-abc123.md").write_text("old\n")
        (item_dir / "x-abc123.md").write_text("healed by hand\n")
        # The SAME basename under another item has no collision there — the
        # skip is keyed per directory, so this one renames normally.
        other_dir = tmp_path / "enrichment" / other_id
        other_dir.mkdir(parents=True)
        (other_dir / "tweet-abc123.md").write_text("other item's post\n")
        item_path = tmp_path / "corpus" / "2026" / f"{item_id}.md"
        item_path.parent.mkdir(parents=True)
        item_path.write_text(
            corpus_text(item_id, kinds=("tweet",), enrichment=("tweet-abc123.md", "x-abc123.md"))
        )
        other_path = tmp_path / "corpus" / "2026" / f"{other_id}.md"
        other_path.write_text(
            corpus_text(other_id, kinds=("tweet",), enrichment=("tweet-abc123.md",))
        )
        write_ledger(
            tmp_path,
            [
                {
                    "hash": "1111111111",
                    "url": "https://x.com/a/status/1",
                    "kind": "tweet",
                    "status": "done",
                    "date": "2026-05-01",
                    "path": f"enrichment/{item_id}/tweet-abc123.md",
                },
                {
                    "hash": "2222222222",
                    "url": "https://x.com/b/status/2",
                    "kind": "tweet",
                    "status": "done",
                    "date": "2026-05-02",
                    "path": f"enrichment/{other_id}/tweet-abc123.md",
                },
            ],
        )
        report = migration.apply(tmp_path)
        # Disk: the collision pair untouched; the collision-free dir renamed.
        assert (item_dir / "tweet-abc123.md").read_text() == "old\n"
        assert (item_dir / "x-abc123.md").read_text() == "healed by hand\n"
        assert (other_dir / "x-abc123.md").read_text() == "other item's post\n"
        assert not (other_dir / "tweet-abc123.md").exists()
        # Listings follow the disk, not the rename table.
        assert read_item(item_path).enrichment == ["tweet-abc123.md", "x-abc123.md"]
        assert read_item(other_path).enrichment == ["x-abc123.md"]
        # Ledger paths likewise.
        entries = ledger.load(tmp_path / "state" / "enrichment-ledger.jsonl")
        assert entries["1111111111"].path == f"enrichment/{item_id}/tweet-abc123.md"
        assert entries["2222222222"].path == f"enrichment/{other_id}/x-abc123.md"
        # The skip note states that the references still use the old name.
        note = next(s.why for s in report.skipped if "refusing to overwrite" in s.why)
        assert "corpus listings and ledger paths still say tweet-abc123.md" in note


class TestLedgerTranslation:
    def test_done_line_gets_kind_path_item_engine(self, tmp_path, migration):
        write_corpus(tmp_path, "2026-05-01-item-a1b2c3")
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "73bd784849",
                    "url": "https://x.com/a/status/1",
                    "kind": "tweet",
                    "status": "done",
                    "date": "2026-05-01",
                    "title": "a post",
                    "path": "enrichment/2026-05-01-item-a1b2c3/tweet-73bd78.md",
                }
            ],
        )
        migration.apply(tmp_path)
        entry = ledger.load(path)["73bd784849"]
        assert entry.kind is Kind.X
        assert entry.path == "enrichment/2026-05-01-item-a1b2c3/x-73bd78.md"
        assert entry.item == "2026-05-01-item-a1b2c3"
        assert entry.engine == "0.0.1"
        assert entry.title == "a post"
        assert entry.date == datetime.date(2026, 5, 1)

    def test_retired_statuses_become_waiting_transcribe(self, tmp_path, migration):
        url = "https://www.youtube.com/watch?v=abc"
        unit_hash = _legacy_uhash(url)
        item_path = tmp_path / "corpus" / "2026" / "2026-05-01-item-a1b2c3.md"
        item_path.parent.mkdir(parents=True)
        item_path.write_text(
            corpus_text("2026-05-01-item-a1b2c3", urls=(url,), kinds=("youtube",))
        )
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": unit_hash,
                    "url": url,
                    "kind": "youtube",
                    "status": "nocaptions",
                    "date": "2026-05-01",
                },
                {
                    "hash": "bbbbbbbbbb",
                    "url": url,
                    "kind": "youtube",
                    "status": "toolong",
                    "date": "2026-05-01",
                    "path": "enrichment/2026-05-01-item-a1b2c3/nothing.md",
                },
            ],
        )
        migration.apply(tmp_path)
        entries = ledger.load(path)
        # nocaptions: no path — the item comes from hashing corpus URLs with
        # the OLD canonicalization (the hashes were minted with it).
        assert entries[unit_hash].status is Status.WAITING
        assert entries[unit_hash].needs is Need.TRANSCRIBE
        assert entries[unit_hash].item == "2026-05-01-item-a1b2c3"
        assert entries["bbbbbbbbbb"].status is Status.WAITING
        assert entries["bbbbbbbbbb"].needs is Need.TRANSCRIBE

    def test_error_as_reason_ported_on_dead_and_skipped(self, tmp_path, migration):
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "aaaaaaaaaa",
                    "url": "https://a.test/gone",
                    "kind": "blog",
                    "status": "dead",
                    "date": "2026-05-01",
                    "error": "HTTP Error 404: Not Found",
                    "item": "2026-05-01-item-a1b2c3",
                },
                {
                    "hash": "cccccccccc",
                    "url": "https://a.test/skipped",
                    "kind": "blog",
                    "status": "skipped",
                    "date": "2026-05-01",
                    "error": "paywalled, owner said skip",
                    "item": "2026-05-01-item-a1b2c3",
                },
            ],
        )
        migration.apply(tmp_path)
        entries = ledger.load(path)
        assert entries["aaaaaaaaaa"].kind is Kind.WEB
        assert entries["aaaaaaaaaa"].reason == "HTTP Error 404: Not Found"
        assert entries["cccccccccc"].reason == "paywalled, owner said skip"

    def test_note_field_ported_into_reason(self, tmp_path, migration):
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "dddddddddd",
                    "url": "https://a.test/m",
                    "kind": "paper",
                    "status": "manual",
                    "date": "2026-05-01",
                    "note": "abstract only",
                    "item": "2026-05-01-item-a1b2c3",
                }
            ],
        )
        migration.apply(tmp_path)
        assert ledger.load(path)["dddddddddd"].reason == "abstract only"

    def test_manual_without_reason_gets_unstated(self, tmp_path, migration):
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "eeeeeeeeee",
                    "url": "https://a.test/m2",
                    "kind": "blog",
                    "status": "manual",
                    "date": "2026-05-01",
                    "item": "2026-05-01-item-a1b2c3",
                }
            ],
        )
        migration.apply(tmp_path)
        assert ledger.load(path)["eeeeeeeeee"].reason == "unstated (pre-migration)"

    def test_done_error_text_preserved_in_report_not_line(self, tmp_path, migration):
        # The schema forbids reason on done: the choice is stated in the report and
        # the text lives there, never silently dropped.
        write_corpus(tmp_path, "2026-05-01-item-a1b2c3")
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "f0f0f0f0f0",
                    "url": "https://a.test/d",
                    "kind": "blog",
                    "status": "done",
                    "date": "2026-05-01",
                    "error": "wobbly fetch, retried by hand",
                    "path": "enrichment/2026-05-01-item-a1b2c3/blog-f0f0f0.md",
                }
            ],
        )
        report = migration.apply(tmp_path)
        entry = ledger.load(path)["f0f0f0f0f0"]
        assert entry.reason is None
        assert entry.error is None
        assert any(
            "wobbly fetch, retried by hand" in action and "forbidden on done" in action
            for action in report.actions
        )

    def test_error_status_without_message_gets_unrecorded(self, tmp_path, migration):
        # The old whisper flow could append {**e, status: "error"} with no
        # error text; the new schema requires one.
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "abcdefabcd",
                    "url": "https://a.test/e",
                    "kind": "youtube",
                    "status": "error",
                    "date": "2026-05-01",
                    "item": "2026-05-01-item-a1b2c3",
                }
            ],
        )
        migration.apply(tmp_path)
        entry = ledger.load(path)["abcdefabcd"]
        assert entry.status is Status.ERROR
        assert entry.error == "unrecorded (pre-migration error)"
        # engine 0.0.1 is what makes retry-on-new-engine fire.
        assert entry.engine == "0.0.1"

    def test_via_whisper_dropped_with_note(self, tmp_path, migration):
        write_corpus(tmp_path, "2026-05-01-item-a1b2c3")
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "1234567890",
                    "url": "https://a.test/w",
                    "kind": "youtube",
                    "status": "done",
                    "date": "2026-05-01",
                    "via": "whisper",
                    "path": "enrichment/2026-05-01-item-a1b2c3/youtube-123456.md",
                }
            ],
        )
        report = migration.apply(tmp_path)
        assert ledger.load(path)["1234567890"].via is None
        assert any(
            "via 'whisper' dropped — pre-migration provenance" in action
            for action in report.actions
        )

    def test_via_fxtwitter_drops_the_field_not_the_line(self, tmp_path, migration):
        # The old x fetcher stamped its transport ('fxtwitter') as via — 15
        # wild lines on the flagship instance. Any non-current via drops with
        # a note; dropping the whole line would forfeit its migration-2 reseed.
        write_corpus(tmp_path, "2026-05-01-item-a1b2c3", kinds=("x",))
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "6666666666",
                    "url": "https://x.com/a/status/2",
                    "kind": "tweet",
                    "status": "done",
                    "date": "2026-05-01",
                    "via": "fxtwitter",
                    "path": "enrichment/2026-05-01-item-a1b2c3/tweet-666666.md",
                }
            ],
        )
        report = migration.apply(tmp_path)
        entry = ledger.load(path)["6666666666"]
        assert entry.kind is Kind.X
        assert entry.status is Status.DONE
        assert entry.via is None
        assert any("via 'fxtwitter' dropped" in action for action in report.actions)
        assert report.skipped == []

    def test_stray_title_on_non_done_dropped_with_note(self, tmp_path, migration):
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "2222222222",
                    "url": "https://a.test/t",
                    "kind": "paper",
                    "status": "error",
                    "date": "2026-05-01",
                    "title": "a paper",
                    "error": "boom",
                    "item": "2026-05-01-item-a1b2c3",
                }
            ],
        )
        report = migration.apply(tmp_path)
        assert ledger.load(path)["2222222222"].title is None
        assert any("stray title" in action for action in report.actions)

    def test_identical_notes_collapse_to_one_with_a_count(self, tmp_path, migration):
        # Superseded lines of one hash repeat the same translation verbatim
        # (last-per-hash keeps the audit trail) — the report says it once,
        # multiplicity kept, nothing dropped.
        record = {
            "hash": "2222222222",
            "url": "https://a.test/t",
            "kind": "paper",
            "status": "error",
            "date": "2026-05-01",
            "title": "a paper",
            "error": "boom",
            "item": "2026-05-01-item-a1b2c3",
        }
        other = dict(record, hash="3333333333", url="https://a.test/u", title="another")
        write_ledger(tmp_path, [record] * 8 + [other])
        report = migration.apply(tmp_path)
        stray = [action for action in report.actions if "stray title" in action]
        assert len(stray) == 2  # one per distinct note, not one per line
        assert any("(x8)" in action and "'a paper'" in action for action in stray)
        assert any("(x" not in action and "'another'" in action for action in stray)

    def test_unattributable_line_is_dropped_and_named(self, tmp_path, migration):
        # No item, no path, no enrichment file on disk, no corpus URL hashing
        # to it. Nothing can own it, so it goes — the corpus re-raises
        # anything that still matters, and git history holds the old ledger.
        record = {
            "hash": "3333333333",
            "url": "https://a.test/orphan",
            "kind": "blog",
            "status": "dead",
            "date": "2026-05-01",
        }
        good = {
            "hash": "aaaaaaaaaa",
            "url": "https://a.test/fine",
            "kind": "blog",
            "status": "dead",
            "date": "2026-05-01",
            "item": "2026-05-01-item-a1b2c3",
        }
        path = write_ledger(tmp_path, [record, good])
        report = migration.apply(tmp_path)
        assert report.skipped == []  # nothing is left for a human
        summary = next(a for a in report.actions if "untranslatable line(s) dropped" in a)
        assert "1 untranslatable" in summary
        assert "git history" in summary
        named = next(a for a in report.actions if a.startswith("ledger dropped "))
        assert "3333333333" in named
        assert "https://a.test/orphan" in named
        assert "no item attribution" in named
        entries = ledger.load(path)
        assert set(entries) == {"aaaaaaaaaa"}

    def test_no_residue_file_is_created(self, tmp_path, migration):
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "3333333333",
                    "url": "https://a.test/orphan",
                    "kind": "blog",
                    "status": "dead",
                    "date": "2026-05-01",
                }
            ],
        )
        migration.apply(tmp_path)
        assert [p.name for p in path.parent.iterdir()] == ["enrichment-ledger.jsonl"]

    def test_the_dropped_list_is_capped_with_the_remainder_counted(self, tmp_path, migration):
        records = [
            {
                "hash": f"{index:010d}",
                "url": f"https://a.test/orphan-{index}",
                "kind": "blog",
                "status": "dead",
                "date": "2026-05-01",
            }
            for index in range(9)
        ]
        write_ledger(tmp_path, records)
        report = migration.apply(tmp_path)
        named = [a for a in report.actions if a.startswith("ledger dropped ")]
        assert len(named) == 5
        assert any("and 4 further dropped line(s)" in a for a in report.actions)

    def test_unknown_field_is_dropped_hand_healed_shape(self, tmp_path, migration):
        record = {
            "hash": "4444444444",
            "url": "https://a.test/h",
            "kind": "blog",
            "status": "done",
            "date": "2026-05-01",
            "healed_by": "alex",
        }
        path = write_ledger(tmp_path, [record])
        report = migration.apply(tmp_path)
        assert any("unknown field(s)" in a for a in report.actions if "ledger dropped" in a)
        assert ledger.load(path) == {}

    def test_an_output_on_disk_attributes_a_line_that_names_none(self, tmp_path, migration):
        # The line records no item and no path, but the file it produced sits
        # under enrichment/<item>/ named for its own hash — that IS the owner.
        write_corpus(tmp_path, "2026-05-01-item-a1b2c3")
        enrich_dir = tmp_path / "enrichment" / "2026-05-01-item-a1b2c3"
        enrich_dir.mkdir(parents=True)
        (enrich_dir / "blog-555555.md").write_text("content\n")
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "5555550000",
                    "url": "https://a.test/attributable",
                    "kind": "blog",
                    "status": "dead",
                    "date": "2026-05-01",
                }
            ],
        )
        report = migration.apply(tmp_path)
        assert not any("dropped" in a and "untranslatable" in a for a in report.actions)
        assert ledger.load(path)["5555550000"].item == "2026-05-01-item-a1b2c3"

    def test_a_hash_prefix_under_two_items_is_never_guessed(self, tmp_path, migration):
        # Six hex digits collide eventually; attributing to the wrong item
        # would write the next rerun's output into the wrong tree.
        for item in ("2026-05-01-item-a1b2c3", "2026-05-02-item-d4e5f6"):
            enrich_dir = tmp_path / "enrichment" / item
            enrich_dir.mkdir(parents=True)
            (enrich_dir / "blog-555555.md").write_text("content\n")
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "5555550000",
                    "url": "https://a.test/ambiguous",
                    "kind": "blog",
                    "status": "dead",
                    "date": "2026-05-01",
                }
            ],
        )
        report = migration.apply(tmp_path)
        assert any("untranslatable line(s) dropped" in a for a in report.actions)
        assert ledger.load(path) == {}

    def test_no_phantom_notes_from_dropped_lines(self, tmp_path, migration):
        # A line that buffers a note (here: the done error-as-reason port,
        # which runs before item attribution) but then fails translation must
        # contribute nothing to actions — notes flush only on success.
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "3333333333",
                    "url": "https://a.test/orphan",
                    "kind": "blog",
                    "status": "done",
                    "date": "2026-05-01",
                    "error": "flaky, hand-checked",
                    # no item, no path, no disk output, no corpus owner
                }
            ],
        )
        report = migration.apply(tmp_path)
        assert not any("preserved here" in action for action in report.actions)
        assert any("untranslatable line(s) dropped" in action for action in report.actions)
        assert path.read_text() == ""  # sole line dropped; ledger empty but clean

    def test_the_enrichment_tree_outranks_a_stale_recorded_path(self, tmp_path, migration):
        # The item was renamed after the line was written — same shortid, new
        # slug — so the recorded path names a directory that is gone while the
        # output it produced sits under the new id. Attributing to the string
        # would name an id no corpus file answers to.
        live_id = "2026-05-01-renamed-a1b2c3"
        write_corpus(tmp_path, live_id, kinds=("youtube",))
        enrich_dir = tmp_path / "enrichment" / live_id
        enrich_dir.mkdir(parents=True)
        (enrich_dir / "youtube-555555.md").write_text("transcript\n")
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "5555550000",
                    "url": "https://a.test/renamed",
                    "kind": "youtube",
                    "status": "done",
                    "date": "2026-05-01",
                    "path": "enrichment/2026-05-01-old-slug-a1b2c3/youtube-555555.md",
                }
            ],
        )
        migration.apply(tmp_path)
        entry = ledger.load(path)["5555550000"]
        assert entry.item == live_id
        assert entry.path == f"enrichment/{live_id}/youtube-555555.md"

    def test_a_recorded_path_survives_where_its_item_is_still_live(self, tmp_path, migration):
        # Nothing on disk carries the hash, so the recorded path is the only
        # provenance there is — and the item it names is still in the corpus,
        # so the work is still owned. Repointing the path would be a guess.
        write_corpus(tmp_path, "2026-05-01-old-slug-a1b2c3", kinds=("youtube",))
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "6666660000",
                    "url": "https://a.test/gone",
                    "kind": "youtube",
                    "status": "done",
                    "date": "2026-05-01",
                    "path": "enrichment/2026-05-01-old-slug-a1b2c3/youtube-666666.md",
                }
            ],
        )
        migration.apply(tmp_path)
        entry = ledger.load(path)["6666660000"]
        assert entry.item == "2026-05-01-old-slug-a1b2c3"
        assert entry.path == "enrichment/2026-05-01-old-slug-a1b2c3/youtube-666666.md"

    def test_a_line_attributable_only_to_a_dead_item_is_dropped(self, tmp_path, migration):
        # The owner excluded the item: the corpus file and enrichment/<id>/
        # went, and only the recorded path string is left saying the name.
        # Reading the id back out of it attributes finished work to something
        # that no longer exists, and every later stage then has to reason
        # about a line nothing can own.
        write_corpus(tmp_path, "2026-05-01-live-a1b2c3")
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "7777770000",
                    "url": "https://a.test/excluded",
                    "kind": "blog",
                    "status": "done",
                    "date": "2026-05-01",
                    "path": "enrichment/2026-05-01-excluded-999999/blog-777777.md",
                }
            ],
        )
        report = migration.apply(tmp_path)
        assert report.skipped == []
        named = next(a for a in report.actions if a.startswith("ledger dropped "))
        assert "7777770000" in named
        assert "2026-05-01-excluded-999999" in named
        assert ledger.load(path) == {}

    def test_a_dead_recorded_path_yields_to_the_live_item_listing_the_url(
        self, tmp_path, migration
    ):
        # The rescue is for live items only, but it is a real rescue: the
        # recorded path names an id with no corpus file while a live item
        # still lists the URL, so the work is that item's and stays.
        url = "https://a.test/still-listed"
        write_corpus(tmp_path, "2026-05-01-claimant-a1b2c3", urls=(url,))
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": _legacy_uhash(url),
                    "url": url,
                    "kind": "blog",
                    "status": "dead",
                    "date": "2026-05-01",
                    "path": "enrichment/2026-05-01-old-slug-999999/blog-abcdef.md",
                }
            ],
        )
        report = migration.apply(tmp_path)
        assert not any("untranslatable line(s) dropped" in a for a in report.actions)
        assert ledger.load(path)[_legacy_uhash(url)].item == "2026-05-01-claimant-a1b2c3"

    def test_an_orphaned_enrichment_directory_never_attributes_a_line(
        self, tmp_path, migration
    ):
        # The tree is asked first, but it answers with a directory, not with
        # an item: a corpus file deleted while enrichment/<id>/ was left
        # behind is residue, and residue must not own work.
        enrich_dir = tmp_path / "enrichment" / "2026-05-01-orphan-999999"
        enrich_dir.mkdir(parents=True)
        (enrich_dir / "blog-888888.md").write_text("content\n")
        write_corpus(tmp_path, "2026-05-01-live-a1b2c3")
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "8888880000",
                    "url": "https://a.test/orphaned-tree",
                    "kind": "blog",
                    "status": "dead",
                    "date": "2026-05-01",
                }
            ],
        )
        report = migration.apply(tmp_path)
        named = next(a for a in report.actions if a.startswith("ledger dropped "))
        assert "2026-05-01-orphan-999999" in named
        assert ledger.load(path) == {}

    def test_malformed_corpus_url_never_aborts_the_migration(self, tmp_path, migration):
        # urlsplit raises ValueError on an invalid IPv6 literal; one
        # hand-healed URL must not take down the whole owners scan.
        good_url = "https://a.test/fine"
        item_path = tmp_path / "corpus" / "2026" / "2026-05-01-item-a1b2c3.md"
        item_path.parent.mkdir(parents=True)
        item_path.write_text(
            corpus_text(
                "2026-05-01-item-a1b2c3",
                urls=("https://[invalid-ipv6/broken", good_url),
                kinds=("web",),
            )
        )
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": _legacy_uhash(good_url),
                    "url": good_url,
                    "kind": "blog",
                    "status": "dead",
                    "date": "2026-05-01",
                }
            ],
        )
        migration.apply(tmp_path)
        assert ledger.load(path)[_legacy_uhash(good_url)].item == "2026-05-01-item-a1b2c3"

    def test_current_schema_lines_pass_through_unchanged(self, tmp_path, migration):
        entry = LedgerEntry(
            hash="5555555555",
            url="https://a.test/new",
            item="2026-05-01-item-a1b2c3",
            kind=Kind.WEB,
            status=Status.MANUAL,
            reason="thin-extraction",
            engine="0.4.0",
            date=datetime.date(2026, 8, 1),
        )
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        ledger.append(path, entry)
        original = path.read_text()
        migration.apply(tmp_path)
        assert path.read_text() == original

    def test_current_schema_capped_skip_survives_a_re_apply(self, tmp_path, migration):
        # The applied-migrations log can be lost (union-merge race, un-pulled
        # repo), so migration 1 re-runs over lines the rewritten engine wrote.
        # A cap-refused skip is one of those lines: dropping it would erase
        # a live marker from the ledger it came from.
        entry = LedgerEntry(
            hash="6666666666",
            url="https://a.test/capped",
            item="2026-05-01-item-a1b2c3",
            kind=Kind.WEB,
            status=Status.SKIPPED,
            capped=True,
            reason="media cap reached for this item",
            engine="0.4.0",
            date=datetime.date(2026, 8, 1),
        )
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        ledger.append(path, entry)
        original = path.read_text()
        report = migration.apply(tmp_path)
        assert path.read_text() == original
        assert report.skipped == []
        assert not any("dropped" in action for action in report.actions)
        assert ledger.load(path)["6666666666"].capped is True

    def test_current_schema_write_timestamp_survives_a_re_apply(self, tmp_path, migration):
        # Same re-apply exposure as `capped`: a key missing from the
        # tolerated list drops every line the running engine wrote.
        at = datetime.datetime(2026, 8, 20, 9, 0, 0, 125000, tzinfo=datetime.UTC)
        entry = LedgerEntry(
            hash="7777777777",
            url="https://a.test/stamped",
            item="2026-05-01-item-a1b2c3",
            kind=Kind.WEB,
            status=Status.QUEUED,
            engine="0.4.0",
            date=datetime.date(2026, 8, 20),
            at=at,
        )
        path = tmp_path / "state" / "enrichment-ledger.jsonl"
        ledger.append(path, entry)
        original = path.read_text()
        report = migration.apply(tmp_path)
        assert path.read_text() == original
        assert report.skipped == []
        assert ledger.load(path)["7777777777"].at == at

    @pytest.mark.parametrize("junk", ["yesterday", "2026-08-20T09:00:00"])
    def test_a_junk_write_timestamp_costs_the_value_not_the_line(
        self, tmp_path, migration, junk
    ):
        # `at` breaks ties between concurrent writes; it is not load-bearing
        # like date/status/kind/item. Dropping a line's whole work history
        # over a malformed tie-breaker (unparseable, or naive and so
        # uncomparable) would protect nothing.
        path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "8888888888",
                    "url": "https://a.test/junk-at",
                    "item": "2026-05-01-item-a1b2c3",
                    "kind": "web",
                    "status": "manual",
                    "reason": "owner ruled",
                    "engine": "0.4.0",
                    "date": "2026-08-20",
                    "at": junk,
                }
            ],
        )
        report = migration.apply(tmp_path)
        assert not any("dropped" in a and "untranslatable" in a for a in report.actions)
        assert any(
            f"unusable write timestamp {junk!r} dropped" in action for action in report.actions
        )
        entry = ledger.load(path)["8888888888"]
        assert entry.at is None
        assert (entry.status, entry.reason, entry.item, entry.engine) == (
            Status.MANUAL,
            "owner ruled",
            "2026-05-01-item-a1b2c3",
            "0.4.0",
        )
        assert entry.date == datetime.date(2026, 8, 20)

    def test_missing_ledger_is_tolerated(self, tmp_path, migration):
        # Un-pulled repos are a supported input.
        report = migration.apply(tmp_path)
        assert report.skipped == []


class TestConfigRename:
    OLD = (
        '{\n  "name_map": {"u1": "alex"},\n  "internal_domains": ["a.test"],\n'
        '  "noise_prefixes": ["fwd:"]\n}\n'
    )
    # What the migration writes: OLD minus name_map (dropped, not carried).
    CLEANED: ClassVar[dict] = {"internal_domains": ["a.test"], "noise_prefixes": ["fwd:"]}

    def test_renamed_carrying_content_minus_name_map(self, tmp_path, migration):
        state = tmp_path / "state"
        state.mkdir()
        (state / "normalize-config.json").write_text(self.OLD)
        report = migration.apply(tmp_path)
        assert not (state / "normalize-config.json").exists()
        assert json.loads((state / "config.json").read_text()) == self.CLEANED
        Config.load(state / "config.json")  # the migrated file parses loudly clean
        assert any("normalize-config.json → config.json" in action for action in report.actions)
        assert any(
            "name_map removed — never applied by any engine version" in action
            and "Discord/Space backfill" in action
            for action in report.actions
        )

    def test_config_without_name_map_gets_no_note(self, tmp_path, migration):
        state = tmp_path / "state"
        state.mkdir()
        (state / "normalize-config.json").write_text('{"internal_domains": ["a.test"]}\n')
        report = migration.apply(tmp_path)
        assert json.loads((state / "config.json").read_text()) == {
            "internal_domains": ["a.test"]
        }
        assert not any("name_map" in action for action in report.actions)

    def test_unknown_key_skipped_for_the_session(self, tmp_path, migration):
        state = tmp_path / "state"
        state.mkdir()
        (state / "normalize-config.json").write_text('{"mystery_knob": true}\n')
        report = migration.apply(tmp_path)
        assert (state / "normalize-config.json").exists()
        assert not (state / "config.json").exists()
        assert any("does not fit the new config schema" in s.why for s in report.skipped)

    def test_identical_racing_copy_settled(self, tmp_path, migration):
        # The racing machine ran THIS migration first, so its config.json
        # already holds the cleaned (name_map-free) content.
        state = tmp_path / "state"
        state.mkdir()
        (state / "normalize-config.json").write_text(self.OLD)
        (state / "config.json").write_text(json.dumps(self.CLEANED, indent=2) + "\n")
        report = migration.apply(tmp_path)
        assert not (state / "normalize-config.json").exists()
        assert any("racing machine" in action for action in report.actions)

    def test_diverged_copies_skipped(self, tmp_path, migration):
        state = tmp_path / "state"
        state.mkdir()
        (state / "normalize-config.json").write_text(self.OLD)
        (state / "config.json").write_text('{"name_map": {}}\n')
        report = migration.apply(tmp_path)
        assert (state / "normalize-config.json").exists()
        assert any("merge the two by hand" in s.why for s in report.skipped)


class TestIdempotency:
    def test_second_apply_is_a_clean_noop(self, tmp_path, migration):
        item_path = tmp_path / "corpus" / "2026" / "2026-05-01-item-a1b2c3.md"
        item_path.parent.mkdir(parents=True)
        item_path.write_text(
            corpus_text(
                "2026-05-01-item-a1b2c3",
                kinds=("tweet",),
                enrichment=("tweet-73bd78.md",),
            )
        )
        enrich_dir = tmp_path / "enrichment" / "2026-05-01-item-a1b2c3"
        enrich_dir.mkdir(parents=True)
        (enrich_dir / "tweet-73bd78.md").write_text("content\n")
        ledger_path = write_ledger(
            tmp_path,
            [
                {
                    "hash": "73bd784849",
                    "url": "https://x.com/a/status/1",
                    "kind": "tweet",
                    "status": "done",
                    "date": "2026-05-01",
                    "path": "enrichment/2026-05-01-item-a1b2c3/tweet-73bd78.md",
                },
                # An unattributable line: dropped on the first apply, and the
                # second apply must report NOTHING for it (no phantom drops,
                # no phantom actions) — it is gone from the file.
                {
                    "hash": "3333333333",
                    "url": "https://a.test/orphan",
                    "kind": "blog",
                    "status": "dead",
                    "date": "2026-05-01",
                },
            ],
        )
        (tmp_path / "state" / "normalize-config.json").write_text('{"name_map": {}}\n')

        first = migration.apply(tmp_path)
        assert first.skipped == []
        assert sum("untranslatable line(s) dropped" in a for a in first.actions) == 1
        snapshot = {
            "item": item_path.read_bytes(),
            "ledger": ledger_path.read_bytes(),
            "config": (tmp_path / "state" / "config.json").read_bytes(),
            "enrichment": sorted(p.name for p in enrich_dir.iterdir()),
        }
        second = migration.apply(tmp_path)
        assert second.actions == []
        assert second.skipped == []
        assert item_path.read_bytes() == snapshot["item"]
        assert ledger_path.read_bytes() == snapshot["ledger"]
        assert (tmp_path / "state" / "config.json").read_bytes() == snapshot["config"]
        assert sorted(p.name for p in enrich_dir.iterdir()) == snapshot["enrichment"]
        # No residue: the migration leaves state/ holding only what it owns.
        assert sorted(p.name for p in ledger_path.parent.iterdir()) == [
            "config.json",
            "enrichment-ledger.jsonl",
        ]
