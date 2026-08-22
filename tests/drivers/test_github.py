"""Tests for drivers/github.py: URL-shape routing and classified gh failures."""

import base64
import json
from collections.abc import Sequence

import pytest

from dex_engine.drivers.github import GhResult, GitHubDriver
from dex_engine.pipeline.types import Kind, Status
from tests.drivers.conftest import body_of, fixture_text, make_unit, reason_of


class FakeGh:
    """args tuple -> GhResult; unexpected invocations are loud."""

    def __init__(self, responses: dict[tuple[str, ...], GhResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> GhResult:
        key = tuple(args)
        self.calls.append(key)
        if key not in self.responses:
            raise AssertionError(f"unexpected gh invocation {key!r}")
        return self.responses[key]


def ok(stdout: str) -> GhResult:
    return GhResult(returncode=0, stdout=stdout, stderr="")


def fail(stderr: str) -> GhResult:
    return GhResult(returncode=1, stdout="", stderr=stderr)


def driver_for(gh_responses: dict | None = None) -> GitHubDriver:
    return GitHubDriver(gh=FakeGh(gh_responses or {}))


def contents(data: bytes) -> GhResult:
    """The contents-API payload shape: base64 `content` plus its encoding."""
    return ok(
        json.dumps({"encoding": "base64", "content": base64.b64encode(data).decode("ascii")})
    )


README_ARGS = (
    "api",
    "repos/acme/pipeline-kit/readme",
    "-H",
    "Accept: application/vnd.github.raw+json",
)


class TestIdentity:
    def test_kind_and_sleep(self):
        driver = driver_for()
        assert driver.kind is Kind.GITHUB
        assert driver.sleep == 0.3

    def test_matches_github_and_gist_hosts(self):
        driver = driver_for()
        assert driver.matches("https://github.com/acme/pipeline-kit")
        assert driver.matches("https://gist.github.com/octomaint/abc123def456")
        assert not driver.matches("https://gitlab.com/acme/thing")


class TestRepo:
    def test_repo_meta_and_readme_body(self):
        driver = driver_for(
            {
                ("api", "repos/acme/pipeline-kit"): ok(fixture_text("github", "repo.json")),
                README_ARGS: ok(fixture_text("github", "readme.md")),
            }
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert result.status is Status.DONE
        assert result.meta["title"] == "acme/pipeline-kit"
        assert result.meta["stars"] == 2481
        assert result.meta["archived"] is None  # not archived -> omitted from frontmatter
        assert str(result.meta["topics"]).count(",") == 7  # capped at 8 topics
        assert "# pipeline-kit" in body_of(result)

    def test_missing_readme_yields_placeholder_body(self):
        driver = driver_for(
            {
                ("api", "repos/acme/pipeline-kit"): ok(fixture_text("github", "repo.json")),
                README_ARGS: fail("gh: Not Found (HTTP 404)"),
            }
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert result.status is Status.DONE
        assert body_of(result) == "(no README)"

    def test_deleted_repo_is_dead(self):
        driver = driver_for({("api", "repos/acme/gone"): fail("gh: Not Found (HTTP 404)")})
        result = driver.fetch(make_unit("https://github.com/acme/gone", Kind.GITHUB))
        assert result.status is Status.DEAD

    def test_rate_limited_api_is_blocked(self):
        driver = driver_for(
            {("api", "repos/acme/pipeline-kit"): fail("gh: API rate limit exceeded (HTTP 403)")}
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert result.status is Status.BLOCKED

    def test_gh_timeout_shape_classifies_blocked(self):
        # run_gh converts TimeoutExpired to this GhResult shape — a hung gh
        # is the world misbehaving, never an engine error.
        driver = driver_for({("api", "repos/acme/pipeline-kit"): fail("gh timed out after 120s")})
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert result.status is Status.BLOCKED
        assert "timed out" in reason_of(result)

    def test_gh_failure_without_a_code_is_blocked_with_scrubbed_reason(self):
        driver = driver_for(
            {
                ("api", "repos/acme/pipeline-kit"): fail(
                    "error connecting to api.github.com from /Users/owner/base"
                )
            }
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert result.status is Status.BLOCKED
        assert "/Users/owner" not in reason_of(result)


class TestProfile:
    def test_profile_with_top_repos_by_stars(self):
        driver = driver_for(
            {
                ("api", "users/octomaint"): ok(fixture_text("github", "user.json")),
                ("api", "users/octomaint/repos?sort=pushed&per_page=100"): ok(
                    fixture_text("github", "user-repos.json")
                ),
            }
        )
        result = driver.fetch(make_unit("https://github.com/octomaint", Kind.GITHUB))
        assert result.status is Status.DONE
        assert result.meta["title"] == "Octo Maintainer"
        assert result.meta["followers"] == 512
        body = body_of(result)
        assert body.index("pipeline-kit") < body.index("vtt-clean") < body.index("dotfiles")

    def test_repo_listing_failure_does_not_fail_the_profile(self):
        driver = driver_for(
            {
                ("api", "users/octomaint"): ok(fixture_text("github", "user.json")),
                ("api", "users/octomaint/repos?sort=pushed&per_page=100"): fail(
                    "gh: API rate limit exceeded (HTTP 403)"
                ),
            }
        )
        result = driver.fetch(make_unit("https://github.com/octomaint", Kind.GITHUB))
        assert result.status is Status.DONE
        assert "(repo listing unavailable)" in body_of(result)


class TestGist:
    def test_gist_files_render_fenced(self):
        driver = driver_for(
            {("api", "gists/abc123def456"): ok(fixture_text("github", "gist.json"))}
        )
        url = "https://gist.github.com/octomaint/abc123def456"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert result.status is Status.DONE
        assert result.meta["title"] == "ledger compaction one-liner"
        body = body_of(result)
        assert "### compact.py" in body
        assert "### notes.md" in body
        assert "Last-per-hash wins" in body

    def test_gist_index_is_skipped_with_reason(self):
        result = driver_for().fetch(make_unit("https://gist.github.com/octomaint", Kind.GITHUB))
        assert result.status is Status.SKIPPED
        assert "gist index" in reason_of(result)

    @pytest.mark.parametrize(
        "gist_id",
        [
            "0f1e2d3c4b5a69788796a5b4c3d2e1f0",  # today's 32-char hex
            "9f0e8d7c6b5a49382716",  # 2013-era 20-char hex
            "4277",  # pre-2013 sequential decimal
        ],
    )
    def test_bare_gist_id_links_fetch_by_id(self, gist_id):
        # Legacy gist.github.com/<id> share links (no username segment)
        # still resolve — the API call only ever needed the id.
        driver = driver_for({("api", f"gists/{gist_id}"): ok(fixture_text("github", "gist.json"))})
        result = driver.fetch(make_unit(f"https://gist.github.com/{gist_id}", Kind.GITHUB))
        assert result.status is Status.DONE
        assert "### compact.py" in body_of(result)


class TestIssue:
    def test_issue_title_and_body(self):
        driver = driver_for(
            {("api", "repos/acme/pipeline-kit/issues/42"): ok(fixture_text("github", "issue.json"))}
        )
        url = "https://github.com/acme/pipeline-kit/issues/42"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert result.status is Status.DONE
        assert result.meta["title"] == "enricher ledgers 403 challenges as dead"
        assert "Cloudflare 403" in body_of(result)

    def test_pull_urls_route_through_the_issues_api(self):
        gh = FakeGh(
            {("api", "repos/acme/pipeline-kit/issues/7"): ok(fixture_text("github", "issue.json"))}
        )
        driver = GitHubDriver(gh=gh)
        url = "https://github.com/acme/pipeline-kit/pull/7"
        assert driver.fetch(make_unit(url, Kind.GITHUB)).status is Status.DONE


class TestBlob:
    URL = "https://github.com/acme/pipeline-kit/blob/main/src/detect.py"
    CONTENTS = ("api", "repos/acme/pipeline-kit/contents/src/detect.py?ref=main")

    def test_blob_fetches_through_the_authenticated_gh_seam_and_fences(self):
        # raw.githubusercontent.com is unauthenticated: it 404s for every
        # private-repo blob however the machine is signed in, and that 404
        # classified live content as dead. gh carries the auth.
        driver = driver_for({self.CONTENTS: contents(b"def detect(): ...")})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert result.status is Status.DONE
        assert result.meta["file"] == "src/detect.py"
        assert body_of(result) == "```\ndef detect(): ...\n```"

    @pytest.mark.parametrize("spelling", ["a b.md", "a%20b.md"])
    def test_blob_path_reaches_the_api_encoded_exactly_once(self, spelling):
        args = ("api", "repos/acme/pipeline-kit/contents/docs/a%20b.md?ref=main")
        gh = FakeGh({args: contents(b"hello")})
        url = f"https://github.com/acme/pipeline-kit/blob/main/docs/{spelling}"
        assert GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)).status is Status.DONE

    def test_missing_path_is_dead_through_the_gh_404(self):
        driver = driver_for({self.CONTENTS: fail("gh: Not Found (HTTP 404)")})
        assert driver.fetch(make_unit(self.URL, Kind.GITHUB)).status is Status.DEAD

    def test_oversize_blob_parks_manual_never_dead(self):
        # Over 1MB the contents API answers `encoding: "none"` with an empty
        # body — the file is there, just not inline.
        driver = driver_for(
            {self.CONTENTS: ok(json.dumps({"encoding": "none", "content": "", "size": 4645520}))}
        )
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert result.status is Status.MANUAL
        assert "larger than the contents API serves inline" in reason_of(result)


class TestEdges:
    def test_github_root_is_skipped_with_reason(self):
        result = driver_for().fetch(make_unit("https://github.com", Kind.GITHUB))
        assert result.status is Status.SKIPPED
        assert "root" in reason_of(result)

    def test_unparseable_api_json_is_blocked(self):
        driver = driver_for({("api", "repos/acme/pipeline-kit"): ok("<html>oops</html>")})
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert result.status is Status.BLOCKED
        assert "unparseable JSON" in reason_of(result)
