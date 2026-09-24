"""Tests for atomic.py: the one same-dir-temp-then-replace implementation."""

import os

import pytest

from dex_engine import atomic


def failing_fdopen(monkeypatch) -> None:
    """Make the helper's write step fail mid-flight (disk full)."""
    real = os.fdopen

    def failing(fd, *args, **kwargs):
        real(fd, *args, **kwargs).close()
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("dex_engine.atomic.os.fdopen", failing)


class TestWrite:
    def test_write_text_creates_the_file(self, tmp_path):
        path = tmp_path / "state.jsonl"
        atomic.write_text(path, "line — unicode\n")
        assert path.read_text(encoding="utf-8") == "line — unicode\n"
        assert [p.name for p in tmp_path.iterdir()] == ["state.jsonl"]

    def test_write_bytes_replaces_the_previous_content(self, tmp_path):
        path = tmp_path / "blob.bin"
        atomic.write_bytes(path, b"first")
        atomic.write_bytes(path, b"second")
        assert path.read_bytes() == b"second"
        assert [p.name for p in tmp_path.iterdir()] == ["blob.bin"]

    def test_failed_write_keeps_the_original_and_leaves_no_temp(self, tmp_path, monkeypatch):
        path = tmp_path / "state.jsonl"
        path.write_text("intact\n")
        failing_fdopen(monkeypatch)
        with pytest.raises(OSError, match="No space left"):
            atomic.write_text(path, "replacement\n")
        assert path.read_text() == "intact\n"
        assert [p.name for p in tmp_path.iterdir()] == ["state.jsonl"]

    def test_failed_first_write_leaves_nothing_at_all(self, tmp_path, monkeypatch):
        failing_fdopen(monkeypatch)
        with pytest.raises(OSError, match="No space left"):
            atomic.write_bytes(tmp_path / "fresh.bin", b"data")
        assert list(tmp_path.iterdir()) == []


class TestDropLines:
    def test_drops_the_named_lines_and_carries_the_rest_byte_for_byte(self, tmp_path):
        path = tmp_path / "passes.jsonl"
        path.write_text('{"item": "a"}\n{"item": "b"}\n{"item":"c" }\n', encoding="utf-8")
        assert atomic.drop_lines(path, lambda line: '"b"' in line) == 1
        assert path.read_text(encoding="utf-8") == '{"item": "a"}\n{"item":"c" }\n'

    def test_counts_every_line_it_drops(self, tmp_path):
        path = tmp_path / "passes.jsonl"
        path.write_text("drop\nkeep\ndrop\ndrop\n", encoding="utf-8")
        assert atomic.drop_lines(path, lambda line: line == "drop") == 3
        assert path.read_text(encoding="utf-8") == "keep\n"

    def test_blank_lines_go_with_a_rewrite_and_are_never_offered(self, tmp_path):
        path = tmp_path / "passes.jsonl"
        path.write_text("keep\n\n   \ndrop\nkeep too", encoding="utf-8")
        seen: list[str] = []

        def drop(line: str) -> bool:
            seen.append(line)
            return line == "drop"

        assert atomic.drop_lines(path, drop) == 1
        assert seen == ["keep", "drop", "keep too"]
        assert path.read_text(encoding="utf-8") == "keep\nkeep too\n"

    def test_a_file_with_nothing_to_drop_is_not_rewritten(self, tmp_path):
        # The union-merged history stays exactly as git last saw it.
        path = tmp_path / "passes.jsonl"
        path.write_text("keep\n\nkeep\n", encoding="utf-8")
        before = path.stat().st_ino
        assert atomic.drop_lines(path, lambda _line: False) == 0
        assert path.read_text(encoding="utf-8") == "keep\n\nkeep\n"
        assert path.stat().st_ino == before

    def test_the_rewrite_is_the_atomic_replace(self, tmp_path, monkeypatch):
        path = tmp_path / "passes.jsonl"
        path.write_text("drop\nkeep\n", encoding="utf-8")
        failing_fdopen(monkeypatch)
        with pytest.raises(OSError, match="No space left"):
            atomic.drop_lines(path, lambda line: line == "drop")
        assert path.read_text(encoding="utf-8") == "drop\nkeep\n"
        assert [p.name for p in tmp_path.iterdir()] == ["passes.jsonl"]
