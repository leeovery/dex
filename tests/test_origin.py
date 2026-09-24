"""Tests for origin.py: the origin remote, read as the GitHub repository it names."""

import pytest

from dex_engine.gitread import git_output
from dex_engine.origin import github_repo, origin_url


class TestGithubRepo:
    @pytest.mark.parametrize(
        "url",
        [
            "git@github.com:someone/dex-cooking.git",
            "git@github.com:someone/dex-cooking",
            "https://github.com/someone/dex-cooking.git",
            "https://github.com/someone/dex-cooking",
            "ssh://git@github.com/someone/dex-cooking.git",
            "https://github.com/someone/dex-cooking.git\n",
        ],
        ids=["ssh", "ssh-bare", "https", "https-bare", "ssh-url", "as-git-prints-it"],
    )
    def test_every_github_spelling_names_the_repo(self, url):
        assert github_repo(url) == "someone/dex-cooking"

    def test_a_dot_inside_the_repo_name_is_kept(self):
        assert github_repo("git@github.com:someone/dex.cooking.git") == "someone/dex.cooking"

    @pytest.mark.parametrize(
        "url",
        [
            "git@gitlab.com:someone/dex-cooking.git",
            "https://gitlab.com/someone/dex-cooking",
            "https://github.company.test/someone/dex-cooking",
            "/srv/git/dex-cooking.git",
            "",
        ],
        ids=["gitlab-ssh", "gitlab-https", "github-lookalike-host", "local-path", "empty"],
    )
    def test_a_remote_elsewhere_names_no_repo(self, url):
        assert github_repo(url) is None


class TestOriginUrl:
    def test_reads_the_origin_remote(self, tmp_path, own_git):
        own_git(tmp_path, "init", "-q")
        own_git(tmp_path, "remote", "add", "origin", "git@github.com:someone/dex-cooking.git")
        assert origin_url(tmp_path) == "git@github.com:someone/dex-cooking.git"

    @pytest.mark.parametrize(
        "base", ["git@github.com:", "https://github.com/"], ids=["ssh", "https"]
    )
    def test_an_insteadof_rewrite_never_hides_the_stored_url(self, tmp_path, own_git, base):
        # git expands `url.<to>.insteadOf <from>` when it reads a remote for
        # use; a machine that fetches GitHub through a mirror or a local
        # path still names the GitHub repository in its origin.
        stored = f"{base}someone/dex-cooking.git"
        mirror = (tmp_path / "mirror").as_uri() + "/"
        own_git(tmp_path, "init", "-q")
        own_git(tmp_path, "remote", "add", "origin", stored)
        own_git(tmp_path, "config", f"url.{mirror}.insteadOf", base)
        rewritten = git_output(tmp_path, ["remote", "get-url", "origin"])
        assert rewritten == f"{mirror}someone/dex-cooking.git\n"
        assert origin_url(tmp_path) == stored

    def test_only_origin_counts(self, tmp_path, own_git):
        own_git(tmp_path, "init", "-q")
        own_git(tmp_path, "remote", "add", "upstream", "git@github.com:someone/other.git")
        assert origin_url(tmp_path) is None

    def test_a_repository_with_no_remote_has_no_origin(self, tmp_path, own_git):
        own_git(tmp_path, "init", "-q")
        assert origin_url(tmp_path) is None

    @pytest.mark.usefixtures("own_git")
    def test_outside_a_repository_there_is_no_origin(self, tmp_path):
        assert origin_url(tmp_path) is None

    def test_with_no_git_at_all_there_is_no_origin(self, tmp_path, monkeypatch):
        nowhere = tmp_path / "no-git"
        nowhere.mkdir()
        monkeypatch.setenv("PATH", str(nowhere))
        assert origin_url(tmp_path) is None
