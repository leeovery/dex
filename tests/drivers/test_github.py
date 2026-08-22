"""Tests for drivers/github.py: URL-shape routing, and what blob bytes mean.

The blob round trip itself — the ref/path boundary, the contents endpoint,
the oversize park — belongs to the shared seam and is pinned in test_gh.py.
What is here is the driver's own judgment: fence it, re-detect it, or park.
"""

import json

import pytest

from dex_engine.drivers.github import GitHubDriver
from dex_engine.pipeline.detect import detect_kind
from dex_engine.pipeline.registry import DRIVERS
from dex_engine.pipeline.types import Format, Kind, Status
from tests.drivers.conftest import (
    FakeGh,
    body_of,
    fixture_text,
    gh_contents,
    gh_fail,
    gh_ok,
    make_unit,
    reason_of,
)


def driver_for(gh_responses: dict | None = None) -> GitHubDriver:
    return GitHubDriver(gh=FakeGh(gh_responses or {}))


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


RESERVED_URLS = [
    "https://github.com/features/copilot",
    "https://github.com/topics/rust",
    "https://github.com/sponsors/octomaint",
    "https://github.com/orgs/acme/repositories",
    "https://github.com/collections/design-essentials",
    "https://github.com/marketplace/actions/checkout",
    "https://github.com/trending",
    "https://github.com/trending/python?since=weekly",
    "https://github.com/about",
    "https://github.com/pricing",
    "https://github.com/settings/profile",
    "https://github.com/explore",
    "https://github.com/security",
    "https://github.com/readme/featured",
]


class TestReservedNamespaces:
    """Reserved first segments are neither user nor repo — the web driver's."""

    @pytest.mark.parametrize("url", RESERVED_URLS)
    def test_github_declines_reserved_first_segments(self, url):
        # The API 404s these while a browser renders them: driving them as
        # profile or repo work ledgered live pages dead.
        assert not driver_for().matches(url)

    @pytest.mark.parametrize("url", RESERVED_URLS)
    def test_the_web_driver_claims_them_and_no_one_else_does(self, url):
        assert detect_kind(url, DRIVERS) is Kind.WEB

    def test_a_repo_named_after_a_reserved_word_is_still_repo_work(self):
        # Only the FIRST segment is reserved: acme/topics is an ordinary repo.
        assert driver_for().matches("https://github.com/acme/topics")
        assert driver_for().matches("https://github.com/acme/pipeline-kit/issues/42")

    def test_gist_urls_are_never_screened(self):
        assert driver_for().matches("https://gist.github.com/topics/abc123def456")


class TestRepo:
    def test_repo_meta_and_readme_body(self):
        driver = driver_for(
            {
                ("api", "repos/acme/pipeline-kit"): gh_ok(fixture_text("github", "repo.json")),
                README_ARGS: gh_ok(fixture_text("github", "readme.md")),
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
                ("api", "repos/acme/pipeline-kit"): gh_ok(fixture_text("github", "repo.json")),
                README_ARGS: gh_fail("gh: Not Found (HTTP 404)"),
            }
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert result.status is Status.DONE
        assert body_of(result) == "(no README)"

    def test_deleted_repo_is_dead(self):
        driver = driver_for({("api", "repos/acme/gone"): gh_fail("gh: Not Found (HTTP 404)")})
        result = driver.fetch(make_unit("https://github.com/acme/gone", Kind.GITHUB))
        assert result.status is Status.DEAD

    def test_rate_limited_api_is_blocked(self):
        driver = driver_for(
            {("api", "repos/acme/pipeline-kit"): gh_fail("gh: API rate limit exceeded (HTTP 403)")}
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert result.status is Status.BLOCKED

    def test_gh_timeout_shape_classifies_blocked(self):
        # run_gh converts TimeoutExpired to this GhResult shape — a hung gh
        # is the world misbehaving, never an engine error.
        timeout = gh_fail("gh timed out after 120s")
        driver = driver_for({("api", "repos/acme/pipeline-kit"): timeout})
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert result.status is Status.BLOCKED
        assert "timed out" in reason_of(result)

    def test_gh_failure_without_a_code_is_blocked_with_scrubbed_reason(self):
        driver = driver_for(
            {
                ("api", "repos/acme/pipeline-kit"): gh_fail(
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
                ("api", "users/octomaint"): gh_ok(fixture_text("github", "user.json")),
                ("api", "users/octomaint/repos?sort=pushed&per_page=100"): gh_ok(
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
                ("api", "users/octomaint"): gh_ok(fixture_text("github", "user.json")),
                ("api", "users/octomaint/repos?sort=pushed&per_page=100"): gh_fail(
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
            {("api", "gists/abc123def456"): gh_ok(fixture_text("github", "gist.json"))}
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
        gist = gh_ok(fixture_text("github", "gist.json"))
        driver = driver_for({("api", f"gists/{gist_id}"): gist})
        result = driver.fetch(make_unit(f"https://gist.github.com/{gist_id}", Kind.GITHUB))
        assert result.status is Status.DONE
        assert "### compact.py" in body_of(result)


class TestIssue:
    def test_issue_title_and_body(self):
        issue = gh_ok(fixture_text("github", "issue.json"))
        driver = driver_for({("api", "repos/acme/pipeline-kit/issues/42"): issue})
        url = "https://github.com/acme/pipeline-kit/issues/42"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert result.status is Status.DONE
        assert result.meta["title"] == "enricher ledgers 403 challenges as dead"
        assert "Cloudflare 403" in body_of(result)

    def test_pull_urls_route_through_the_issues_api(self):
        issue = gh_ok(fixture_text("github", "issue.json"))
        gh = FakeGh({("api", "repos/acme/pipeline-kit/issues/7"): issue})
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
        driver = driver_for({self.CONTENTS: gh_contents(b"def detect(): ...")})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert result.status is Status.DONE
        assert result.meta["file"] == "src/detect.py"
        assert body_of(result) == "```\ndef detect(): ...\n```"

    def test_missing_path_is_dead_through_the_gh_404(self):
        # The 404 sends the driver to look for a longer ref first; `main` is
        # the whole ref, so nothing re-splits and the 404 stands.
        driver = driver_for(
            {
                self.CONTENTS: gh_fail("gh: Not Found (HTTP 404)"),
                ("api", "repos/acme/pipeline-kit/git/matching-refs/heads/main"): gh_ok(
                    json.dumps([{"ref": "refs/heads/main"}])
                ),
                ("api", "repos/acme/pipeline-kit/git/matching-refs/tags/main"): gh_ok("[]"),
            }
        )
        assert driver.fetch(make_unit(self.URL, Kind.GITHUB)).status is Status.DEAD

    def test_a_document_blob_redetects_to_file_work(self):
        # A real ledger PDF blob fenced 40k characters of
        # replacement-character soup and went done. It is not source — but
        # it IS extractable, now that the file driver reads blob bytes
        # through the same authenticated API.
        driver = driver_for({self.CONTENTS: gh_contents(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj")})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert result.status is Status.QUEUED
        assert result.redetect is not None
        assert result.redetect.kind is Kind.FILE
        assert result.redetect.format is Format.PDF

    def test_an_unrecognized_binary_blob_parks_manual_never_fenced(self):
        driver = driver_for({self.CONTENTS: gh_contents(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert result.status is Status.MANUAL
        assert "binary, not UTF-8 text" in reason_of(result)
        assert result.body is None

    def test_utf8_source_with_non_ascii_still_fences(self):
        driver = driver_for({self.CONTENTS: gh_contents("# naïve — résumé\n".encode())})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert result.status is Status.DONE
        assert "naïve — résumé" in body_of(result)

    def test_an_lfs_pointer_is_never_fenced_as_the_document_it_stands_for(self):
        # An unsmudged LFS pointer is honest UTF-8 with no signature, so an
        # unnamed sniff let 130 bytes of `oid sha256:…` fence and ledger
        # `done` as though it were the document it points at. Named, it is
        # pdf work like any other committed pdf — and the file driver, which
        # fetches these same bytes back, is where a pointer is finally
        # parked, before an extractor is handed the stand-in text.
        pointer = (
            b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:08709a87567d8311d6fd29c4f4a5386801153e71450e628c4a5a5d7e85feda8b\n"
            b"size 7416886\n"
        )
        args = ("api", "repos/acme/pipeline-kit/contents/docs/sicp.pdf?ref=main")
        driver = driver_for({args: gh_contents(pointer)})
        url = "https://github.com/acme/pipeline-kit/blob/main/docs/sicp.pdf"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert result.body is None
        assert result.redetect is not None
        assert result.redetect.format is Format.PDF

    def test_a_committed_csv_goes_to_the_extractor_rather_than_fencing(self):
        # A CSV has no signature either, so only the name catches it — and
        # letting text-shaped documents fence is exactly what let an LFS
        # pointer for a .csv through. It re-detects like every other
        # document rather than parking: an extractor reads the committed
        # bytes into a real table, where a fence would truncate at 40k
        # characters and read as a wall of commas.
        args = ("api", "repos/acme/pipeline-kit/contents/data/runs.csv?ref=main")
        driver = driver_for({args: gh_contents(b"run,status\n1,done\n2,dead\n")})
        url = "https://github.com/acme/pipeline-kit/blob/main/data/runs.csv"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert result.status is Status.QUEUED
        assert result.redetect is not None
        assert result.redetect.kind is Kind.FILE
        assert result.redetect.format is Format.CSV

    @pytest.mark.parametrize("path", ["src/detect.py", "README.md", "Makefile", "docs/notes.txt"])
    def test_source_and_prose_extensions_still_fence(self, path):
        # The extension fallback only knows Format values: nothing a repo
        # actually holds as source or prose is diverted by naming the file.
        args = ("api", f"repos/acme/pipeline-kit/contents/{path}?ref=main")
        driver = driver_for({args: gh_contents(b"def detect(): ...")})
        url = f"https://github.com/acme/pipeline-kit/blob/main/{path}"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert result.status is Status.DONE
        assert body_of(result) == "```\ndef detect(): ...\n```"

    def test_oversize_blob_parks_manual_never_dead(self):
        # Over 1MB the contents API answers `encoding: "none"` with an empty
        # body — the file is there, just not inline.
        driver = driver_for(
            {self.CONTENTS: gh_ok(json.dumps({"encoding": "none", "content": "", "size": 4645520}))}
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
        driver = driver_for({("api", "repos/acme/pipeline-kit"): gh_ok("<html>oops</html>")})
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert result.status is Status.BLOCKED
        assert "unparseable JSON" in reason_of(result)
