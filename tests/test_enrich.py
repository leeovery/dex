"""Tests for enrich.py: the thin CLI — parse, build, call, print."""

import json
import os

import pytest

from dex_engine.capabilities.transcribe.whisper_api import WhisperApi
from dex_engine.enrich import _load_env, build_parser, main
from dex_engine.pipeline.types import Instance


class TestParser:
    def test_run_accepts_limit(self):
        args = build_parser().parse_args(["run", "--limit", "5"])
        assert (args.command, args.limit) == ("run", 5)

    def test_status_accepts_item(self):
        args = build_parser().parse_args(["status", "--item", "2026-08-19-x-55ad7b"])
        assert (args.command, args.item) == ("status", "2026-08-19-x-55ad7b")
        assert build_parser().parse_args(["status"]).item is None

    def test_fetch_takes_item_urls_parent_force(self):
        args = build_parser().parse_args(
            ["fetch", "2026-08-19-x-55ad7b", "https://a.test", "https://b.test", "--force"]
        )
        assert args.item == "2026-08-19-x-55ad7b"
        assert args.urls == ["https://a.test", "https://b.test"]
        assert args.parent is None
        assert args.force is True

    def test_mark_takes_status_reason_path(self):
        args = build_parser().parse_args(
            ["mark", "https://a.test", "manual", "--reason", "thin-extraction"]
        )
        assert (args.status, args.reason, args.path) == ("manual", "thin-extraction", None)

    def test_typoed_flags_are_loud_not_ignored(self):
        # The old hand-rolled parser silently ignored typo'd flags.
        with pytest.raises(SystemExit):
            build_parser().parse_args(["run", "--limt", "5"])

    def test_unknown_status_is_loud(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["mark", "https://a.test", "finished"])

    def test_transcribe_takes_model_and_limit(self):
        args = build_parser().parse_args(["transcribe"])
        assert (args.command, args.model, args.limit) == ("transcribe", None, 10)
        args = build_parser().parse_args(["transcribe", "--model", "small", "--limit", "3"])
        assert (args.model, args.limit) == ("small", 3)

    def test_a_command_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_item_new_takes_capture_slug_shared_by(self):
        args = build_parser().parse_args(
            ["item", "new", "inbox/20260818-101530.md", "--slug", "great-post",
             "--shared-by", "Alex"]
        )
        assert (args.command, args.item_command) == ("item", "new")
        assert str(args.capture) == "inbox/20260818-101530.md"
        assert (args.slug, args.shared_by) == ("great-post", "Alex")

    def test_item_requires_a_subcommand(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["item"])


class TestMain:
    def test_run_on_an_empty_instance_prints_the_report(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        main(["run"])
        out = capsys.readouterr().out
        assert out.startswith("## Enrich run — 0 units processed")
        assert "nothing parked" in out

    def test_status_prints_the_surface_and_capability_report(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        main(["status"])
        out = capsys.readouterr().out
        assert out.startswith("## Ledger — 0 entries")
        assert "## Capabilities" in out  # how a free-floor instance learns what a key buys
        assert "transcribe" in out

    def test_status_item_renders_the_item_view(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        main(["status", "--item", "2026-08-19-x-55ad7b"])
        out = capsys.readouterr().out
        assert out.startswith("## Item 2026-08-19-x-55ad7b — no work units")

    def test_transcribe_on_an_empty_instance_reports_cleanly(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        main(["transcribe"])
        out = capsys.readouterr().out
        assert out.startswith("## Enrich run — 0 units processed")

    def test_pass_records_a_stage(self, instance: Instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        main(["pass", "2026-08-19-x-55ad7b", "--stage", "harvest"])
        assert "recorded harvest pass" in capsys.readouterr().out
        record = json.loads(instance.passes_path.read_text().split("\n")[0])
        assert record["stage"] == "harvest"
        assert record["rules"] == 1

    def test_item_new_creates_the_item(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        capture = instance.root / "inbox" / "20260818-101530.md"
        capture.parent.mkdir()
        capture.write_text("https://example.test/post\n\nwhy I saved it\n")
        main(["item", "new", str(capture), "--shared-by", "Alex"])
        out = capsys.readouterr().out
        assert out.startswith("created corpus/2026/")
        assert len(list(instance.corpus_dir.glob("*/*.md"))) == 1

    def test_compact_prints_the_result(self, instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        main(["compact"])
        assert "compacted: 0 superseded lines removed" in capsys.readouterr().out

    def test_pipeline_errors_exit_cleanly(self, instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        with pytest.raises(SystemExit) as excinfo:
            main(["mark", "https://never.seen.test/x", "dead"])
        assert "no ledger entry" in str(excinfo.value)

    def test_malformed_config_is_loud(self, instance: Instance, monkeypatch):
        monkeypatch.chdir(instance.root)
        instance.config_path.write_text('{"media_fech": "lead"}')
        with pytest.raises(SystemExit) as excinfo:
            main(["status"])
        assert "media_fech" in str(excinfo.value)


class TestLoadEnv:
    """The instance ``.env`` is the local-secret slot; every verb loads it."""

    @pytest.fixture(autouse=True)
    def _clean_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        yield
        os.environ.pop("OPENAI_API_KEY", None)

    def test_dotenv_key_reaches_the_provider(self, instance: Instance):
        (instance.root / ".env").write_text("# secrets\nOPENAI_API_KEY=sk-from-dotenv\n")
        _load_env(instance.root)
        assert os.environ["OPENAI_API_KEY"] == "sk-from-dotenv"
        # The provider resolves its key from the environment it now holds.
        api = WhisperApi(which=lambda _name: "/usr/bin/ffmpeg")
        assert api.available().ok

    def test_every_verb_loads_it(self, instance: Instance, monkeypatch, capsys):
        monkeypatch.chdir(instance.root)
        (instance.root / ".env").write_text("OPENAI_API_KEY=sk-from-dotenv\n")
        main(["status"])
        capsys.readouterr()
        assert os.environ["OPENAI_API_KEY"] == "sk-from-dotenv"

    def test_a_real_environment_variable_wins(self, instance: Instance, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
        (instance.root / ".env").write_text("OPENAI_API_KEY=sk-from-dotenv\n")
        _load_env(instance.root)
        assert os.environ["OPENAI_API_KEY"] == "sk-real"

    def test_comments_and_malformed_lines_are_ignored(self, instance: Instance):
        (instance.root / ".env").write_text(
            "# OPENAI_API_KEY=commented-out\n"
            "no equals sign here\n"
            "=nameless\n"
            "OPENAI_API_KEY = sk-spaced \n"
        )
        _load_env(instance.root)
        assert os.environ["OPENAI_API_KEY"] == "sk-spaced"

    def test_missing_dotenv_is_a_noop(self, instance: Instance):
        _load_env(instance.root)
        assert "OPENAI_API_KEY" not in os.environ

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_surrounding_quotes_are_stripped(self, instance: Instance, quote: str):
        # A shell-shaped .env is the wild shape; a literal quote in the key
        # 401s every call with a baffling waiting reason.
        (instance.root / ".env").write_text(f"OPENAI_API_KEY={quote}sk-quoted{quote}\n")
        _load_env(instance.root)
        assert os.environ["OPENAI_API_KEY"] == "sk-quoted"
        api = WhisperApi(which=lambda _name: "/usr/bin/ffmpeg")
        assert api.available().ok

    def test_an_unreadable_dotenv_exits_with_a_stated_line(
        self, instance: Instance, monkeypatch, capsys
    ):
        monkeypatch.chdir(instance.root)
        (instance.root / ".env").write_bytes(b"OPENAI_API_KEY=\xff\xfe\n")
        with pytest.raises(SystemExit) as excinfo:
            main(["status"])
        assert str(excinfo.value).startswith("dex-enrich:")
        capsys.readouterr()

    def test_a_dotenv_directory_exits_with_a_stated_line(
        self, instance: Instance, monkeypatch, capsys
    ):
        monkeypatch.chdir(instance.root)
        (instance.root / ".env").mkdir()
        with pytest.raises(SystemExit) as excinfo:
            main(["status"])
        assert str(excinfo.value).startswith("dex-enrich:")
        capsys.readouterr()
