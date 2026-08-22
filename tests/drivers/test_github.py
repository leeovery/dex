"""Tests for drivers/github.py: URL-shape routing and classified gh failures."""

import base64
import json
from collections.abc import Sequence

import pytest

from dex_engine.drivers.github import GhResult, GitHubDriver
from dex_engine.pipeline.detect import detect_kind
from dex_engine.pipeline.registry import DRIVERS
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
        # The 404 sends the driver to look for a longer ref first; `main` is
        # the whole ref, so nothing re-splits and the 404 stands.
        driver = driver_for(
            {
                self.CONTENTS: fail("gh: Not Found (HTTP 404)"),
                ("api", "repos/acme/pipeline-kit/git/matching-refs/heads/main"): ok(
                    json.dumps([{"ref": "refs/heads/main"}])
                ),
                ("api", "repos/acme/pipeline-kit/git/matching-refs/tags/main"): ok("[]"),
            }
        )
        assert driver.fetch(make_unit(self.URL, Kind.GITHUB)).status is Status.DEAD

    def test_a_document_blob_parks_manual_naming_its_format(self):
        # A real ledger PDF blob fenced 40k characters of
        # replacement-character soup and went done.
        driver = driver_for({self.CONTENTS: contents(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj")})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert result.status is Status.MANUAL
        assert "is a pdf document" in reason_of(result)

    def test_an_unrecognized_binary_blob_parks_manual_never_fenced(self):
        driver = driver_for({self.CONTENTS: contents(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert result.status is Status.MANUAL
        assert "binary, not UTF-8 text" in reason_of(result)
        assert result.body is None

    def test_utf8_source_with_non_ascii_still_fences(self):
        driver = driver_for({self.CONTENTS: contents("# naïve — résumé\n".encode())})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert result.status is Status.DONE
        assert "naïve — résumé" in body_of(result)

    def test_an_lfs_pointer_parks_manual_instead_of_fencing_its_stand_in_text(self):
        # An unsmudged LFS pointer is honest UTF-8 with no signature, so an
        # unnamed sniff let 130 bytes of `oid sha256:…` fence and ledger
        # `done` as though it were the document it points at.
        pointer = (
            b"version https://git-lfs.github.com/spec/v1\n"
            b"oid sha256:08709a87567d8311d6fd29c4f4a5386801153e71450e628c4a5a5d7e85feda8b\n"
            b"size 7416886\n"
        )
        args = ("api", "repos/acme/pipeline-kit/contents/docs/sicp.pdf?ref=main")
        driver = driver_for({args: contents(pointer)})
        url = "https://github.com/acme/pipeline-kit/blob/main/docs/sicp.pdf"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert result.status is Status.MANUAL
        assert "is a pdf document" in reason_of(result)
        assert result.body is None

    def test_a_committed_csv_parks_for_the_extractor_rather_than_fencing(self):
        # Judgment call: a CSV is text and would fence, but it has no
        # signature either, so allowing it to fence is what lets an LFS
        # pointer for a .csv through. One rule — an extractable document
        # parks for capture — and csv-builtin then renders a real table
        # instead of a fence truncated at 40k characters.
        args = ("api", "repos/acme/pipeline-kit/contents/data/runs.csv?ref=main")
        driver = driver_for({args: contents(b"run,status\n1,done\n2,dead\n")})
        url = "https://github.com/acme/pipeline-kit/blob/main/data/runs.csv"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert result.status is Status.MANUAL
        assert "is a csv document" in reason_of(result)

    @pytest.mark.parametrize("path", ["src/detect.py", "README.md", "Makefile", "docs/notes.txt"])
    def test_source_and_prose_extensions_still_fence(self, path):
        # The extension fallback only knows Format values: nothing a repo
        # actually holds as source or prose is diverted by naming the file.
        args = ("api", f"repos/acme/pipeline-kit/contents/{path}?ref=main")
        driver = driver_for({args: contents(b"def detect(): ...")})
        url = f"https://github.com/acme/pipeline-kit/blob/main/{path}"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert result.status is Status.DONE
        assert body_of(result) == "```\ndef detect(): ...\n```"

    def test_a_sha_ref_needs_no_lookup(self):
        sha = "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"
        args = ("api", f"repos/acme/pipeline-kit/contents/src/detect.py?ref={sha}")
        gh = FakeGh({args: contents(b"def detect(): ...")})
        url = f"https://github.com/acme/pipeline-kit/blob/{sha}/src/detect.py"
        assert GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)).status is Status.DONE
        assert len(gh.calls) == 1  # the guess was right; no ref lookup was spent

    def test_oversize_blob_parks_manual_never_dead(self):
        # Over 1MB the contents API answers `encoding: "none"` with an empty
        # body — the file is there, just not inline.
        driver = driver_for(
            {self.CONTENTS: ok(json.dumps({"encoding": "none", "content": "", "size": 4645520}))}
        )
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert result.status is Status.MANUAL
        assert "larger than the contents API serves inline" in reason_of(result)


def matching_refs(*names: str) -> GhResult:
    """A ``git/matching-refs`` page, in the API's own shape."""
    return ok(json.dumps([{"ref": name, "object": {"sha": "0" * 40}} for name in names]))


class TestBlobRefBoundary:
    """Where the ref stops and the path starts is not in the URL — resolve it.

    raw.githubusercontent.com settled the boundary server-side. The contents
    API takes the two halves apart, so a slashed branch or a `refs/heads/`
    permalink sent a wrong ref AND a wrong path, 404'd, and ledgered live
    files `dead`.
    """

    REPO = "repos/rust-lang/rust"

    def test_a_slashed_branch_resolves_through_the_repos_own_refs(self):
        gh = FakeGh(
            {
                # The one-segment guess: branch `automation`, path `bors/…`.
                ("api", f"{self.REPO}/contents/bors/auto/README.md?ref=automation"): fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): matching_refs(
                    "refs/heads/automation/bors/auto",
                    "refs/heads/automation/bors/auto-merge",
                    "refs/heads/automation/bors/try",
                ),
                (
                    "api",
                    f"{self.REPO}/contents/README.md?ref=automation%2Fbors%2Fauto",
                ): contents(b"# The Rust Programming Language"),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/automation/bors/auto/README.md"
        result = GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB))
        assert result.status is Status.DONE
        assert result.meta["file"] == "README.md"
        assert "Rust" in body_of(result)

    def test_a_sibling_ref_never_claims_the_path_by_string_prefix(self):
        # `automation/bors-next` starts with `automation/bors` as a string;
        # segment-wise it is a different branch and must not take the URL.
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/bors/README.md?ref=automation"): fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): matching_refs(
                    "refs/heads/automation/bors-next", "refs/heads/automation/bors"
                ),
                ("api", f"{self.REPO}/contents/README.md?ref=automation%2Fbors"): contents(b"ok"),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/automation/bors/README.md"
        assert GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)).status is Status.DONE

    def test_the_longest_matching_ref_wins(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/1.2/docs/x.md?ref=release"): fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/release"): matching_refs(
                    "refs/heads/release", "refs/heads/release/1.2"
                ),
                ("api", f"{self.REPO}/contents/docs/x.md?ref=release%2F1.2"): contents(b"ok"),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/release/1.2/docs/x.md"
        assert GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)).status is Status.DONE

    def test_a_ref_that_would_swallow_the_whole_tail_is_not_a_split(self):
        # Branch `docs/x.md` exists, but then the URL addresses no file at
        # all — the guess (and its 404) stands.
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/x.md?ref=docs"): fail("gh: Not Found (HTTP 404)"),
                ("api", f"{self.REPO}/git/matching-refs/heads/docs"): matching_refs(
                    "refs/heads/docs/x.md"
                ),
                ("api", f"{self.REPO}/git/matching-refs/tags/docs"): matching_refs(),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/docs/x.md"
        assert GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)).status is Status.DEAD

    def test_a_slashed_tag_resolves_after_the_branches_come_back_empty(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/9/README.md?ref=v1"): fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/v1"): matching_refs(),
                ("api", f"{self.REPO}/git/matching-refs/tags/v1"): matching_refs("refs/tags/v1/9"),
                ("api", f"{self.REPO}/contents/README.md?ref=v1%2F9"): contents(b"ok"),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/v1/9/README.md"
        assert GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)).status is Status.DONE

    @pytest.mark.parametrize(
        ("tail", "endpoint"),
        [
            ("refs/heads/master/README", "contents/README?ref=refs%2Fheads%2Fmaster"),
            ("refs/tags/v1.0.0/docs/x.md", "contents/docs/x.md?ref=refs%2Ftags%2Fv1.0.0"),
        ],
    )
    def test_the_refs_prefix_permalink_form_needs_no_lookup(self, tail, endpoint):
        # GitHub code search returns four figures of `blob/refs/heads/` links;
        # the form names its own namespace, so the split is free.
        gh = FakeGh({("api", f"{self.REPO}/{endpoint}"): contents(b"Hello World!")})
        url = f"https://github.com/rust-lang/rust/blob/{tail}"
        result = GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB))
        assert result.status is Status.DONE
        assert len(gh.calls) == 1

    def test_a_refs_prefix_permalink_on_a_slashed_branch_still_resolves(self):
        gh = FakeGh(
            {
                (
                    "api",
                    f"{self.REPO}/contents/bors/auto/README.md?ref=refs%2Fheads%2Fautomation",
                ): fail("gh: Not Found (HTTP 404)"),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): matching_refs(
                    "refs/heads/automation/bors/auto"
                ),
                (
                    "api",
                    f"{self.REPO}/contents/README.md?ref=refs%2Fheads%2Fautomation%2Fbors%2Fauto",
                ): contents(b"ok"),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/refs/heads/automation/bors/auto/README.md"
        assert GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)).status is Status.DONE

    def test_a_genuinely_missing_path_is_still_dead(self):
        # The floor the resolution must not break: no ref rescues a file
        # that is not there, and the unit must not go unclassifiable.
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/no-such-file.txt?ref=master"): fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/master"): matching_refs(
                    "refs/heads/master"
                ),
                ("api", f"{self.REPO}/git/matching-refs/tags/master"): matching_refs(),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/master/no-such-file.txt"
        assert GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)).status is Status.DEAD

    def test_a_missing_path_on_a_resolved_slashed_branch_is_dead(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/bors/auto/nope.md?ref=automation"): fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): matching_refs(
                    "refs/heads/automation/bors/auto"
                ),
                ("api", f"{self.REPO}/contents/nope.md?ref=automation%2Fbors%2Fauto"): fail(
                    "gh: Not Found (HTTP 404)"
                ),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/automation/bors/auto/nope.md"
        assert GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)).status is Status.DEAD

    def test_a_failing_ref_lookup_leaves_the_original_classification(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/bors/auto/README.md?ref=automation"): fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): fail(
                    "gh: API rate limit exceeded (HTTP 403)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/tags/automation"): fail(
                    "gh: API rate limit exceeded (HTTP 403)"
                ),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/automation/bors/auto/README.md"
        assert GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)).status is Status.DEAD

    def test_a_non_404_failure_never_spends_a_ref_lookup(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/bors/auto/README.md?ref=automation"): fail(
                    "gh: API rate limit exceeded (HTTP 403)"
                )
            }
        )
        url = "https://github.com/rust-lang/rust/blob/automation/bors/auto/README.md"
        assert GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)).status is Status.BLOCKED
        assert len(gh.calls) == 1


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
