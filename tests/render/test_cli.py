"""Tests for render/cli.py: the cognitive call path's verbatim emitter."""

import json
from pathlib import Path

import pytest

from dex_engine.render.cli import build_parser, main, render_file


def write_payload(tmp_path, raw) -> str:
    path = tmp_path / "payload.json"
    path.write_text(raw if isinstance(raw, str) else json.dumps(raw))
    return str(path)


class TestParser:
    def test_file_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_typoed_flags_are_loud(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--fle", "x.json"])


class TestRenderFile:
    def test_renders_the_named_surface(self, tmp_path):
        path = write_payload(
            tmp_path,
            {"surface": "ingest-receipt", "payload": {"item": "2026-08-18-note-a1b2c3"}},
        )
        assert render_file(Path(path)) == "## Ingested 2026-08-18-note-a1b2c3 — no units fetched\n"

    def test_bad_json_is_loud(self, tmp_path):
        with pytest.raises(ValueError, match="invalid JSON"):
            render_file(Path(write_payload(tmp_path, "not json")))

    def test_missing_surface_is_loud(self, tmp_path):
        with pytest.raises(ValueError, match="surface"):
            render_file(Path(write_payload(tmp_path, {"payload": {}})))

    def test_unknown_key_is_loud(self, tmp_path):
        with pytest.raises(ValueError, match="surfce"):
            render_file(Path(write_payload(tmp_path, {"surfce": "status", "payload": {}})))


class TestMain:
    def test_prints_verbatim(self, tmp_path, capsys):
        path = write_payload(
            tmp_path,
            {"surface": "ingest-receipt", "payload": {"item": "2026-08-18-note-a1b2c3"}},
        )
        main(["--file", path])
        assert capsys.readouterr().out == "## Ingested 2026-08-18-note-a1b2c3 — no units fetched\n"

    def test_nonconforming_payload_exits_cleanly(self, tmp_path):
        path = write_payload(tmp_path, {"surface": "ingest-receipt", "payload": {}})
        with pytest.raises(SystemExit) as excinfo:
            main(["--file", path])
        assert "item" in str(excinfo.value)

    def test_missing_file_exits_cleanly(self, tmp_path):
        with pytest.raises(SystemExit) as excinfo:
            main(["--file", str(tmp_path / "absent.json")])
        assert "dex-render" in str(excinfo.value)
