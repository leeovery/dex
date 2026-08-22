"""Tests for new.py: instance scaffolding from the bundled template."""

import io
import json
import sys
from pathlib import Path

import pytest

from dex_engine.new import EPHEMERAL, SEEDS, TREE, build_parser, main, scaffold
from dex_engine.pipeline.types import Config
from dex_engine.render.cli import main as render_main

# The repo's template tree — what the wheel bundles as dex_engine/instance.
TEMPLATE = Path(__file__).resolve().parent.parent / "instance"


class RecordingRun:
    def __init__(self):
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append((list(args), cwd))


class TestScaffold:
    def test_builds_tree_seeds_and_machinery(self, tmp_path):
        root = tmp_path / "dex-cooking"
        run = RecordingRun()
        lines = scaffold(root, run=run, template=TEMPLATE)
        for directory in TREE:
            assert (root / directory / ".gitkeep").exists()
        for directory in EPHEMERAL:
            assert (root / directory).is_dir()
        for rel in SEEDS:
            assert (root / rel).exists()
        # Machinery arrives via the same template sync every instance runs.
        assert (root / "bin" / "dex").exists()
        assert (root / ".claude" / "dex-contract.md").exists()
        assert (root / ".gitattributes").exists()
        assert (root / "CLAUDE.md").exists()
        assert (root / "README.md").exists()
        assert run.calls == [
            (["git", "init", "-q"], root),
            (["git", "lfs", "install", "--local"], root),
        ]
        assert lines[0] == f"created {root}"
        assert any("bin/dex inbox ensure" in line for line in lines)

    def test_seeds_config_json_not_the_pre_rename_file(self, tmp_path):
        root = tmp_path / "dex-cooking"
        scaffold(root, run=RecordingRun(), template=TEMPLATE)
        assert not (root / "state" / "normalize-config.json").exists()
        config = root / "state" / "config.json"
        assert json.loads(config.read_text()) == {"internal_domains": []}
        Config.load(config)  # the seed parses under the loud loader

    def test_cache_is_gitignored_from_birth(self, tmp_path):
        root = tmp_path / "dex-cooking"
        scaffold(root, run=RecordingRun(), template=TEMPLATE)
        assert "cache/" in (root / ".gitignore").read_text()

    def test_cache_exists_but_is_never_tracked(self, tmp_path):
        root = tmp_path / "dex-cooking"
        scaffold(root, run=RecordingRun(), template=TEMPLATE)
        assert (root / "cache").is_dir()
        # No .gitkeep: the dir is gitignored, so a keeper file would be a
        # tracked file inside an ignored directory.
        assert not (root / "cache" / ".gitkeep").exists()

    def test_a_fresh_instance_can_render_the_documented_receipt(self, tmp_path, monkeypatch):
        # The per-item procedure's last step writes cache/receipt.json and
        # runs `bin/dex render --file cache/receipt.json`.
        root = tmp_path / "dex-cooking"
        scaffold(root, run=RecordingRun(), template=TEMPLATE)
        payload = {
            "surface": "ingest-receipt",
            "payload": {"item": "2026-08-18-note-a1b2c3", "signal": "high"},
        }
        (root / "cache" / "receipt.json").write_text(json.dumps(payload))
        monkeypatch.chdir(root)
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        render_main(["--file", "cache/receipt.json"])
        assert "ingested 2026-08-18-note-a1b2c3" in out.getvalue()

    def test_nonempty_target_is_loud(self, tmp_path):
        root = tmp_path / "dex-cooking"
        root.mkdir()
        (root / "keep.txt").write_text("x")
        with pytest.raises(ValueError, match="not empty"):
            scaffold(root, run=RecordingRun(), template=TEMPLATE)


class TestCli:
    def test_name_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_main_reports_failures_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "dex-cooking"
        target.mkdir()
        (target / "keep.txt").write_text("x")
        with pytest.raises(SystemExit) as excinfo:
            main(["dex-cooking"])
        assert "not empty" in str(excinfo.value)
