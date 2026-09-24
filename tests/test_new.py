"""Tests for new.py: instance scaffolding from the bundled template."""

import datetime
import io
import json
import sys
from pathlib import Path

import pytest

from dex_engine.directives import log_path as directives_log
from dex_engine.directives import pending
from dex_engine.lens import lens_finding
from dex_engine.new import EPHEMERAL, NAMED_SEEDS, SEEDS, TREE, build_parser, main, scaffold
from dex_engine.pipeline.types import Config, Instance
from dex_engine.render.cli import main as render_main
from tests.directives.conftest import make_directive

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
        for rel in NAMED_SEEDS:
            assert (root / rel).exists()
        assert run.calls == [
            (["git", "init", "-q"], root),
            (["git", "lfs", "install", "--local"], root),
        ]
        assert lines[0] == f"created {root}"
        assert any("bin/dex inbox ensure" in line for line in lines)

    def test_next_steps_respect_the_github_choice(self, tmp_path):
        # GitHub is a setup-time choice the owner may decline; the printed
        # next steps offer the gh command conditionally, never as an
        # unconditional instruction.
        root = tmp_path / "dex-cooking"
        lines = scaffold(root, run=RecordingRun(), template=TEMPLATE)
        assert lines == [
            f"created {root}",
            (
                "next: fill in lens.md (what this dex reads for) and README.md's <owner>/<repo>, "
                "commit, then:"
            ),
            "  if using GitHub: gh repo create dex-cooking --private --source . --push",
            "  bin/dex inbox ensure",
        ]

    def test_the_lens_is_named_for_the_instance_and_still_to_be_filled_in(self, tmp_path):
        root = tmp_path / "dex-cooking"
        scaffold(root, run=RecordingRun(), template=TEMPLATE)
        title, *rest = (root / "lens.md").read_text().splitlines()
        _seed_title, *seed_rest = (TEMPLATE / "lens.md").read_text().splitlines()
        assert title == "# dex-cooking"
        assert rest == seed_rest
        # The placeholder lines stay for setup to fill, so lint and sync
        # say so until it has.
        finding = lens_finding(Instance(root=root), TEMPLATE)
        assert finding is not None
        assert finding.startswith("`lens.md` still holds the seed's placeholder text")

    def test_the_readme_is_named_and_links_the_lens(self, tmp_path):
        root = tmp_path / "dex-cooking"
        scaffold(root, run=RecordingRun(), template=TEMPLATE)
        readme = (root / "README.md").read_text()
        assert readme.splitlines()[0] == "# dex-cooking"
        assert "<instance name>" not in readme
        assert "[`lens.md`](./lens.md)" in readme
        # The repo is only known once setup settles GitHub.
        assert "an existing dex at <owner>/<repo>." in readme

    def test_claude_md_is_the_template_s_own(self, tmp_path):
        root = tmp_path / "dex-cooking"
        scaffold(root, run=RecordingRun(), template=TEMPLATE)
        assert (root / "CLAUDE.md").read_text() == (TEMPLATE / "CLAUDE.md").read_text()

    def test_claude_md_arrives_through_sync_alone(self, tmp_path, monkeypatch):
        synced: list[Path] = []
        monkeypatch.setattr("dex_engine.new.sync", lambda root, **_kwargs: synced.append(root))
        root = tmp_path / "dex-cooking"
        scaffold(root, run=RecordingRun(), template=TEMPLATE)
        assert synced == [root]
        assert not (root / "CLAUDE.md").exists()

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
        assert "## Ingested 2026-08-18-note-a1b2c3" in out.getvalue()

    def test_every_shipped_directive_is_recorded_done_at_birth(self, tmp_path):
        root = tmp_path / "dex-cooking"
        shipped = [make_directive(1), make_directive(2)]
        scaffold(
            root,
            run=RecordingRun(),
            template=TEMPLATE,
            shipped=shipped,
            today=lambda: datetime.date(2026, 9, 24),
            version=lambda: "0.2.0",
        )
        records = [json.loads(line) for line in directives_log(root).read_text().splitlines()]
        assert records == [
            {"number": 1, "engine": "0.2.0", "date": "2026-09-24"},
            {"number": 2, "engine": "0.2.0", "date": "2026-09-24"},
        ]
        assert pending(root, shipped) == []

    def test_an_engine_shipping_no_directives_writes_no_log(self, tmp_path):
        root = tmp_path / "dex-cooking"
        scaffold(root, run=RecordingRun(), template=TEMPLATE, shipped=[])
        assert not directives_log(root).exists()

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
