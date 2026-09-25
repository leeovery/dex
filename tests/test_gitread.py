"""Tests for gitread.py: read-only git queries, and None when git cannot answer."""

from dex_engine.gitread import git_output


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
