"""The instance shim (instance/dex): pin-aware dispatch, POSIX sh.

Run as a subprocess against a stub uvx on PATH — the shim's whole job is
choosing the ref and exec'ing, so the stub just echoes the argv it got.
"""

import os
import subprocess
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parent.parent / "instance" / "dex"


@pytest.fixture
def fake_uvx(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    uvx = bin_dir / "uvx"
    uvx.write_text('#!/bin/sh\necho "$@"\n')
    uvx.chmod(0o755)
    return bin_dir


def run_shim(cwd: Path, fake_bin: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
    return subprocess.run(  # noqa: S603 — test-built args, no shell
        ["sh", str(SHIM), *args],  # noqa: S607 — sh resolves via PATH; the shim IS POSIX sh
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


class TestShim:
    def test_no_pin_tracks_main(self, tmp_path, fake_uvx):
        result = run_shim(tmp_path, fake_uvx, "enrich", "run")
        assert result.returncode == 0
        assert result.stdout.strip() == (
            "--from git+https://github.com/leeovery/dex@main dex-enrich run"
        )

    def test_pin_file_selects_the_tag(self, tmp_path, fake_uvx):
        (tmp_path / ".dex-engine-pin").write_text("v0.2.0\n")
        result = run_shim(tmp_path, fake_uvx, "sync")
        assert result.stdout.strip() == (
            "--from git+https://github.com/leeovery/dex@v0.2.0 dex-sync"
        )

    def test_pin_without_trailing_newline_still_reads(self, tmp_path, fake_uvx):
        (tmp_path / ".dex-engine-pin").write_text("v0.2.0")
        result = run_shim(tmp_path, fake_uvx, "lint")
        assert "@v0.2.0 dex-lint" in result.stdout

    def test_crlf_pin_yields_a_clean_ref(self, tmp_path, fake_uvx):
        # A hand-edited pin saved with CRLF must not smuggle \r into the ref.
        (tmp_path / ".dex-engine-pin").write_bytes(b"v0.2.0\r\n")
        result = run_shim(tmp_path, fake_uvx, "sync")
        assert "@v0.2.0 dex-sync" in result.stdout
        assert "\r" not in result.stdout

    def test_surrounding_whitespace_is_stripped(self, tmp_path, fake_uvx):
        (tmp_path / ".dex-engine-pin").write_text("  v0.2.0  \n")
        result = run_shim(tmp_path, fake_uvx, "lint")
        assert "@v0.2.0 dex-lint" in result.stdout

    def test_empty_pin_file_falls_back_to_main(self, tmp_path, fake_uvx):
        (tmp_path / ".dex-engine-pin").write_text("\n")
        result = run_shim(tmp_path, fake_uvx, "enrich")
        assert "@main dex-enrich" in result.stdout

    def test_no_command_prints_usage_and_fails(self, tmp_path, fake_uvx):
        result = run_shim(tmp_path, fake_uvx)
        assert result.returncode == 1
        assert "usage: bin/dex" in result.stdout
