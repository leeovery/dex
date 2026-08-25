"""The instance shim (instance/dex): pin-aware dispatch, POSIX sh.

Run as a subprocess against a stub uvx on PATH — the shim's whole job is
choosing the ref and exec'ing, so the stub just echoes the argv it got.

Every case runs under each POSIX shell the machine has, because "POSIX sh"
is not one interpreter: macOS's /bin/sh is bash in POSIX mode and accepts
bashisms the shim must not rely on, while a Linux instance runs dash. A
shim bug that only bites under dash once shipped that way, so the suite
names the shells instead of trusting whatever /bin/sh happens to be.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parent.parent / "instance" / "dex"

# `sh` is whatever the platform ships — bash on macOS, dash on Debian/Ubuntu —
# so bash, dash and ksh are also named outright: a machine that has them
# really exercises them, whatever its /bin/sh happens to be.
SHELLS = ("sh", "bash", "dash", "ksh")

# CI names the shells it guarantees here (space-separated) so a runner image
# that stops shipping one FAILS instead of quietly shrinking the matrix.
_REQUIRED = frozenset(os.environ.get("DEX_SHIM_SHELLS", "").split())


@pytest.fixture(params=SHELLS)
def shell(request: pytest.FixtureRequest) -> str:
    name = request.param
    found = shutil.which(name)
    if found is None:
        if name in _REQUIRED:
            pytest.fail(f"DEX_SHIM_SHELLS requires {name!r}, but it is not on PATH")
        pytest.skip(f"{name} is not on PATH")
    return found


@pytest.fixture
def fake_uvx(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    uvx = bin_dir / "uvx"
    uvx.write_text('#!/bin/sh\necho "$@"\n')
    uvx.chmod(0o755)
    return bin_dir


def run_shim(shell: str, cwd: Path, fake_bin: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
    return subprocess.run(  # noqa: S603 — test-built args, no shell
        [shell, str(SHIM), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


class TestShim:
    def test_no_pin_tracks_main(self, shell, tmp_path, fake_uvx):
        result = run_shim(shell, tmp_path, fake_uvx, "enrich", "run")
        assert result.returncode == 0
        assert result.stdout.strip() == (
            "--from git+https://github.com/leeovery/dex@main dex-enrich run"
        )

    def test_pin_file_selects_the_tag(self, shell, tmp_path, fake_uvx):
        (tmp_path / ".dex-engine-pin").write_text("v0.2.0\n")
        result = run_shim(shell, tmp_path, fake_uvx, "sync")
        assert result.stdout.strip() == (
            "--from git+https://github.com/leeovery/dex@v0.2.0 dex-sync"
        )

    def test_pin_without_trailing_newline_still_reads(self, shell, tmp_path, fake_uvx):
        (tmp_path / ".dex-engine-pin").write_text("v0.2.0")
        result = run_shim(shell, tmp_path, fake_uvx, "lint")
        assert "@v0.2.0 dex-lint" in result.stdout

    def test_crlf_pin_yields_a_clean_ref(self, shell, tmp_path, fake_uvx):
        # A hand-edited pin saved with CRLF must not smuggle \r into the ref.
        (tmp_path / ".dex-engine-pin").write_bytes(b"v0.2.0\r\n")
        result = run_shim(shell, tmp_path, fake_uvx, "sync")
        assert "@v0.2.0 dex-sync" in result.stdout
        assert "\r" not in result.stdout

    def test_surrounding_whitespace_is_stripped(self, shell, tmp_path, fake_uvx):
        (tmp_path / ".dex-engine-pin").write_text("  v0.2.0  \n")
        result = run_shim(shell, tmp_path, fake_uvx, "lint")
        assert "@v0.2.0 dex-lint" in result.stdout

    def test_empty_pin_file_falls_back_to_main(self, shell, tmp_path, fake_uvx):
        (tmp_path / ".dex-engine-pin").write_text("\n")
        result = run_shim(shell, tmp_path, fake_uvx, "enrich")
        assert "@main dex-enrich" in result.stdout

    def test_no_command_prints_usage_and_fails(self, shell, tmp_path, fake_uvx):
        result = run_shim(shell, tmp_path, fake_uvx)
        assert result.returncode == 1
        assert "usage: bin/dex" in result.stdout
