"""Tests for drivers/gh.py: the blob round trip both drivers share.

The ref/path boundary is pinned here, at the seam, rather than in either
driver: a copy in the github driver's tests would let the file driver's own
blob fetches regress to the 404-into-`dead` this resolution exists to stop.
"""

import json

import pytest

from dex_engine.drivers.gh import Blob, BlobRef, blob_ref, fetch_blob, gh_api, gh_api_list
from dex_engine.pipeline.classify import Classification
from dex_engine.pipeline.types import Status
from tests.drivers.conftest import FakeGh, gh_contents, gh_fail, gh_matching_refs, gh_ok


def blob_of(url: str) -> BlobRef:
    ref = blob_ref(url)
    assert ref is not None
    return ref


def fetched(url: str, gh: FakeGh) -> Blob | Classification:
    return fetch_blob(gh, blob_of(url))


def bytes_of(outcome: Blob | Classification) -> bytes:
    assert isinstance(outcome, Blob)
    return outcome.data


def status_of(outcome: Blob | Classification) -> Status:
    assert isinstance(outcome, Classification)
    return outcome.status


class TestBlobRef:
    def test_a_blob_url_yields_the_repo_and_the_unsplit_tail(self):
        ref = blob_of("https://github.com/acme/kit/blob/main/docs/a.md")
        assert (ref.owner, ref.repo) == ("acme", "kit")
        assert ref.tail == ("main", "docs", "a.md")

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/acme/kit",
            "https://github.com/acme/kit/issues/7",
            "https://gist.github.com/acme/deadbeef",
            "https://example.test/acme/kit/blob/main/a.md",
        ],
    )
    def test_every_other_shape_is_not_a_blob(self, url):
        assert blob_ref(url) is None


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
        # `automation/bors-next` starts with `automation/bors` as a string;
        # segment-wise it is a different branch and must not take the URL.
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
