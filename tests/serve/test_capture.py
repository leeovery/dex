"""Tests for the capture tool: the inbox file, the commit, and honesty when it fails.

The commits are real git in real repositories — what a commit touched, and
what a refusing hook does to one, is git's answer to give and not worth
faking.
"""

import datetime
import re
import subprocess

import pytest

from dex_engine.pipeline.capture import parse_capture
from dex_engine.serve import library

from .conftest import BOOKS, COFFEE, git, record, refusal

STAMPED = re.compile(r"inbox/\d{8}-\d{6}\.md")
NOW = datetime.datetime(2026, 8, 20, 10, 15, 30, tzinfo=datetime.UTC)


def hook(root, script: str) -> None:
    """Install a pre-commit hook that refuses, with the output it refuses with."""
    path = root / ".git" / "hooks" / "pre-commit"
    path.write_text(f"#!/bin/sh\n{script}\nexit 1\n")
    path.chmod(0o755)


def captured(root, result) -> str:
    """The capture file's text on disk, from a tool result."""
    return (root / result["path"]).read_text(encoding="utf-8")


class TestTheFile:
    def test_lands_in_the_named_instance_inbox(self, server, repos):
        result = record(
            server,
            "capture",
            {"instance": BOOKS, "url": "https://example.test/post", "note": "worth reading"},
        )
        assert STAMPED.fullmatch(result["path"])
        assert result["instance"] == BOOKS
        assert captured(repos[1], result) == "https://example.test/post\n\nworth reading\n"
        assert list((repos[0] / "inbox").glob("*.md")) == []

    def test_is_the_format_the_processor_reads(self, server, repos):
        result = record(
            server,
            "capture",
            {"instance": COFFEE, "url": "https://example.test/post", "note": "worth reading"},
        )
        frontmatter, body = parse_capture(captured(repos[0], result))
        # A text capture carries no frontmatter at all; the body is the
        # whole provenance record, and becomes the item body verbatim.
        assert frontmatter == {}
        assert body == "https://example.test/post\n\nworth reading\n"

    def test_a_note_needs_no_url(self, server, repos):
        result = record(server, "capture", {"instance": COFFEE, "note": "a standing thought"})
        assert captured(repos[0], result) == "a standing thought\n"

    def test_a_url_needs_no_note(self, server, repos):
        result = record(server, "capture", {"instance": COFFEE, "url": "https://example.test/p"})
        assert captured(repos[0], result) == "https://example.test/p\n"

    def test_neither_a_url_nor_a_note_is_refused(self, server, repos):
        assert "a URL, a note, or both" in refusal(server, "capture", {"instance": COFFEE})
        assert list((repos[0] / "inbox").glob("*.md")) == []

    def test_an_unknown_instance_names_the_served_ones(self, server):
        message = refusal(server, "capture", {"instance": "dex-nope", "note": "x"})
        assert "no instance named 'dex-nope'" in message
        assert f"{COFFEE}, {BOOKS}" in message

    def test_an_inbox_that_cannot_be_written_is_a_refusal(self, server, repos):
        (repos[0] / "inbox").write_text("not a directory")
        assert "cannot write the capture" in refusal(
            server, "capture", {"instance": COFFEE, "note": "a standing thought"}
        )

    def test_two_captures_in_one_second_each_keep_their_own_note(self, roster, repos):
        # Through the library, where the clock is injectable: the server
        # reads the real one, and a burst is exactly the collision case.
        first = library.capture(roster, instance=COFFEE, url="", note="first", now=NOW)
        second = library.capture(roster, instance=COFFEE, url="", note="second", now=NOW)
        assert (first.path, second.path) == (
            "inbox/20260820-101530.md",
            "inbox/20260820-101531.md",
        )
        assert (repos[0] / second.path).read_text(encoding="utf-8") == "second\n"
        assert (first.committed, second.committed) == (True, True)


class TestTheCommit:
    def test_commits_the_capture_where_it_landed(self, server, repos):
        result = record(server, "capture", {"instance": COFFEE, "note": "a standing thought"})
        assert result["committed"] is True
        assert result["detail"] == ""
        assert git(repos[0], "log", "-1", "--format=%s").strip() == f"capture: {result['path']}"
        assert git(repos[0], "show", f"HEAD:{result['path']}") == "a standing thought\n"
        assert git(repos[0], "status", "--porcelain").strip() == ""

    def test_carries_nothing_the_owner_had_staged(self, server, repos):
        (repos[0] / "wiki" / "index.md").write_text("# Index\n\n- [[espresso]]\n")
        git(repos[0], "add", "--", "wiki/index.md")
        result = record(server, "capture", {"instance": COFFEE, "note": "a standing thought"})
        assert git(repos[0], "show", "--name-only", "--format=", "HEAD").split() == [result["path"]]
        assert git(repos[0], "diff", "--cached", "--name-only").split() == ["wiki/index.md"]

    def test_an_instance_that_is_no_repo_still_keeps_the_note(self, server, roots):
        result = record(server, "capture", {"instance": COFFEE, "note": "a standing thought"})
        assert result["committed"] is False
        assert "written, but not committed" in result["detail"]
        assert "not a git repository" in result["detail"]
        assert captured(roots[0], result) == "a standing thought\n"

    def test_a_refusing_hook_is_quoted_and_the_note_kept(self, server, repos):
        hook(repos[0], 'echo "the hook said no" >&2')
        result = record(server, "capture", {"instance": COFFEE, "note": "a standing thought"})
        assert result["committed"] is False
        assert "the hook said no" in result["detail"]
        assert captured(repos[0], result) == "a standing thought\n"
        assert git(repos[0], "log", "-1", "--format=%s").strip() == "the instance so far"

    def test_a_hook_that_refuses_silently_still_says_git_refused(self, server, repos):
        hook(repos[0], ":")
        result = record(server, "capture", {"instance": COFFEE, "note": "a standing thought"})
        assert result["detail"] == "written, but not committed: git commit exited 1"

    def test_a_hook_speaking_bytes_that_are_not_text_does_not_crash(self, server, repos):
        hook(repos[0], r'printf "bad \377 byte" >&2')
        result = record(server, "capture", {"instance": COFFEE, "note": "a standing thought"})
        assert result["committed"] is False
        assert "bad" in result["detail"]

    @pytest.mark.parametrize(
        ("boom", "said"),
        [
            (OSError("no git on PATH"), "no git on PATH"),
            (
                subprocess.TimeoutExpired(["git"], 60),
                "Command '['git']' timed out after 60 seconds",
            ),
        ],
    )
    def test_git_that_will_not_run_at_all_still_keeps_the_note(
        self, monkeypatch, roster, repos, boom, said
    ):
        def explode(*_args, **_kwargs):
            raise boom

        monkeypatch.setattr(subprocess, "run", explode)
        result = library.capture(roster, instance=COFFEE, url="", note="a thought", now=NOW)
        assert result.committed is False
        assert result.detail == f"written, but not committed: {said}"
        assert (repos[0] / result.path).read_text(encoding="utf-8") == "a thought\n"


class TestTheInstanceBoundary:
    def test_a_capture_reaches_only_the_instance_it_names(self, server, repos):
        record(server, "capture", {"instance": BOOKS, "note": "a books thought"})
        assert git(repos[0], "log", "--format=%s").strip() == "the instance so far"
        assert git(repos[1], "log", "-1", "--format=%s").startswith("capture: inbox/")

    def test_the_path_is_relative_to_the_instance_it_named(self, server, repos):
        result = record(server, "capture", {"instance": BOOKS, "note": "a books thought"})
        assert not result["path"].startswith("/")
        assert (repos[1] / result["path"]).is_file()
