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
