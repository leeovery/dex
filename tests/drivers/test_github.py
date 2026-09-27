"""Tests for drivers/github.py: URL-shape routing, and what a path's contents mean.

The path round trip itself — the ref/path boundary, the contents endpoint,
the oversize park — belongs to the shared seam and is pinned in test_gh.py.
What is here is the driver's own judgment: which route a shape takes, and
whether what comes back is fenced, listed, re-detected or parked.
"""

import json

import pytest

from dex_engine.drivers.github import GitHubDriver
from dex_engine.pipeline.detect import Detection, detect, detect_kind
from dex_engine.pipeline.registry import default_drivers
from dex_engine.pipeline.types import (
    Content,
    Format,
    Job,
    Kind,
    Missing,
    Redetected,
    Refused,
    Unusable,
)
from tests.drivers.conftest import (
    FakeGh,
    body_of,
    content_of,
    fixture_text,
    gh_contents,
    gh_fail,
    gh_listing,
    gh_matching_refs,
    gh_ok,
    make_unit,
)

DRIVERS = default_drivers()


def driver_for(gh_responses: dict | None = None) -> GitHubDriver:
    return GitHubDriver(gh=FakeGh(gh_responses or {}))


RAW = ("-H", "Accept: application/vnd.github.raw+json")
README_ARGS = ("api", "repos/acme/pipeline-kit/readme", *RAW)
REPO_ARGS = ("api", "repos/acme/pipeline-kit")
NOT_FOUND = gh_fail("gh: Not Found (HTTP 404)")
RATE_LIMITED = gh_fail("gh: API rate limit exceeded (HTTP 403)")

ATTACHED_PDF = (
    "https://github.com/user-attachments/files/31802150/"
    "EXTERNAL.Function.Hooks.Core.Architecture.pdf"
)
ATTACHED_PICTURE = "https://github.com/user-attachments/assets/2fad9a87-d37f-4e00-9d4e-1aadf9326e77"


def truncated(kept: int, whole: int, url: str) -> str:
    return f"**Truncated:** cut at {kept:,} of {whole:,} characters; the rest is at {url}"


def lines(count: int, width: int) -> str:
    """``count`` lines of ``width`` characters each, newline included."""
    return "".join(f"{n:0{width - 1}d}\n" for n in range(count))


def unusable_of(outcome: object) -> Unusable:
    assert isinstance(outcome, Unusable), f"expected Unusable, got {outcome!r}"
    return outcome


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
    ATTACHED_PDF,
    ATTACHED_PICTURE,
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

    def test_an_attached_document_reaches_file_work_through_the_head_sniff(self):
        # Read as owner `user-attachments`, repo `files`, the link 404'd into
        # `dead` while a plain GET served the PDF. Declined, it is the
        # catch-all's, and detection's one HEAD names the document.
        detection = detect(ATTACHED_PDF, DRIVERS, sniff=lambda _: "application/pdf")
        assert detection == Detection(kind=Kind.FILE, format=Format.PDF)


class TestLedgeredAttachments:
    """A unit ledgered github before the namespace was reserved still heals.

    A requeue drives a unit by its stored kind and never re-detects it, so
    reserving the segment alone would leave these dead forever.
    """

    def test_an_attached_document_corrects_to_file_work(self):
        gh = FakeGh({})
        result = GitHubDriver(gh=gh).fetch(make_unit(ATTACHED_PDF, Kind.GITHUB))
        assert result == Redetected(kind=Kind.FILE, format=Format.PDF)
        assert gh.calls == []

    def test_an_attached_picture_corrects_straight_to_media_work(self):
        # Never to plain web: the web driver would correct it again on
        # seeing the picture, and two corrections in one run park as a loop.
        result = driver_for().fetch(make_unit(ATTACHED_PICTURE, Kind.GITHUB))
        assert result == Redetected(kind=Kind.WEB, job=Job.MEDIA)

    def test_the_namespace_is_matched_as_matches_declines_it(self):
        url = "https://github.com/User-Attachments/assets/2fad9a87-d37f-4e00-9d4e-1aadf9326e77"
        assert not driver_for().matches(url)
        assert driver_for().fetch(make_unit(url, Kind.GITHUB)) == Redetected(
            kind=Kind.WEB, job=Job.MEDIA
        )

    def test_an_attachment_that_is_no_document_parks_naming_it(self):
        url = "https://github.com/user-attachments/files/31802151/crash%20log.zip"
        result = unusable_of(driver_for().fetch(make_unit(url, Kind.GITHUB)))
        assert result.rescuable
        assert result.evidence.startswith("crash log.zip is a download")

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/user-attachments",
            "https://github.com/user-attachments/files/31802150",
            "https://github.com/user-attachments/assets/2fad9a87/extra",
            "https://github.com/user-attachments/videos/2fad9a87",
        ],
    )
    def test_any_other_attachment_shape_parks_naming_the_namespace(self, url):
        result = unusable_of(driver_for().fetch(make_unit(url, Kind.GITHUB)))
        assert result.rescuable
        assert result.evidence.startswith("github /user-attachments/ link")


class TestRepo:
    def test_repo_meta_and_readme_body(self):
        driver = driver_for(
            {
                ("api", "repos/acme/pipeline-kit"): gh_ok(fixture_text("github", "repo.json")),
                README_ARGS: gh_ok(fixture_text("github", "readme.md")),
            }
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert isinstance(result, Content)
        assert result.meta == {
            "title": "acme/pipeline-kit",
            "description": "Driver-based ingestion machinery: detect, fetch, classify, ledger.",
            "stars": 2481,
            "archived": None,  # not archived -> omitted from frontmatter
            # capped at 8 of the fixture's 9 topics
            "topics": "ingestion, pipelines, ledger, python, knowledge-base, etl, cli, jsonl",
            "ref": None,  # the default branch's README -> omitted from frontmatter
        }
        assert "# pipeline-kit" in body_of(result)

    def test_an_archived_repo_says_so(self):
        payload = {**json.loads(fixture_text("github", "repo.json")), "archived": True}
        driver = driver_for({REPO_ARGS: gh_ok(json.dumps(payload)), README_ARGS: gh_ok("# kit")})
        result = content_of(
            driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        )
        assert result.meta["archived"] == "true"

    def test_missing_readme_yields_placeholder_body(self):
        driver = driver_for(
            {
                ("api", "repos/acme/pipeline-kit"): gh_ok(fixture_text("github", "repo.json")),
                README_ARGS: gh_fail("gh: Not Found (HTTP 404)"),
            }
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert isinstance(result, Content)
        assert body_of(result) == "(no README)"

    def test_a_readme_the_api_refused_is_blocked_never_absent(self):
        # "(no README)" on a rate limit ledgered the repo done without its
        # README, and a done unit is never fetched again.
        driver = driver_for(
            {REPO_ARGS: gh_ok(fixture_text("github", "repo.json")), README_ARGS: RATE_LIMITED}
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert isinstance(result, Refused)
        assert not result.permanent

    def test_a_readme_over_the_cap_is_cut_at_a_line_and_says_so(self):
        readme = lines(1_000, 61)
        driver = driver_for(
            {REPO_ARGS: gh_ok(fixture_text("github", "repo.json")), README_ARGS: gh_ok(readme)}
        )
        url = "https://github.com/acme/pipeline-kit"
        body = body_of(driver.fetch(make_unit(url, Kind.GITHUB)))
        assert body == f"{readme[:59_962]}\n\n{truncated(59_962, 61_000, url)}"

    def test_a_readme_at_the_cap_is_whole(self):
        readme = lines(1_000, 60)
        driver = driver_for(
            {REPO_ARGS: gh_ok(fixture_text("github", "repo.json")), README_ARGS: gh_ok(readme)}
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert body_of(result) == readme.rstrip()

    def test_a_blank_readme_is_no_readme(self):
        driver = driver_for(
            {REPO_ARGS: gh_ok(fixture_text("github", "repo.json")), README_ARGS: gh_ok(" \n\n")}
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert body_of(result) == "(no README)"

    def test_deleted_repo_is_dead(self):
        driver = driver_for({("api", "repos/acme/gone"): gh_fail("gh: Not Found (HTTP 404)")})
        result = driver.fetch(make_unit("https://github.com/acme/gone", Kind.GITHUB))
        assert isinstance(result, Missing)

    def test_rate_limited_api_is_blocked(self):
        driver = driver_for(
            {("api", "repos/acme/pipeline-kit"): gh_fail("gh: API rate limit exceeded (HTTP 403)")}
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert isinstance(result, Refused)
        assert not result.permanent

    def test_gh_timeout_shape_classifies_blocked(self):
        # run_gh converts TimeoutExpired to this GhResult shape — a hung gh
        # is the world misbehaving, never an engine error.
        timeout = gh_fail("gh timed out after 120s")
        driver = driver_for({("api", "repos/acme/pipeline-kit"): timeout})
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert isinstance(result, Refused)
        assert "timed out" in result.evidence

    def test_gh_failure_without_a_code_is_blocked_with_scrubbed_reason(self):
        driver = driver_for(
            {
                ("api", "repos/acme/pipeline-kit"): gh_fail(
                    "error connecting to api.github.com from /Users/owner/base"
                )
            }
        )
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert isinstance(result, Refused)
        assert "/Users/owner" not in result.evidence


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
        assert isinstance(result, Content)
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
        assert isinstance(result, Content)
        assert "(repo listing unavailable)" in body_of(result)


class TestGist:
    def test_gist_files_render_fenced(self):
        driver = driver_for(
            {("api", "gists/abc123def456"): gh_ok(fixture_text("github", "gist.json"))}
        )
        url = "https://gist.github.com/octomaint/abc123def456"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert isinstance(result, Content)
        assert result.meta["title"] == "ledger compaction one-liner"
        body = body_of(result)
        assert "### compact.py" in body
        assert "### notes.md" in body
        assert "Last-per-hash wins" in body

    def test_gist_index_is_unusable_with_nothing_to_rescue(self):
        result = driver_for().fetch(make_unit("https://gist.github.com/octomaint", Kind.GITHUB))
        assert isinstance(result, Unusable)
        assert not result.rescuable  # an index addresses no unit; nothing for judgment either
        assert "gist index" in result.evidence

    def test_a_gist_file_over_the_cap_says_so_after_its_fence(self):
        # The field case: a 20,000-character report stored as the whole gist.
        report = lines(300, 70)
        payload = {
            "description": "report",
            "files": {"report.md": {"content": report}, "notes.md": {"content": "short\n"}},
        }
        driver = driver_for({("api", "gists/abc123def456"): gh_ok(json.dumps(payload))})
        url = "https://gist.github.com/octomaint/abc123def456"
        body = body_of(driver.fetch(make_unit(url, Kind.GITHUB)))
        assert body == (
            f"### report.md\n```\n{report[:19_949]}\n```\n\n{truncated(19_949, 21_000, url)}"
            "\n\n### notes.md\n```\nshort\n\n```"
        )

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
        assert isinstance(result, Content)
        assert "### compact.py" in body_of(result)


class TestIssue:
    def test_issue_title_and_body(self):
        issue = gh_ok(fixture_text("github", "issue.json"))
        driver = driver_for({("api", "repos/acme/pipeline-kit/issues/42"): issue})
        url = "https://github.com/acme/pipeline-kit/issues/42"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert isinstance(result, Content)
        assert result.meta["title"] == "enricher ledgers 403 challenges as dead"
        assert "Cloudflare 403" in body_of(result)

    @pytest.mark.parametrize("tail", ["pull/7", "pull/7/files", "issues/7"])
    def test_pull_urls_route_through_the_issues_api(self, tail):
        issue = gh_ok(fixture_text("github", "issue.json"))
        driver = driver_for({("api", "repos/acme/pipeline-kit/issues/7"): issue})
        url = f"https://github.com/acme/pipeline-kit/{tail}"
        assert isinstance(driver.fetch(make_unit(url, Kind.GITHUB)), Content)

    def test_the_new_issue_form_is_no_issue(self):
        # `issues/new` asked the API for issue "new", which 404'd into dead.
        url = "https://github.com/acme/pipeline-kit/issues/new"
        result = unusable_of(driver_for().fetch(make_unit(url, Kind.GITHUB)))
        assert result.evidence.startswith("github /issues/ link")


class TestBlob:
    URL = "https://github.com/acme/pipeline-kit/blob/main/src/detect.py"
    CONTENTS = ("api", "repos/acme/pipeline-kit/contents/src/detect.py?ref=main")

    def test_blob_fetches_through_the_authenticated_gh_seam_and_fences(self):
        # raw.githubusercontent.com is unauthenticated: it 404s for every
        # private-repo blob however the machine is signed in, and that 404
        # classified live content as dead. gh carries the auth.
        driver = driver_for({self.CONTENTS: gh_contents(b"def detect(): ...")})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert isinstance(result, Content)
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
        assert isinstance(driver.fetch(make_unit(self.URL, Kind.GITHUB)), Missing)

    def test_a_document_blob_redetects_to_file_work(self):
        # A real ledger PDF blob fenced 40k characters of
        # replacement-character soup and went done. It is not source — but
        # it IS extractable, now that the file driver reads blob bytes
        # through the same authenticated API.
        driver = driver_for({self.CONTENTS: gh_contents(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj")})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert result == Redetected(kind=Kind.FILE, format=Format.PDF)

    def test_an_unrecognized_binary_blob_parks_manual_never_fenced(self):
        driver = driver_for({self.CONTENTS: gh_contents(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert isinstance(result, Unusable)  # fenced output is unrepresentable on it
        assert "binary, not UTF-8 text" in result.evidence

    def test_utf8_source_with_non_ascii_still_fences(self):
        driver = driver_for({self.CONTENTS: gh_contents("# naïve — résumé\n".encode())})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert isinstance(result, Content)
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
        assert result == Redetected(kind=Kind.FILE, format=Format.PDF)  # identity only

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
        assert result == Redetected(kind=Kind.FILE, format=Format.CSV)

    @pytest.mark.parametrize("path", ["src/detect.py", "README.md", "Makefile", "docs/notes.txt"])
    def test_source_and_prose_extensions_still_fence(self, path):
        # The extension fallback only knows Format values: nothing a repo
        # actually holds as source or prose is diverted by naming the file.
        args = ("api", f"repos/acme/pipeline-kit/contents/{path}?ref=main")
        driver = driver_for({args: gh_contents(b"def detect(): ...")})
        url = f"https://github.com/acme/pipeline-kit/blob/main/{path}"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert isinstance(result, Content)
        assert body_of(result) == "```\ndef detect(): ...\n```"

    def test_a_file_over_the_cap_is_cut_at_a_line_end_and_says_so(self):
        # The field case: a 65,718-byte module stored as its first 40,000
        # characters, ending inside an f-string, with nothing to say so.
        source = lines(1_000, 70)
        driver = driver_for({self.CONTENTS: gh_contents(source.encode())})
        body = body_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        # 571 lines end at 39,970; the 572nd would cross the cap.
        assert body == f"```\n{source[:39_969]}\n```\n\n{truncated(39_969, 70_000, self.URL)}"

    def test_a_line_ending_on_the_cap_is_kept(self):
        source = "a\n" + "x" * 39_998 + "\ntail\n"
        driver = driver_for({self.CONTENTS: gh_contents(source.encode())})
        body = body_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        assert body == f"```\n{source[:40_000]}\n```\n\n{truncated(40_000, 40_006, self.URL)}"

    def test_a_line_ending_one_past_the_cap_is_left_out(self):
        source = lines(399, 100) + "y" * 101 + "\ntail\n"
        driver = driver_for({self.CONTENTS: gh_contents(source.encode())})
        body = body_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        assert body == f"```\n{source[:39_899]}\n```\n\n{truncated(39_899, 40_007, self.URL)}"

    def test_the_whole_length_is_counted_in_characters(self):
        # Bytes would overstate it: every é is two of them in UTF-8.
        source = "é" * 99 + "\n"
        driver = driver_for({self.CONTENTS: gh_contents((source * 500).encode())})
        body = body_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        assert body.endswith(truncated(39_999, 50_000, self.URL))

    @pytest.mark.parametrize("size", [40_000, 39_999])
    def test_a_file_at_or_under_the_cap_is_fenced_whole(self, size):
        source = lines(400, 100)[: size - 1] + "\n"
        driver = driver_for({self.CONTENTS: gh_contents(source.encode())})
        body = body_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        assert body == f"```\n{source}\n```"

    def test_a_line_longer_than_the_cap_is_cut_inside_itself(self):
        # Minified source: no line end to cut at, and nothing kept is worse.
        source = "x" * 50_000
        driver = driver_for({self.CONTENTS: gh_contents(source.encode())})
        body = body_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        assert body == f"```\n{'x' * 40_000}\n```\n\n{truncated(40_000, 50_000, self.URL)}"

    def test_a_line_that_would_cross_the_cap_is_left_out_however_long(self):
        source = "a\n" + "x" * 50_000
        driver = driver_for({self.CONTENTS: gh_contents(source.encode())})
        body = body_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        assert body == f"```\na\n```\n\n{truncated(1, 50_002, self.URL)}"

    def test_a_line_end_opening_the_file_is_no_place_to_cut(self):
        source = "\n" + "x" * 50_000
        driver = driver_for({self.CONTENTS: gh_contents(source.encode())})
        body = body_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        assert body == f"```\n{source[:40_000]}\n```\n\n{truncated(40_000, 50_001, self.URL)}"

    def test_oversize_blob_parks_manual_never_dead(self):
        # Over 1MB the contents API answers `encoding: "none"` with an empty
        # body — the file is there, just not inline.
        driver = driver_for(
            {self.CONTENTS: gh_ok(json.dumps({"encoding": "none", "content": "", "size": 4645520}))}
        )
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert isinstance(result, Refused)
        assert result.permanent  # the API will refuse it inline every time
        assert "larger than the contents API serves inline" in result.evidence


class TestDirectory:
    URL = "https://github.com/anthropics/claude-code/tree/main/mods"
    CONTENTS = ("api", "repos/anthropics/claude-code/contents/mods?ref=main")
    README = ("api", "repos/anthropics/claude-code/readme/mods?ref=main", *RAW)
    LISTING = gh_listing(("README.md", "file"), ("agents-md", "dir"))
    ENTRIES = "- `README.md`\n- `agents-md/`"

    def test_a_directory_is_its_readme_and_its_listing(self):
        # The field case: this link ledgered the repo's ROOT README as done,
        # and nothing said the linked directory never landed.
        driver = driver_for({self.CONTENTS: self.LISTING, self.README: gh_ok("# Mods\n")})
        result = content_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        assert result.meta == {"title": "anthropics/claude-code/mods"}
        assert body_of(result) == f"## README\n\n# Mods\n\n## Contents\n\n{self.ENTRIES}"

    def test_a_directory_without_a_readme_is_its_listing(self):
        driver = driver_for({self.CONTENTS: self.LISTING, self.README: NOT_FOUND})
        body = body_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        assert body == f"## README\n\n(no README)\n\n## Contents\n\n{self.ENTRIES}"

    def test_a_readme_the_api_refused_is_blocked_never_absent(self):
        driver = driver_for({self.CONTENTS: self.LISTING, self.README: RATE_LIMITED})
        result = driver.fetch(make_unit(self.URL, Kind.GITHUB))
        assert isinstance(result, Refused)
        assert not result.permanent

    def test_the_readme_is_capped_and_the_listing_kept_whole(self):
        readme = lines(1_000, 61)
        driver = driver_for({self.CONTENTS: self.LISTING, self.README: gh_ok(readme)})
        body = body_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        assert body == (
            f"## README\n\n{readme[:59_962]}\n\n{truncated(59_962, 61_000, self.URL)}"
            f"\n\n## Contents\n\n{self.ENTRIES}"
        )

    def test_a_blob_link_to_a_directory_reads_the_directory(self):
        # It blocked on the contents API's array answer, then went manual.
        url = "https://github.com/anthropics/claude-code/blob/main/mods"
        driver = driver_for({self.CONTENTS: self.LISTING, self.README: NOT_FOUND})
        assert body_of(driver.fetch(make_unit(url, Kind.GITHUB))).endswith(self.ENTRIES)

    def test_a_tree_link_to_a_file_is_the_file(self):
        driver = driver_for({self.CONTENTS: gh_contents(b"def mod(): ...")})
        result = content_of(driver.fetch(make_unit(self.URL, Kind.GITHUB)))
        assert result.meta == {"file": "mods"}

    def test_a_missing_directory_is_dead(self):
        refs = "repos/anthropics/claude-code/git/matching-refs"
        driver = driver_for(
            {
                self.CONTENTS: NOT_FOUND,
                ("api", f"{refs}/heads/main"): gh_matching_refs("refs/heads/main"),
                ("api", f"{refs}/tags/main"): gh_matching_refs(),
            }
        )
        assert isinstance(driver.fetch(make_unit(self.URL, Kind.GITHUB)), Missing)


class TestRepoAtRef:
    """A path link naming a ref alone is the repo, read at that ref."""

    ROOT = ("api", "repos/acme/pipeline-kit/contents/?ref=v2")
    README = ("api", "repos/acme/pipeline-kit/readme?ref=v2", *RAW)

    @pytest.mark.parametrize("view", ["tree", "blob"])
    def test_a_bare_ref_is_the_repo_with_that_refs_readme(self, view):
        driver = driver_for(
            {
                self.ROOT: gh_listing(("README.md", "file")),
                REPO_ARGS: gh_ok(fixture_text("github", "repo.json")),
                self.README: gh_ok("# pipeline-kit v2\n\n"),
            }
        )
        url = f"https://github.com/acme/pipeline-kit/{view}/v2"
        result = content_of(driver.fetch(make_unit(url, Kind.GITHUB)))
        assert result.meta["title"] == "acme/pipeline-kit"
        assert result.meta["ref"] == "v2"
        assert body_of(result) == "# pipeline-kit v2"

    def test_a_refs_readme_over_the_cap_says_where_the_rest_is(self):
        readme = lines(1_000, 61)
        driver = driver_for(
            {
                self.ROOT: gh_listing(("README.md", "file")),
                REPO_ARGS: gh_ok(fixture_text("github", "repo.json")),
                self.README: gh_ok(readme),
            }
        )
        url = "https://github.com/acme/pipeline-kit/tree/v2"
        body = body_of(driver.fetch(make_unit(url, Kind.GITHUB)))
        assert body.endswith(f"\n\n{truncated(59_962, 61_000, url)}")

    def test_a_ref_that_does_not_exist_is_dead(self):
        # Read without the root listing, a gone branch would be the repo's
        # metadata over "(no README)", ledgered done.
        refs = "repos/acme/pipeline-kit/git/matching-refs"
        driver = driver_for(
            {
                self.ROOT: NOT_FOUND,
                ("api", f"{refs}/heads/v2"): gh_matching_refs(),
                ("api", f"{refs}/tags/v2"): gh_matching_refs(),
            }
        )
        url = "https://github.com/acme/pipeline-kit/tree/v2"
        assert isinstance(driver.fetch(make_unit(url, Kind.GITHUB)), Missing)


class TestRaw:
    CONTENTS = ("api", "repos/acme/pipeline-kit/contents/src/detect.py?ref=main")

    def test_a_raw_link_is_the_file_it_serves(self):
        driver = driver_for({self.CONTENTS: gh_contents(b"def detect(): ...")})
        url = "https://github.com/acme/pipeline-kit/raw/main/src/detect.py"
        assert body_of(driver.fetch(make_unit(url, Kind.GITHUB))) == "```\ndef detect(): ...\n```"

    def test_a_raw_document_is_file_work(self):
        args = ("api", "repos/acme/pipeline-kit/contents/docs/spec.pdf?ref=main")
        driver = driver_for({args: gh_contents(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")})
        url = "https://github.com/acme/pipeline-kit/raw/main/docs/spec.pdf"
        result = driver.fetch(make_unit(url, Kind.GITHUB))
        assert result == Redetected(kind=Kind.FILE, format=Format.PDF)


class TestDownloads:
    """Release assets and source archives are files; only a document is file work."""

    @pytest.mark.parametrize(
        ("tail", "fmt"),
        [
            ("releases/download/v1.0/book.pdf", Format.PDF),
            ("releases/download/release/1.0/deck.PPTX", Format.PPTX),
            ("releases/latest/download/book.pdf", Format.PDF),
        ],
    )
    def test_a_released_document_is_file_work(self, tail, fmt):
        gh = FakeGh({})
        url = f"https://github.com/acme/pipeline-kit/{tail}"
        result = GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB))
        assert result == Redetected(kind=Kind.FILE, format=fmt)
        assert gh.calls == []

    @pytest.mark.parametrize(
        ("tail", "name"),
        [
            ("releases/download/v1.0/pipeline-kit.dmg", "pipeline-kit.dmg"),
            ("releases/download/v1.0/pipeline-kit", "pipeline-kit"),
            ("releases/latest/download/pipeline-kit.dmg", "pipeline-kit.dmg"),
            ("archive/refs/tags/v1.0.zip", "v1.0.zip"),
            ("archive/main.tar.gz", "main.tar.gz"),
        ],
    )
    def test_any_other_download_parks_naming_it_before_a_byte_moves(self, tail, name):
        url = f"https://github.com/acme/pipeline-kit/{tail}"
        result = unusable_of(driver_for().fetch(make_unit(url, Kind.GITHUB)))
        assert result.rescuable
        assert result.evidence == f"{name} is a download, not a document the file driver extracts"


UNROUTED = [
    ("commit/7fd1a60b01f91b314f59955a4e4d4e80d8edf11d", "commit"),
    ("commits/main", "commits"),
    ("compare/v1.0...v1.1", "compare"),
    ("blame/main/src/detect.py", "blame"),
    ("releases", "releases"),
    ("releases/tag/v1.0", "releases"),
    ("releases/download/v1.0", "releases"),
    ("releases/latest", "releases"),
    ("tags", "tags"),
    ("discussions/12", "discussions"),
    ("wiki", "wiki"),
    ("wiki/Design", "wiki"),
    ("actions/runs/42", "actions"),
    ("pulls", "pulls"),
    ("issues", "issues"),
    ("projects", "projects"),
    ("security/advisories/GHSA-xxxx-yyyy-zzzz", "security"),
    ("tree", "tree"),
    ("archive", "archive"),
]


class TestUnrouted:
    """A shape no route reads parks for judgment; it never collapses to the root README."""

    @pytest.mark.parametrize(("tail", "shape"), UNROUTED)
    def test_it_parks_naming_the_shape_without_a_call(self, tail, shape):
        gh = FakeGh({})
        url = f"https://github.com/acme/pipeline-kit/{tail}"
        result = unusable_of(GitHubDriver(gh=gh).fetch(make_unit(url, Kind.GITHUB)))
        assert result.rescuable
        assert result.evidence == (
            f"github /{shape}/ link — the driver reads repos, directories, files and issues, "
            "and has no route for this one"
        )
        assert gh.calls == []


class TestEdges:
    def test_github_root_is_unusable_with_nothing_to_rescue(self):
        result = driver_for().fetch(make_unit("https://github.com", Kind.GITHUB))
        assert isinstance(result, Unusable)
        assert not result.rescuable
        assert "root" in result.evidence

    def test_unparseable_api_json_is_blocked(self):
        driver = driver_for({("api", "repos/acme/pipeline-kit"): gh_ok("<html>oops</html>")})
        result = driver.fetch(make_unit("https://github.com/acme/pipeline-kit", Kind.GITHUB))
        assert isinstance(result, Refused)
        assert not result.permanent
        assert "unparseable JSON" in result.evidence
