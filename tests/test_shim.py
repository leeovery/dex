"""The instance shim (instance/dex): pin-aware dispatch, POSIX sh.

Run as a subprocess against a stub uvx on PATH — the shim's whole job is
choosing the ref and exec'ing, so the stub just echoes the argv it got.

Every case runs under each POSIX shell the machine has, because "POSIX sh"
is not one interpreter: macOS's /bin/sh is bash in POSIX mode and accepts
bashisms the shim must not rely on, while a Linux instance runs dash. A
shim bug that only bites under dash once shipped that way, so the suite
names the shells instead of trusting whatever /bin/sh happens to be.

The shim is copied to ``<instance>/bin/dex`` and run from a DIFFERENT
directory: the pin is read beside the shim, never from the working
directory, and a suite that ran it from the instance root would never
notice the difference.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parent.parent / "instance" / "dex"

REPO = "git+https://github.com/leeovery/dex"
COMMIT = "8c40a95e0392c47b2e1c74ff1884c8d667c44e80"

# `sh` is whatever the platform ships — bash on macOS, dash on Debian/Ubuntu —
# so bash, dash and ksh are also named outright: a machine that has them
# really exercises them, whatever its /bin/sh happens to be.
SHELLS = ("sh", "bash", "dash", "ksh")

# CI names the shells it guarantees here (space-separated) so a runner image
# that stops shipping one FAILS instead of quietly shrinking the matrix.
_REQUIRED = frozenset(os.environ.get("DEX_SHIM_SHELLS", "").split())

# The stub uvx: it echoes the argv it was given, which is the whole of what
# the shim decides. A run that reaches it at all is a run that launched.
_FAKE_UVX = '#!/bin/sh\necho "$@"\n'


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
def instance(tmp_path: Path) -> Path:
    """An instance root holding a copy of the shim at bin/dex."""
    root = tmp_path / "instance"
    (root / "bin").mkdir(parents=True)
    shutil.copy(SHIM, root / "bin" / "dex")
    return root


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    """The working directory every run uses: not the instance root."""
    cwd = tmp_path / "some-project"
    cwd.mkdir()
    return cwd


@pytest.fixture
def fake_uvx(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    uvx = bin_dir / "uvx"
    uvx.write_text(_FAKE_UVX)
    uvx.chmod(0o755)
    return bin_dir


def run_shim(
    shell: str, instance: Path, cwd: Path, fake_bin: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
    return subprocess.run(  # noqa: S603 — test-built args, no shell
        [shell, str(instance / "bin" / "dex"), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def pin(instance: Path, line: str | bytes) -> None:
    path = instance / ".dex-engine-pin"
    if isinstance(line, bytes):
        path.write_bytes(line)
    else:
        path.write_text(line)


class TestRef:
    def test_no_pin_tracks_main(self, shell, instance, elsewhere, fake_uvx):
        result = run_shim(shell, instance, elsewhere, fake_uvx, "enrich", "run")
        assert result.returncode == 0
        assert result.stdout.strip() == f"--from {REPO}@main dex-enrich run"

    def test_tag_only_pin_selects_the_tag(self, shell, instance, elsewhere, fake_uvx):
        pin(instance, "v0.2.0\n")
        result = run_shim(shell, instance, elsewhere, fake_uvx, "sync")
        assert result.stdout.strip() == f"--from {REPO}@v0.2.0 dex-sync"

    def test_commit_is_what_the_launch_uses(self, shell, instance, elsewhere, fake_uvx):
        pin(instance, f"v0.2.0 {COMMIT}\n")
        result = run_shim(shell, instance, elsewhere, fake_uvx, "lint", "--write")
        assert result.stdout.strip() == f"--from {REPO}@{COMMIT} dex-lint --write"


class TestWhereThePinIsRead:
    def test_pin_is_read_beside_the_shim_not_in_the_working_directory(
        self, shell, instance, elsewhere, fake_uvx
    ):
        pin(instance, "v0.2.0\n")
        (elsewhere / ".dex-engine-pin").write_text("v9.9.9\n")
        result = run_shim(shell, instance, elsewhere, fake_uvx, "lint")
        assert "@v0.2.0 dex-lint" in result.stdout

    def test_a_pin_in_the_working_directory_alone_is_ignored(
        self, shell, instance, elsewhere, fake_uvx
    ):
        (elsewhere / ".dex-engine-pin").write_text("v9.9.9\n")
        result = run_shim(shell, instance, elsewhere, fake_uvx, "lint")
        assert "@main dex-lint" in result.stdout

    def test_run_from_the_instance_root_still_works(self, shell, instance, fake_uvx):
        pin(instance, "v0.2.0\n")
        result = run_shim(shell, instance, instance, fake_uvx, "sync")
        assert "@v0.2.0 dex-sync" in result.stdout


class TestPinLine:
    @pytest.mark.parametrize(
        "field",
        [
            COMMIT[:39],  # too short
            COMMIT + "0",  # too long
            "8c40a95e0392c47b2e1c74ff1884c8d667c44eGG",  # not hex
            "main",
        ],
    )
    def test_a_second_field_that_is_not_a_commit_leaves_the_tag_in_charge(
        self, shell, instance, elsewhere, fake_uvx, field
    ):
        pin(instance, f"v0.2.0 {field}\n")
        result = run_shim(shell, instance, elsewhere, fake_uvx, "lint")
        assert result.stdout.strip() == f"--from {REPO}@v0.2.0 dex-lint"

    def test_fields_after_the_commit_are_ignored(self, shell, instance, elsewhere, fake_uvx):
        pin(instance, f"v0.2.0 {COMMIT} whatever else\n")
        result = run_shim(shell, instance, elsewhere, fake_uvx, "lint")
        assert f"@{COMMIT} dex-lint" in result.stdout

    def test_pin_without_trailing_newline_still_reads(self, shell, instance, elsewhere, fake_uvx):
        pin(instance, f"v0.2.0 {COMMIT}")
        result = run_shim(shell, instance, elsewhere, fake_uvx, "lint")
        assert f"@{COMMIT} dex-lint" in result.stdout

    def test_crlf_pin_yields_a_clean_ref(self, shell, instance, elsewhere, fake_uvx):
        # A hand-edited pin saved with CRLF must not smuggle \r into the ref.
        pin(instance, f"v0.2.0 {COMMIT}\r\n".encode())
        result = run_shim(shell, instance, elsewhere, fake_uvx, "sync")
        assert f"@{COMMIT} dex-sync" in result.stdout
        assert "\r" not in result.stdout

    def test_crlf_tag_only_pin_yields_a_clean_ref(self, shell, instance, elsewhere, fake_uvx):
        pin(instance, b"v0.2.0\r\n")
        result = run_shim(shell, instance, elsewhere, fake_uvx, "sync")
        assert result.stdout.strip() == f"--from {REPO}@v0.2.0 dex-sync"

    def test_surrounding_whitespace_is_stripped(self, shell, instance, elsewhere, fake_uvx):
        pin(instance, "  v0.2.0  \n")
        result = run_shim(shell, instance, elsewhere, fake_uvx, "lint")
        assert "@v0.2.0 dex-lint" in result.stdout

    def test_empty_pin_file_falls_back_to_main(self, shell, instance, elsewhere, fake_uvx):
        pin(instance, "\n")
        result = run_shim(shell, instance, elsewhere, fake_uvx, "enrich")
        assert "@main dex-enrich" in result.stdout


class TestUsage:
    def test_no_command_prints_usage_and_fails(self, shell, instance, elsewhere, fake_uvx):
        result = run_shim(shell, instance, elsewhere, fake_uvx)
        assert result.returncode == 1
        assert "usage: bin/dex" in result.stdout
