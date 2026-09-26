"""Tests for drivers/gh.py: the path round trip both drivers share.

The ref/path boundary is pinned here, at the seam, rather than in either
driver: a copy in the github driver's tests would let the file driver's own
blob fetches regress to the 404-into-`dead` this resolution exists to stop.
"""

import json

import pytest

from dex_engine.drivers.gh import (
    Blob,
    RefPath,
    Tree,
    fetch_blob,
    fetch_path,
    fetch_readme,
    gh_api,
    gh_api_list,
    ref_path,
)
from dex_engine.pipeline.classify import Classification
from dex_engine.pipeline.types import Status
from tests.drivers.conftest import (
    FakeGh,
    gh_contents,
    gh_fail,
    gh_listing,
    gh_matching_refs,
    gh_ok,
)

RAW = ("-H", "Accept: application/vnd.github.raw+json")


def path_of(url: str) -> RefPath:
    ref = ref_path(url)
    assert ref is not None
    return ref


def fetched(url: str, gh: FakeGh) -> Blob | Classification:
    return fetch_blob(gh, path_of(url))


def found(url: str, gh: FakeGh) -> Blob | Tree | Classification:
    return fetch_path(gh, path_of(url))


def bytes_of(outcome: Blob | Classification) -> bytes:
    assert isinstance(outcome, Blob)
    return outcome.data


def status_of(outcome: Blob | Tree | Classification) -> Status:
    assert isinstance(outcome, Classification)
    return outcome.status


def tree_of(outcome: Blob | Tree | Classification) -> Tree:
    assert isinstance(outcome, Tree), f"expected a Tree, got {outcome!r}"
    return outcome


class TestRefPath:
    def test_a_blob_url_yields_the_repo_and_the_unsplit_tail(self):
        ref = path_of("https://github.com/acme/kit/blob/main/docs/a.md")
        assert (ref.owner, ref.repo) == ("acme", "kit")
        assert ref.tail == ("main", "docs", "a.md")

    @pytest.mark.parametrize(("view", "min_path"), [("blob", 1), ("raw", 1), ("tree", 0)])
    def test_each_view_of_a_path_is_one_address(self, view, min_path):
        # GitHub redirects blob and tree to each other by what the path
        # holds, and raw serves the same file — one address, three
        # spellings. Only a tree link may be a ref alone.
        ref = path_of(f"https://github.com/acme/kit/{view}/main/docs")
        assert ref == RefPath(owner="acme", repo="kit", tail=("main", "docs"), min_path=min_path)

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/acme/kit",
            "https://github.com/acme/kit/issues/7",
            "https://github.com/acme/kit/tree",
            "https://github.com/acme/kit/blame/main/a.md",
            "https://github.com/acme/kit/commit/7fd1a60b01f91b314f59955a4e4d4e80d8edf11d",
            "https://gist.github.com/acme/deadbeef",
            "https://example.test/acme/kit/blob/main/a.md",
        ],
    )
    def test_every_other_shape_is_not_a_path(self, url):
        assert ref_path(url) is None


class TestBlobBytes:
    URL = "https://github.com/acme/kit/blob/main/src/detect.py"
    CONTENTS = ("api", "repos/acme/kit/contents/src/detect.py?ref=main")

    def test_the_bytes_and_the_path_come_back_together(self):
        # raw.githubusercontent.com is unauthenticated: it 404s for every
        # private-repo blob however the machine is signed in, and that 404
        # classified live content as dead. gh carries the auth.
        outcome = fetched(self.URL, FakeGh({self.CONTENTS: gh_contents(b"def detect(): ...")}))
        assert isinstance(outcome, Blob)
        assert outcome.path == "src/detect.py"
        assert outcome.data == b"def detect(): ..."

    @pytest.mark.parametrize("spelling", ["a b.md", "a%20b.md"])
    def test_a_path_reaches_the_api_encoded_exactly_once(self, spelling):
        args = ("api", "repos/acme/kit/contents/docs/a%20b.md?ref=main")
        gh = FakeGh({args: gh_contents(b"hello")})
        assert bytes_of(fetched(f"https://github.com/acme/kit/blob/main/docs/{spelling}", gh))

    def test_an_oversize_blob_is_manual_never_dead(self):
        # Over 1MB the contents API answers `encoding: "none"` with an empty
        # body — the file is there, just not inline.
        payload = gh_ok(json.dumps({"encoding": "none", "content": "", "size": 4645520}))
        outcome = fetched(self.URL, FakeGh({self.CONTENTS: payload}))
        assert status_of(outcome) is Status.MANUAL
        assert isinstance(outcome, Classification)
        assert "larger than the contents API serves inline" in str(outcome.reason)

    def test_a_403_stays_blocked(self):
        gh = FakeGh({self.CONTENTS: gh_fail("gh: API rate limit exceeded (HTTP 403)")})
        assert status_of(fetched(self.URL, gh)) is Status.BLOCKED


class TestRefBoundary:
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
                ("api", f"{self.REPO}/contents/bors/auto/README.md?ref=automation"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): gh_matching_refs(
                    "refs/heads/automation/bors/auto",
                    "refs/heads/automation/bors/auto-merge",
                    "refs/heads/automation/bors/try",
                ),
                (
                    "api",
                    f"{self.REPO}/contents/README.md?ref=automation%2Fbors%2Fauto",
                ): gh_contents(b"# The Rust Programming Language"),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/automation/bors/auto/README.md"
        outcome = fetched(url, gh)
        assert isinstance(outcome, Blob)
        # The path the WINNING split left, not the guess's: whatever sniffs
        # these bytes is handed the real filename.
        assert outcome.path == "README.md"
        assert b"Rust" in outcome.data

    def test_a_sibling_ref_never_claims_the_path_by_string_prefix(self):
        # The repo has `automation/bors`; the URL's branch is
        # `automation/bors-next`, which the repo has NOT pushed. As a string
        # the short one is a prefix of the URL, and taking it would send
        # `?ref=automation/bors` with a path of `-next/README.md` — a request
        # this fake would not even recognize. Segment-wise it does not match
        # at all, so nothing re-splits and the guess's 404 stands.
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/bors-next/README.md?ref=automation"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): gh_matching_refs(
                    "refs/heads/automation/bors"
                ),
                ("api", f"{self.REPO}/git/matching-refs/tags/automation"): gh_matching_refs(),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/automation/bors-next/README.md"
        assert status_of(fetched(url, gh)) is Status.DEAD

    def test_the_right_sibling_still_wins_when_the_repo_has_both(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/bors/README.md?ref=automation"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): gh_matching_refs(
                    "refs/heads/automation/bors-next", "refs/heads/automation/bors"
                ),
                ("api", f"{self.REPO}/contents/README.md?ref=automation%2Fbors"): gh_contents(
                    b"ok"
                ),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/automation/bors/README.md"
        assert bytes_of(fetched(url, gh)) == b"ok"

    def test_the_longest_matching_ref_wins(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/1.2/docs/x.md?ref=release"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/release"): gh_matching_refs(
                    "refs/heads/release", "refs/heads/release/1.2"
                ),
                ("api", f"{self.REPO}/contents/docs/x.md?ref=release%2F1.2"): gh_contents(b"ok"),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/release/1.2/docs/x.md"
        assert bytes_of(fetched(url, gh)) == b"ok"

    def test_a_ref_that_would_swallow_the_whole_tail_is_not_a_split(self):
        # Branch `docs/x.md` exists, but then the URL addresses no file at
        # all — the guess (and its 404) stands.
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/x.md?ref=docs"): gh_fail("gh: Not Found (HTTP 404)"),
                ("api", f"{self.REPO}/git/matching-refs/heads/docs"): gh_matching_refs(
                    "refs/heads/docs/x.md"
                ),
                ("api", f"{self.REPO}/git/matching-refs/tags/docs"): gh_matching_refs(),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/docs/x.md"
        assert status_of(fetched(url, gh)) is Status.DEAD

    def test_a_longer_ref_listed_first_never_hides_the_match(self):
        # The API lists refs in byte order, so `release/1.10/…` precedes
        # `release/1.2`: a name too long to leave a path is passed over,
        # never taken as the end of the search.
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/1.2/docs/x.md?ref=release"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/release"): gh_matching_refs(
                    "refs/heads/release/1.10/a/b/c", "refs/heads/release/1.2"
                ),
                ("api", f"{self.REPO}/contents/docs/x.md?ref=release%2F1.2"): gh_contents(b"ok"),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/release/1.2/docs/x.md"
        assert bytes_of(fetched(url, gh)) == b"ok"

    def test_a_slashed_tag_resolves_after_the_branches_come_back_empty(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/9/README.md?ref=v1"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/v1"): gh_matching_refs(),
                ("api", f"{self.REPO}/git/matching-refs/tags/v1"): gh_matching_refs(
                    "refs/tags/v1/9"
                ),
                ("api", f"{self.REPO}/contents/README.md?ref=v1%2F9"): gh_contents(b"ok"),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/v1/9/README.md"
        assert bytes_of(fetched(url, gh)) == b"ok"

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
        gh = FakeGh({("api", f"{self.REPO}/{endpoint}"): gh_contents(b"Hello World!")})
        assert bytes_of(fetched(f"https://github.com/rust-lang/rust/blob/{tail}", gh))
        assert len(gh.calls) == 1

    def test_a_refs_prefix_permalink_on_a_slashed_branch_still_resolves(self):
        gh = FakeGh(
            {
                (
                    "api",
                    f"{self.REPO}/contents/bors/auto/README.md?ref=refs%2Fheads%2Fautomation",
                ): gh_fail("gh: Not Found (HTTP 404)"),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): gh_matching_refs(
                    "refs/heads/automation/bors/auto"
                ),
                (
                    "api",
                    f"{self.REPO}/contents/README.md?ref=refs%2Fheads%2Fautomation%2Fbors%2Fauto",
                ): gh_contents(b"ok"),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/refs/heads/automation/bors/auto/README.md"
        assert bytes_of(fetched(url, gh)) == b"ok"

    def test_a_sha_ref_needs_no_lookup(self):
        sha = "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"
        args = ("api", f"repos/acme/kit/contents/src/detect.py?ref={sha}")
        gh = FakeGh({args: gh_contents(b"def detect(): ...")})
        assert bytes_of(fetched(f"https://github.com/acme/kit/blob/{sha}/src/detect.py", gh))
        assert len(gh.calls) == 1  # the guess was right; no ref lookup was spent

    def test_a_genuinely_missing_path_is_still_dead(self):
        # The floor the resolution must not break: no ref rescues a file
        # that is not there, and the unit must not go unclassifiable.
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/no-such-file.txt?ref=master"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/master"): gh_matching_refs(
                    "refs/heads/master"
                ),
                ("api", f"{self.REPO}/git/matching-refs/tags/master"): gh_matching_refs(),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/master/no-such-file.txt"
        assert status_of(fetched(url, gh)) is Status.DEAD

    def test_a_missing_path_on_a_resolved_slashed_branch_is_dead(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/bors/auto/nope.md?ref=automation"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): gh_matching_refs(
                    "refs/heads/automation/bors/auto"
                ),
                ("api", f"{self.REPO}/contents/nope.md?ref=automation%2Fbors%2Fauto"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/automation/bors/auto/nope.md"
        assert status_of(fetched(url, gh)) is Status.DEAD

    def test_a_failing_ref_lookup_leaves_the_original_classification(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/bors/auto/README.md?ref=automation"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): gh_fail(
                    "gh: API rate limit exceeded (HTTP 403)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/tags/automation"): gh_fail(
                    "gh: API rate limit exceeded (HTTP 403)"
                ),
            }
        )
        url = "https://github.com/rust-lang/rust/blob/automation/bors/auto/README.md"
        assert status_of(fetched(url, gh)) is Status.DEAD

    def test_a_non_404_failure_never_spends_a_ref_lookup(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/bors/auto/README.md?ref=automation"): gh_fail(
                    "gh: API rate limit exceeded (HTTP 403)"
                )
            }
        )
        url = "https://github.com/rust-lang/rust/blob/automation/bors/auto/README.md"
        assert status_of(fetched(url, gh)) is Status.BLOCKED
        assert len(gh.calls) == 1

    def test_a_tree_link_may_name_a_slashed_branch_alone(self):
        # GitHub renders `tree/automation/bors/auto` — the branch's root —
        # and 404s the same tail as a blob: for a tree link, a ref that
        # swallows the whole tail IS the split.
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/bors/auto?ref=automation"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): gh_matching_refs(
                    "refs/heads/automation/bors/auto"
                ),
                ("api", f"{self.REPO}/contents/?ref=automation%2Fbors%2Fauto"): gh_listing(
                    ("README.md", "file")
                ),
            }
        )
        tree = tree_of(found("https://github.com/rust-lang/rust/tree/automation/bors/auto", gh))
        assert (tree.ref, tree.path) == ("automation/bors/auto", "")

    def test_a_tree_link_on_a_slashed_branch_resolves_to_its_directory(self):
        gh = FakeGh(
            {
                ("api", f"{self.REPO}/contents/bors/auto/src/tools/clippy?ref=automation"): gh_fail(
                    "gh: Not Found (HTTP 404)"
                ),
                ("api", f"{self.REPO}/git/matching-refs/heads/automation"): gh_matching_refs(
                    "refs/heads/automation/bors/auto", "refs/heads/automation/bors/try"
                ),
                (
                    "api",
                    f"{self.REPO}/contents/src/tools/clippy?ref=automation%2Fbors%2Fauto",
                ): gh_listing(("README.md", "file"), ("clippy_lints", "dir")),
            }
        )
        url = "https://github.com/rust-lang/rust/tree/automation/bors/auto/src/tools/clippy"
        tree = tree_of(found(url, gh))
        assert (tree.ref, tree.path) == ("automation/bors/auto", "src/tools/clippy")

    @pytest.mark.parametrize("view", ["blob", "tree"])
    def test_the_refs_prefix_form_may_name_the_ref_alone(self, view):
        # `blob/main` and `tree/main` both address the branch's root, so the
        # qualified spelling of that same ref does too — no lookup spent.
        gh = FakeGh(
            {("api", f"{self.REPO}/contents/?ref=refs%2Fheads%2Fmaster"): gh_listing(("a", "file"))}
        )
        tree = tree_of(found(f"https://github.com/rust-lang/rust/{view}/refs/heads/master", gh))
        assert (tree.ref, tree.path) == ("refs/heads/master", "")
        assert len(gh.calls) == 1


class TestDirectories:
    URL = "https://github.com/acme/kit/tree/main/src"
    CONTENTS = ("api", "repos/acme/kit/contents/src?ref=main")

    def test_a_directory_answers_with_its_listing_at_the_resolved_split(self):
        gh = FakeGh({self.CONTENTS: gh_listing(("detect.py", "file"), ("drivers", "dir"))})
        assert found(self.URL, gh) == Tree(
            ref="main", path="src", entries=("detect.py", "drivers/")
        )

    def test_a_blob_link_to_a_directory_is_the_same_directory(self):
        # GitHub redirects `blob/main/src` to `tree/main/src`; the contents
        # API's array answer is what says which one the path holds.
        gh = FakeGh({self.CONTENTS: gh_listing(("detect.py", "file"))})
        url = "https://github.com/acme/kit/blob/main/src"
        assert tree_of(found(url, gh)).entries == ("detect.py",)

    def test_a_tree_link_to_a_file_is_the_file(self):
        gh = FakeGh({self.CONTENTS: gh_contents(b"def detect(): ...")})
        assert found(self.URL, gh) == Blob(path="src", data=b"def detect(): ...")

    def test_listing_entries_without_a_name_are_skipped(self):
        answer = json.dumps(["stray", {"type": "file"}, {"name": 7}, {"name": "ok.py"}])
        gh = FakeGh({self.CONTENTS: gh_ok(answer)})
        assert tree_of(found(self.URL, gh)).entries == ("ok.py",)

    def test_a_missing_directory_is_dead(self):
        gh = FakeGh(
            {
                self.CONTENTS: gh_fail("gh: Not Found (HTTP 404)"),
                ("api", "repos/acme/kit/git/matching-refs/heads/main"): gh_matching_refs(
                    "refs/heads/main"
                ),
                ("api", "repos/acme/kit/git/matching-refs/tags/main"): gh_matching_refs(),
            }
        )
        assert status_of(found(self.URL, gh)) is Status.DEAD

    @pytest.mark.parametrize(
        ("tail", "endpoint", "where"),
        [
            ("main/src", "contents/src?ref=main", "src"),
            ("main", "contents/?ref=main", "the repo root"),
        ],
    )
    def test_a_file_reader_handed_a_directory_parks_it(self, tail, endpoint, where):
        # Only the file driver asks for bytes alone, and it is handed paths
        # the github driver already read as a file — a directory there means
        # the repo changed under the unit, and no retry fixes that.
        url = f"https://github.com/acme/kit/blob/{tail}"
        gh = FakeGh({("api", f"repos/acme/kit/{endpoint}"): gh_listing(("a.py", "file"))})
        outcome = fetched(url, gh)
        assert status_of(outcome) is Status.MANUAL
        assert isinstance(outcome, Classification)
        assert outcome.reason == f"{where} is a directory, not a file"

    @pytest.mark.parametrize("answer", ['"a string"', "7", "null"])
    def test_an_answer_neither_file_nor_directory_is_blocked(self, answer):
        outcome = found(self.URL, FakeGh({self.CONTENTS: gh_ok(answer)}))
        assert status_of(outcome) is Status.BLOCKED
        assert isinstance(outcome, Classification)
        assert "unexpected shape" in outcome.reason


class TestReadme:
    def test_the_repo_roots_readme_by_default(self):
        gh = FakeGh({("api", "repos/acme/kit/readme", *RAW): gh_ok("# kit\n")})
        assert fetch_readme(gh, "acme", "kit") == "# kit\n"

    def test_a_directorys_readme_at_a_slashed_ref_is_encoded_once(self):
        endpoint = "repos/rust-lang/rust/readme/src/my%20tools?ref=automation%2Fbors%2Fauto"
        gh = FakeGh({("api", endpoint, *RAW): gh_ok("# Clippy\n")})
        readme = fetch_readme(
            gh, "rust-lang", "rust", ref="automation/bors/auto", path="src/my%20tools"
        )
        assert readme == "# Clippy\n"

    def test_a_directory_without_one_answers_none(self):
        gh = FakeGh(
            {
                ("api", "repos/acme/kit/readme/examples?ref=main", *RAW): gh_fail(
                    "gh: Not Found (HTTP 404)"
                )
            }
        )
        assert fetch_readme(gh, "acme", "kit", ref="main", path="examples") is None

    def test_any_other_failure_classifies(self):
        # A rate limit is not "no README": answering None would ledger the
        # unit done without it, and done units are never fetched again.
        gh = FakeGh(
            {
                ("api", "repos/acme/kit/readme", *RAW): gh_fail(
                    "gh: API rate limit exceeded (HTTP 403)"
                )
            }
        )
        outcome = fetch_readme(gh, "acme", "kit")
        assert isinstance(outcome, Classification)
        assert outcome.status is Status.BLOCKED


class TestGhApi:
    def test_a_timeout_is_blocked_with_no_http_code_to_read(self):
        gh = FakeGh({("api", "repos/acme/kit"): gh_fail("gh timed out after 120s")})
        outcome = gh_api(gh, "repos/acme/kit")
        assert isinstance(outcome, Classification)
        assert outcome.status is Status.BLOCKED

    def test_unparseable_json_is_blocked(self):
        gh = FakeGh({("api", "repos/acme/kit"): gh_ok("<html>oops</html>")})
        outcome = gh_api(gh, "repos/acme/kit")
        assert isinstance(outcome, Classification)
        assert "unparseable JSON" in str(outcome.reason)

    def test_an_array_where_an_object_belongs_is_blocked(self):
        gh = FakeGh({("api", "repos/acme/kit"): gh_ok("[]")})
        outcome = gh_api(gh, "repos/acme/kit")
        assert isinstance(outcome, Classification)
        assert "unexpected shape" in str(outcome.reason)

    @pytest.mark.parametrize("payload", [gh_fail("boom"), gh_ok("not json"), gh_ok("{}")])
    def test_a_list_call_answers_empty_for_every_failure(self, payload):
        # Both callers read an empty answer as "no extra information", never
        # as a failure: a profile's repo listing, and a ref lookup that must
        # leave the contents call's own classification standing.
        assert gh_api_list(FakeGh({("api", "x"): payload}), "x") == []
