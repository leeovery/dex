"""Tests for gitread.py: read-only git queries, and None when git cannot answer."""

import shutil
import subprocess

import pytest

from dex_engine.gitread import checkout_bytes, git_output


class TestGitOutput:
    def test_prints_what_git_printed(self, tmp_path, own_git):
        own_git(tmp_path, "init", "-q")
        assert git_output(tmp_path, ["rev-parse", "--is-inside-work-tree"]) == "true\n"

    def test_runs_in_the_repository_at_root(self, tmp_path, own_git):
        repo = tmp_path / "repo"
        repo.mkdir()
        own_git(repo, "init", "-q")
        top = git_output(repo, ["rev-parse", "--show-toplevel"])
        assert top is not None
        assert top.strip() == str(repo.resolve())

    def test_a_query_git_refuses_is_none(self, tmp_path, own_git):
        own_git(tmp_path, "init", "-q")
        assert git_output(tmp_path, ["show", "HEAD:CLAUDE.md"]) is None

    def test_with_no_git_at_all_it_is_none(self, tmp_path, monkeypatch):
        nowhere = tmp_path / "no-git"
        nowhere.mkdir()
        monkeypatch.setenv("PATH", str(nowhere))
        assert git_output(tmp_path, ["--version"]) is None

    def test_output_that_is_not_utf_8_reads_rather_than_raising(self, tmp_path, own_git):
        own_git(tmp_path, "init", "-q")
        (tmp_path / "latin1.md").write_bytes(b"caf\xe9\n")
        own_git(tmp_path, "add", "latin1.md")
        own_git(tmp_path, "commit", "-q", "-m", "latin-1")
        assert git_output(tmp_path, ["show", "HEAD:latin1.md"]) == "caf�\n"


class TestCheckoutBytes:
    def test_a_committed_file_reads_back_byte_for_byte(self, tmp_path, own_git):
        own_git(tmp_path, "init", "-q")
        (tmp_path / "clip.mp4").write_bytes(b"\x00\xff\xfe binary")
        own_git(tmp_path, "add", "clip.mp4")
        own_git(tmp_path, "commit", "-q", "-m", "clip")
        (tmp_path / "clip.mp4").unlink()
        assert checkout_bytes(tmp_path, "HEAD", "clip.mp4") == b"\x00\xff\xfe binary"

    def test_a_path_the_revision_does_not_hold_is_none(self, tmp_path, own_git):
        own_git(tmp_path, "init", "-q")
        own_git(tmp_path, "commit", "-q", "--allow-empty", "-m", "empty")
        assert checkout_bytes(tmp_path, "HEAD", "clip.mp4") is None

    def test_a_checkout_waits_longer_than_a_query(self, tmp_path, own_git, monkeypatch):
        # A checkout may download a whole video from the LFS remote first.
        own_git(tmp_path, "init", "-q")
        own_git(tmp_path, "commit", "-q", "--allow-empty", "-m", "empty")
        waited: list[float] = []
        real = subprocess.run

        def timed(*args, **kwargs):
            waited.append(kwargs["timeout"])
            return real(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", timed)
        git_output(tmp_path, ["rev-parse", "HEAD"])
        checkout_bytes(tmp_path, "HEAD", "clip.mp4")
        query, checkout = waited
        assert checkout > query

    def test_an_lfs_file_reads_as_its_content_whatever_the_callers_smudge_setting(
        self, tmp_path, own_git, monkeypatch
    ):
        if shutil.which("git-lfs") is None:
            pytest.skip("git-lfs is not installed")
        own_git(tmp_path, "init", "-q")
        own_git(tmp_path, "lfs", "install", "--local")
        own_git(tmp_path, "lfs", "track", "*.mp4")
        (tmp_path / "clip.mp4").write_bytes(b"\x00\xff the video itself")
        own_git(tmp_path, "add", ".gitattributes", "clip.mp4")
        own_git(tmp_path, "commit", "-q", "-m", "clip")
        (tmp_path / "clip.mp4").unlink()
        monkeypatch.setenv("GIT_LFS_SKIP_SMUDGE", "1")
        assert checkout_bytes(tmp_path, "HEAD", "clip.mp4") == b"\x00\xff the video itself"
