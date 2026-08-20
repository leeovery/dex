"""Migration 1 against synthetic fixtures reproducing the wild shapes (§12).

Fixtures are constructed here from the pre-rewrite engine's writer code —
never copied from a real instance (this repo is public; instances are
private). Wild shapes covered: old vocabulary (tweet/blog kinds,
nocaptions/toolong statuses), error-as-reason on non-error statuses, the
``note`` field, ``via: whisper``, missing item/engine fields, and the old
``normalize-config.json``.
"""

import datetime
import json

import pytest

from dex_engine.corpus import read_item
from dex_engine.migrations.migration_1 import _legacy_uhash, build
from dex_engine.pipeline import ledger
from dex_engine.pipeline.types import Kind, LedgerEntry, Need, Status

ENGINE = "0.5.0"


def fixed_today() -> datetime.date:
    return datetime.date(2026, 8, 20)


@pytest.fixture
def migration():
    return build(today=fixed_today, engine_version=ENGINE)


def corpus_text(item_id, *, urls=(), kinds=("web",), enrichment=(), body="**lee**: note\n"):
    lines = [
        "---",
        f"id: {item_id}",
        "source: manual",
        "channel: inbox",
        "shared_by: lee",
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


class TestCorpusRenames:
    def test_kinds_and_enrichment_listing_renamed_body_byte_exact(self, tmp_path, migration):
        body = "**lee** (2026-05-01):\ntrailing spaces  \n\nunicode — em\n"
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
        assert item.kinds == ["x", "web", "youtube"]
        assert item.enrichment == ["x-abc123.md", "web-def456.md", "youtube-aaa111.md"]
        assert item.body == body
        assert any("corpus: 1 item(s) rewritten" in action for action in report.actions)

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


class TestLedgerTranslation:
    def test_done_line_gets_kind_path_item_engine(self, tmp_path, migration):
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
        # §5 forbids reason on done: the choice is stated in the report and
        # the text lives there, never silently dropped.
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
        # engine 0.0.1 is what makes retry-on-new-engine fire (§5/§12).
        assert entry.engine == "0.0.1"

    def test_via_whisper_dropped_with_note(self, tmp_path, migration):
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
        assert any("via 'whisper' dropped" in action for action in report.actions)

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

    def test_unattributable_line_skipped_and_left_in_place(self, tmp_path, migration):
        # No item, no path, no corpus URL hashing to it: not provably safe.
        record = {
            "hash": "3333333333",
            "url": "https://a.test/orphan",
            "kind": "blog",
            "status": "dead",
            "date": "2026-05-01",
        }
        path = write_ledger(tmp_path, [record])
        report = migration.apply(tmp_path)
        assert json.loads(path.read_text().split("\n")[0]) == record
        assert any("no item attribution" in s.why for s in report.skipped)
        # The untranslated line still fails loudly at load — by design.
        with pytest.raises(ledger.LedgerSchemaError):
            ledger.load(path)

    def test_unknown_field_skipped_hand_healed_shape(self, tmp_path, migration):
        record = {
            "hash": "4444444444",
            "url": "https://a.test/h",
            "kind": "blog",
            "status": "done",
            "date": "2026-05-01",
            "healed_by": "lee",
        }
        path = write_ledger(tmp_path, [record])
        report = migration.apply(tmp_path)
        assert json.loads(path.read_text().split("\n")[0]) == record
        assert any("unknown field(s)" in s.why for s in report.skipped)

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

    def test_missing_ledger_is_tolerated(self, tmp_path, migration):
        # Un-pulled repos are a supported input (§12).
        report = migration.apply(tmp_path)
        assert report.skipped == []


class TestConfigRename:
    OLD = (
        '{\n  "name_map": {"u1": "lee"},\n  "internal_domains": ["a.test"],\n'
        '  "noise_prefixes": ["fwd:"]\n}\n'
    )

    def test_renamed_carrying_content(self, tmp_path, migration):
        state = tmp_path / "state"
        state.mkdir()
        (state / "normalize-config.json").write_text(self.OLD)
        report = migration.apply(tmp_path)
        assert not (state / "normalize-config.json").exists()
        assert (state / "config.json").read_text() == self.OLD
        assert any("normalize-config.json → config.json" in action for action in report.actions)

    def test_unknown_key_skipped_for_the_session(self, tmp_path, migration):
        state = tmp_path / "state"
        state.mkdir()
        (state / "normalize-config.json").write_text('{"mystery_knob": true}\n')
        report = migration.apply(tmp_path)
        assert (state / "normalize-config.json").exists()
        assert not (state / "config.json").exists()
        assert any("does not fit the new config schema" in s.why for s in report.skipped)

    def test_identical_racing_copy_settled(self, tmp_path, migration):
        state = tmp_path / "state"
        state.mkdir()
        (state / "normalize-config.json").write_text(self.OLD)
        (state / "config.json").write_text(self.OLD)
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
                }
            ],
        )
        (tmp_path / "state" / "normalize-config.json").write_text('{"name_map": {}}\n')

        migration.apply(tmp_path)
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
