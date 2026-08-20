"""Tests for normalize.py: export parsing, clustering, shared detection (§14)."""

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
    slugify,
)
from dex_engine.pipeline.types import Config, Instance

CHANNEL = "general"


def message(  # noqa: PLR0913 — a fixture builder mirrors the export's fields
    msg_id: str,
    content: str,
    *,
    author_id: str = "u1",
    name: str = "lee",
    nickname: str | None = "Lee",
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
    return {
        path.stem: corpus.read_item(path) for path in instance.corpus_dir.glob("*/*.md")
    }


SUBSTANTIAL = "an observation with enough substance to be knowledge, " * 6


class TestHelpers:
    def test_slugify_bounds_and_floors(self):
        assert slugify("A Great Article: Part 2!") == "a-great-article-part-2"
        assert slugify("!!!") == "untitled"
        assert len(slugify("word " * 30)) <= 40

    def test_kind_of_uses_the_shared_registry(self):
        assert kind_of("https://www.youtube.com/watch?v=abc") == "youtube"
        assert kind_of("https://m.youtube.com/watch?v=abc") == "youtube"  # the healed split
        assert kind_of("https://gist.github.com/a/b") == "github"  # the healed gist gap
        assert kind_of("https://x.com/a/status/1") == "x"  # post-rename vocabulary
        assert kind_of("https://example.test/post") == "web"

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
        assert item.shared_by == "Lee"  # nickname over name
        assert item.urls == ["https://example.test/post"]
        assert item.kinds == ["web"]
        assert item.status == "raw"
        assert item.id.endswith(shortid("m1"))
        assert "**Lee** (2026-08-19 10:00):" in item.body
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
        lines = run_normalize(
            instance, Config(internal_domains=["corp.test"])
        )
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


class TestRegeneration:
    def test_regeneration_is_byte_identical(self, instance):
        write_export(instance, [message("m1", "https://example.test/post\nnote")])
        run_normalize(instance, Config())
        (path,) = instance.corpus_dir.glob("*/*.md")
        first = path.read_bytes()
        run_normalize(instance, Config())
        assert path.read_bytes() == first

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
