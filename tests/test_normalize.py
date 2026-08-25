"""Tests for normalize.py: export parsing, clustering, shared detection."""

import dataclasses
import hashlib
import json

import pytest

from dex_engine import corpus
from dex_engine.normalize import (
    build_parser,
    kind_of,
    load_exclusions,
    main,
    run_normalize,
)
from dex_engine.pipeline.capture import slugify
from dex_engine.pipeline.registry import default_drivers
from dex_engine.pipeline.types import Config, Instance

CHANNEL = "general"


def message(  # noqa: PLR0913 — a fixture builder mirrors the export's fields
    msg_id: str,
    content: str,
    *,
    author_id: str = "u1",
    name: str = "alex",
    nickname: str | None = "Alex",
    timestamp: str = "2026-08-19T10:00:00+00:00",
    msg_type: str = "Default",
    reference: str | None = None,
    attachments: list | None = None,
    embeds: list | None = None,
    reactions: list | None = None,
) -> dict:
    return {
        "id": msg_id,
        "type": msg_type,
        "content": content,
        "timestamp": timestamp,
        "author": {"id": author_id, "name": name, "nickname": nickname},
        "reference": {"messageId": reference} if reference else None,
        "attachments": attachments or [],
        "embeds": embeds or [],
        "reactions": reactions or [],
    }


def write_export(instance: Instance, messages: list[dict], channel: str = CHANNEL) -> None:
    chan_dir = instance.root / "raw" / "discord" / channel
    chan_dir.mkdir(parents=True, exist_ok=True)
    (chan_dir / "messages.json").write_text(json.dumps({"messages": messages}))


def shortid(msg_id: str, channel: str = CHANNEL) -> str:
    return hashlib.sha1(f"{channel}/{msg_id}".encode()).hexdigest()[:6]  # noqa: S324 — mirrors normalize


def items(instance: Instance) -> dict[str, corpus.CorpusItem]:
    return {path.stem: corpus.read_item(path) for path in instance.corpus_dir.glob("*/*.md")}


SUBSTANTIAL = "an observation with enough substance to be knowledge, " * 6


class TestHelpers:
    def test_slugify_bounds_and_floors(self):
        assert slugify("A Great Article: Part 2!") == "a-great-article-part-2"
        assert slugify("!!!") == "untitled"
        assert len(slugify("word " * 30)) <= 40

    def test_kind_of_uses_the_shared_registry(self):
        drivers = default_drivers()
        assert kind_of("https://www.youtube.com/watch?v=abc", drivers) == "youtube"
        assert kind_of("https://m.youtube.com/watch?v=abc", drivers) == "youtube"  # healed split
        assert kind_of("https://gist.github.com/a/b", drivers) == "github"  # the healed gist gap
        assert kind_of("https://x.com/a/status/1", drivers) == "x"  # post-rename vocabulary
        assert kind_of("https://example.test/post", drivers) == "web"

    def test_load_exclusions_matches_trailing_shortid(self, tmp_path):
        path = tmp_path / "exclusions.tsv"
        path.write_text("2026-08-19-old-slug-abc123\tout of scope\n\n")
        assert load_exclusions(path) == {"abc123"}
        assert load_exclusions(tmp_path / "absent.tsv") == set()


class TestNormalize:
    def test_link_share_becomes_an_item(self, instance):
        write_export(
            instance,
            [message("m1", "https://example.test/post\ngreat read")],
        )
        lines = run_normalize(instance, Config())
        assert lines == ["discord/general: 1 items written, 0 clusters skipped"]
        item = next(iter(items(instance).values()))
        assert item.source == "discord"
        assert item.channel == CHANNEL
        assert item.shared_by == "Alex"  # nickname over name
        assert item.urls == ["https://example.test/post"]
        assert item.kinds == ["web"]
        assert item.status == "raw"
        assert item.id.endswith(shortid("m1"))
        assert "**Alex** (2026-08-19 10:00):" in item.body
        assert "great read" in item.body

    def test_same_author_burst_clusters_and_reply_joins_target(self, instance):
        write_export(
            instance,
            [
                message("m1", "https://example.test/a"),
                message("m2", "more context", timestamp="2026-08-19T10:05:00+00:00"),
                message(
                    "m3",
                    "reply from someone else",
                    author_id="u2",
                    name="sam",
                    nickname=None,
                    msg_type="Reply",
                    reference="m1",
                    timestamp="2026-08-19T11:00:00+00:00",
                ),
            ],
        )
        run_normalize(instance, Config())
        built = items(instance)
        assert len(built) == 1  # one cluster, one item
        body = next(iter(built.values())).body
        assert "more context" in body
        assert "**sam**" in body  # reply attributed, no nickname

    def test_gap_splits_clusters(self, instance):
        write_export(
            instance,
            [
                message("m1", "https://example.test/a"),
                message(
                    "m2",
                    "https://example.test/b",
                    timestamp="2026-08-19T10:30:00+00:00",
                ),
            ],
        )
        run_normalize(instance, Config())
        assert len(items(instance)) == 2

    def test_thin_chatter_is_skipped(self, instance):
        write_export(instance, [message("m1", "lol nice")])
        lines = run_normalize(instance, Config())
        assert lines == ["discord/general: 0 items written, 1 clusters skipped"]

    def test_substantial_text_without_links_is_kept_as_text(self, instance):
        write_export(instance, [message("m1", SUBSTANTIAL)])
        run_normalize(instance, Config())
        item = next(iter(items(instance).values()))
        assert item.kinds == ["text"]
        assert item.urls == []

    def test_internal_domains_filter_urls(self, instance):
        write_export(instance, [message("m1", "https://chat.corp.test/thread/1")])
        lines = run_normalize(instance, Config(internal_domains=["corp.test"]))
        assert lines == ["discord/general: 0 items written, 1 clusters skipped"]

    def test_embed_title_names_the_slug(self, instance):
        write_export(
            instance,
            [
                message(
                    "m1",
                    "https://example.test/post",
                    embeds=[{"url": "https://example.test/post", "title": "A Great Article"}],
                )
            ],
        )
        run_normalize(instance, Config())
        (item_id,) = items(instance)
        assert item_id == f"2026-08-19-a-great-article-{shortid('m1')}"

    def test_unfurl_attachments_and_reactions_recorded(self, instance):
        write_export(
            instance,
            [
                message(
                    "m1",
                    "https://example.test/post",
                    embeds=[{"url": "https://example.test/post", "title": "A Great Article"}],
                    attachments=[{"url": "assets/photo.png", "fileName": "photo.png"}],
                    reactions=[{"count": 3}, {"count": 2}],
                )
            ],
        )
        run_normalize(instance, Config())
        item = next(iter(items(instance).values()))
        assert item.reactions == 5
        assert item.attachments == [f"raw/discord/{CHANNEL}/assets/photo.png"]
        assert "> unfurl: A Great Article" in item.body
        assert "*[attached: photo.png]*" in item.body

    def test_excluded_shortids_never_regenerate(self, instance):
        write_export(instance, [message("m1", "https://example.test/post")])
        instance.state_dir.mkdir(exist_ok=True)
        (instance.state_dir / "exclusions.tsv").write_text(
            f"2026-08-19-old-name-{shortid('m1')}\tout of scope\n"
        )
        lines = run_normalize(instance, Config())
        assert lines == ["discord/general: 0 items written, 1 clusters skipped"]
        assert items(instance) == {}

    def test_no_exports_is_loud(self, instance):
        with pytest.raises(ValueError, match="no exports found"):
            run_normalize(instance, Config())


class TestVariantExports:
    """One unreadable export never aborts the rest — the containment rule."""

    def write_raw(self, instance: Instance, channel: str, text: str) -> None:
        chan_dir = instance.root / "raw" / "discord" / channel
        chan_dir.mkdir(parents=True, exist_ok=True)
        (chan_dir / "messages.json").write_text(text)

    def test_a_data_package_dump_is_named_and_the_run_continues(self, instance):
        # Discord's own data package writes a bare array — under the same
        # messages.json name the exporter's channel export uses.
        write_export(instance, [message("m1", "https://example.test/post")])
        self.write_raw(
            instance,
            "exported-by-hand",
            json.dumps([{"ID": "1", "Timestamp": "2026-08-19", "Contents": "hi"}]),
        )
        lines = run_normalize(instance, Config())
        assert "discord/general: 1 items written, 0 clusters skipped" in lines
        assert any(
            line.startswith("discord/exported-by-hand: export unreadable")
            and "not a DiscordChatExporter JSON export" in line
            for line in lines
        )
        assert len(items(instance)) == 1

    def test_a_truncated_export_is_named_and_the_run_continues(self, instance):
        # The exporter flushes as it goes: an interrupted export leaves a
        # messages.json whose JSON stops mid-object.
        write_export(instance, [message("m1", "https://example.test/post")])
        self.write_raw(instance, "interrupted", '{"messages": [{"id": "m1", "typ')
        lines = run_normalize(instance, Config())
        assert "discord/general: 1 items written, 0 clusters skipped" in lines
        assert any(line.startswith("discord/interrupted: export unreadable") for line in lines)
        assert len(items(instance)) == 1

    @pytest.mark.parametrize(
        ("missing", "reason"),
        [
            ("id", "no id"),
            ("type", "no type"),
            ("timestamp", "no timestamp"),
            ("author", "no author"),
            ("author.id", "author has no id"),
            ("author.name", "author has no name"),
            ("attachments.url", "attachment has no url or fileName"),
            ("attachments.fileName", "attachment has no url or fileName"),
        ],
    )
    def test_a_message_missing_a_field_the_code_reads_is_skipped(self, instance, missing, reason):
        # A backfill converted from another source into the exporter's
        # shape is where a field goes missing — DiscordChatExporter itself
        # writes every one of them. The rest of the export still normalizes.
        variant = message(
            "m2",
            "https://example.test/other",
            nickname=None,
            timestamp="2026-08-19T10:05:00+00:00",
            attachments=[{"url": "assets/photo.png", "fileName": "photo.png"}],
        )
        field, _, leaf = missing.partition(".")
        if not leaf:
            del variant[field]
        elif field == "attachments":
            del variant[field][0][leaf]
        else:
            del variant[field][leaf]
        write_export(instance, [message("m1", "https://example.test/post"), variant])
        lines = run_normalize(instance, Config())
        assert f"warn: raw/discord/general: 1 message(s) unreadable ({reason}) — skipped" in lines
        assert "discord/general: 1 items written, 0 clusters skipped" in lines
        (item_id,) = items(instance)
        assert item_id.endswith(shortid("m1"))

    def test_an_author_name_the_corpus_will_not_take_is_skipped(self, instance):
        # A conversion that carries display names through verbatim can hand
        # over "Bob " — a name shared_by, a corpus scalar, refuses. Caught
        # in the read, so the channel's earlier clusters are not already on
        # disk under a line calling the whole channel skipped.
        write_export(
            instance,
            [
                message("m1", SUBSTANTIAL),
                message(
                    "m2",
                    "https://example.test/post",
                    author_id="u2",
                    name="bob",
                    nickname="Bob ",
                    timestamp="2026-08-19T12:00:00+00:00",
                ),
            ],
        )
        lines = run_normalize(instance, Config())
        reason = "author name is not a single trimmed line"
        assert f"warn: raw/discord/general: 1 message(s) unreadable ({reason}) — skipped" in lines
        assert "discord/general: 1 items written, 0 clusters skipped" in lines
        assert not any("export unreadable" in line for line in lines)
        (item_id,) = items(instance)
        assert item_id.endswith(shortid("m1"))

    def test_a_timestamp_that_does_not_parse_is_skipped(self, instance):
        # The reply joins its target's cluster, so the gap rule never parses
        # its timestamp — the emit pass does, one cluster after the first
        # has already been written.
        write_export(
            instance,
            [
                message("m1", SUBSTANTIAL),
                message("m2", "https://example.test/post", timestamp="2026-08-19T12:00:00+00:00"),
                message(
                    "m3",
                    "reply from someone else",
                    author_id="u2",
                    name="sam",
                    nickname=None,
                    msg_type="Reply",
                    reference="m2",
                    timestamp="not-a-timestamp",
                ),
            ],
        )
        lines = run_normalize(instance, Config())
        reason = "timestamp is not ISO 8601"
        assert f"warn: raw/discord/general: 1 message(s) unreadable ({reason}) — skipped" in lines
        assert "discord/general: 2 items written, 0 clusters skipped" in lines
        assert not any("export unreadable" in line for line in lines)
        assert sorted(item[-6:] for item in items(instance)) == sorted(
            [shortid("m1"), shortid("m2")]
        )

    def test_a_write_that_fails_is_named_with_its_count_not_called_skipped(
        self, instance, monkeypatch
    ):
        # A full disk faults the emit pass, not the read. The channel has
        # items on disk by then, so it cannot be reported as skipped — and
        # the run still finishes with its report for every other channel.
        write_export(
            instance,
            [
                message("m1", SUBSTANTIAL),
                message("m2", "https://example.test/post", timestamp="2026-08-19T12:00:00+00:00"),
            ],
        )
        write_export(instance, [message("n1", "https://example.test/other")], channel="zzz-later")
        done = []
        real_write_item = corpus.write_item

        def failing_write(path, item):
            if done:
                raise OSError(28, "No space left on device")
            done.append(path)
            real_write_item(path, item)

        monkeypatch.setattr(corpus, "write_item", failing_write)
        lines = run_normalize(instance, Config())
        assert (
            "discord/general: 1 items written, then write failed "
            "([Errno 28] No space left on device) — channel incomplete" in lines
        )
        assert not any("skipped" in line for line in lines if line.startswith("discord/general"))
        assert any(line.startswith("discord/zzz-later:") for line in lines)  # the run finished

    @pytest.mark.parametrize(
        ("field", "value", "reason"),
        [
            # Discord's own data package holds the attachment as a single
            # URL string, and a conversion that copies the field across
            # emits that verbatim — with the author beside it.
            ("attachments", "https://cdn.test/photo.png", "attachments is not a list of objects"),
            ("attachments", ["https://cdn.test/photo.png"], "attachments is not a list of objects"),
            ("embeds", "https://example.test/post", "embeds is not a list of objects"),
            ("reactions", "3", "reactions is not a list of objects"),
            ("author", "alex", "author is not an object"),
            ("reference", "m1", "reference is not an object"),
            ("content", ["a block", "another"], "content is not text"),
            # The scalars INSIDE well-shaped containers — the third member
            # of this family: containers checked, what the emit pass
            # indexes out of them not.
            ("id", ["m2"], "id is not text"),
            ("reference", {"messageId": ["m1"]}, "reference messageId is not text"),
            ("embeds", [{"url": ["https://x.test/a"], "title": "t"}], "embed url is not text"),
            (
                "embeds",
                [{"url": "https://example.test/other", "title": 12345}],
                "embed title is not text",
            ),
            ("reactions", [{"count": "3"}], "reaction count is not a number"),
        ],
    )
    def test_a_field_of_the_wrong_type_is_skipped_like_a_missing_one(
        self, instance, field, value, reason
    ):
        variant = message(
            "m2",
            "https://example.test/other",
            timestamp="2026-08-19T12:00:00+00:00",
        )
        variant[field] = value
        write_export(instance, [message("m1", "https://example.test/post"), variant])
        lines = run_normalize(instance, Config())
        assert f"warn: raw/discord/general: 1 message(s) unreadable ({reason}) — skipped" in lines
        assert "discord/general: 1 items written, 0 clusters skipped" in lines
        (item_id,) = items(instance)
        assert item_id.endswith(shortid("m1"))

    def test_a_message_that_is_not_an_object_is_skipped(self, instance):
        # A conversion that emits the channel's messages as bare strings.
        export: list = [message("m1", "https://example.test/post"), "2026-08-19 alex: hi"]
        write_export(instance, export)
        lines = run_normalize(instance, Config())
        reason = "message is not an object"
        assert f"warn: raw/discord/general: 1 message(s) unreadable ({reason}) — skipped" in lines
        assert "discord/general: 1 items written, 0 clusters skipped" in lines

    def test_an_empty_list_written_as_null_is_read_as_empty(self, instance):
        # A conversion that writes null for an empty collection is not
        # unreadable — it says the message has no attachments, no embeds
        # and no reactions, and the message still normalizes.
        variant = message("m1", "https://example.test/post")
        variant.update(attachments=None, embeds=None, reactions=None, reference=None)
        write_export(instance, [variant])
        lines = run_normalize(instance, Config())
        assert lines == ["discord/general: 1 items written, 0 clusters skipped"]
        item = next(iter(items(instance).values()))
        assert item.attachments == []
        assert item.reactions is None

    def test_a_wrong_typed_field_never_costs_the_run_its_report(self, instance):
        # The whole harm of a crash here: summaries accumulate and print at
        # the end, so one bad message used to take every channel's line
        # with it — including channels that had already written items.
        write_export(instance, [message("m1", "https://example.test/post")], channel="aaa-first")
        variant = message("m2", "https://example.test/other")
        variant["attachments"] = "https://cdn.test/photo.png"
        write_export(instance, [variant], channel="zzz-later")
        lines = run_normalize(instance, Config())
        assert "discord/aaa-first: 1 items written, 0 clusters skipped" in lines
        assert "discord/zzz-later: 0 items written, 0 clusters skipped" in lines

    def test_a_wrong_typed_nested_scalar_never_costs_the_run_its_report(self, instance):
        # The third member of the family: containers were checked, the
        # scalars the emit pass indexes out of them were not — `sum` over a
        # text count and `slugify` over a numeric title both died past the
        # (OSError, ValueError) catch, AFTER items were on disk, taking
        # every channel's summary with them.
        write_export(instance, [message("m1", "https://example.test/post")], channel="aaa-first")
        bad_count = message("m2", SUBSTANTIAL, reactions=[{"count": "3"}])
        bad_title = message(
            "m3",
            "https://example.test/other",
            timestamp="2026-08-19T12:00:00+00:00",
            embeds=[{"url": "https://example.test/other", "title": 12345}],
        )
        write_export(instance, [bad_count, bad_title], channel="zzz-later")
        lines = run_normalize(instance, Config())
        assert "discord/aaa-first: 1 items written, 0 clusters skipped" in lines
        assert "discord/zzz-later: 0 items written, 0 clusters skipped" in lines
        assert (
            "warn: raw/discord/zzz-later: 1 message(s) unreadable "
            "(reaction count is not a number) — skipped" in lines
        )
        assert (
            "warn: raw/discord/zzz-later: 1 message(s) unreadable "
            "(embed title is not text) — skipped" in lines
        )

    def test_an_unhashable_reply_target_is_skipped_in_the_read(self, instance):
        # The read pass looks a Reply's target up by messageId — a dict-key
        # lookup, so an unhashable value died inside the clustering, ahead
        # of every containment: not the emit catch, not the channel guard.
        reply = message(
            "m2",
            "reply text",
            msg_type="Reply",
            reference="m1",
            timestamp="2026-08-19T10:01:00+00:00",
        )
        reply["reference"]["messageId"] = ["m1"]
        write_export(instance, [message("m1", "https://example.test/post"), reply])
        lines = run_normalize(instance, Config())
        reason = "reference messageId is not text"
        assert f"warn: raw/discord/general: 1 message(s) unreadable ({reason}) — skipped" in lines
        assert "discord/general: 1 items written, 0 clusters skipped" in lines

    def test_an_escaped_shape_is_named_channel_incomplete_not_a_crash(self, instance, monkeypatch):
        # The belt over the braces: should a shape ever slip the read pass
        # again, the emit catch contains it as the write fault it lands as
        # — channel incomplete, with the count that landed, the run's
        # report intact — never a raw traceback over a part-written corpus.
        write_export(
            instance,
            [
                message("m1", SUBSTANTIAL),
                message("m2", "https://example.test/post", timestamp="2026-08-19T12:00:00+00:00"),
            ],
        )
        write_export(instance, [message("n1", "https://example.test/other")], channel="zzz-later")
        done = []
        real_write_item = corpus.write_item

        def escaping_write(path, item):
            if done:
                raise TypeError("unsupported operand type(s) for +: 'int' and 'str'")
            done.append(path)
            real_write_item(path, item)

        monkeypatch.setattr(corpus, "write_item", escaping_write)
        lines = run_normalize(instance, Config())
        assert (
            "discord/general: 1 items written, then write failed "
            "(unsupported operand type(s) for +: 'int' and 'str') — channel incomplete" in lines
        )
        assert any(line.startswith("discord/zzz-later:") for line in lines)  # the run finished

    def test_unreadable_messages_are_counted_once_per_reason(self, instance):
        # A conversion that drops a field drops it on every message: the
        # operator wants the count and the reason, not one line per message.
        stripped = []
        for n in range(3):
            variant = message(f"x{n}", SUBSTANTIAL, timestamp=f"2026-08-19T1{n}:00:00+00:00")
            del variant["timestamp"]
            stripped.append(variant)
        write_export(instance, [message("m1", "https://example.test/post"), *stripped])
        lines = run_normalize(instance, Config())
        expected = "warn: raw/discord/general: 3 message(s) unreadable (no timestamp) — skipped"
        assert expected in lines
        assert len(items(instance)) == 1


class TestRegeneration:
    def test_regeneration_is_byte_identical(self, instance):
        write_export(instance, [message("m1", "https://example.test/post\nnote")])
        run_normalize(instance, Config())
        (path,) = instance.corpus_dir.glob("*/*.md")
        first = path.read_bytes()
        run_normalize(instance, Config())
        assert path.read_bytes() == first

    def test_unchanged_items_are_not_rewritten_on_disk(self, instance):
        # Idempotent re-runs must not churn every corpus file through a
        # temp-write-and-replace: an untouched item keeps its very inode.
        write_export(instance, [message("m1", "https://example.test/post\nnote")])
        run_normalize(instance, Config())
        (path,) = instance.corpus_dir.glob("*/*.md")
        before = path.stat()
        run_normalize(instance, Config())
        after = path.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    def test_regeneration_preserves_enricher_owned_fields(self, instance):
        write_export(instance, [message("m1", "https://example.test/post\nnote")])
        run_normalize(instance, Config())
        (path,) = instance.corpus_dir.glob("*/*.md")
        enriched = dataclasses.replace(
            corpus.read_item(path), status="enriched", enrichment=["web-abc123.md"]
        )
        corpus.write_item(path, enriched)
        run_normalize(instance, Config())
        after = corpus.read_item(path)
        assert after.status == "enriched"
        assert after.enrichment == ["web-abc123.md"]
        assert "note" in after.body  # regenerated content intact

    def test_unparseable_existing_item_warns_and_regenerates(self, instance):
        write_export(instance, [message("m1", "https://example.test/post\nnote")])
        run_normalize(instance, Config())
        (path,) = instance.corpus_dir.glob("*/*.md")
        path.write_text("not a corpus item at all")
        lines = run_normalize(instance, Config())
        assert any("does not parse" in line for line in lines)
        assert corpus.read_item(path).status == "raw"


class TestCli:
    def test_parser_takes_no_flags(self):
        build_parser().parse_args([])
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--limit", "5"])

    def test_main_prints_summaries(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        write_export(instance, [message("m1", "https://example.test/post")])
        main([])
        assert "discord/general: 1 items written" in capsys.readouterr().out

    def test_main_without_exports_exits_loud(self, instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        with pytest.raises(SystemExit) as excinfo:
            main([])
        assert "no exports found" in str(excinfo.value)

    def test_malformed_config_is_loud(self, instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        instance.config_path.write_text('{"internal_domanes": []}')
        with pytest.raises(SystemExit) as excinfo:
            main([])
        assert "internal_domanes" in str(excinfo.value)
