"""Tests for inbox.py: the device-tested materialization sequence, hermetic."""

import hashlib

import pytest

from dex_engine.inbox import GithubSeams, build_parser, ensure, parse_capture, reconcile
from tests.capabilities.conftest import fixture_bytes

REPO = "owner/instance"
ASSET_URL = "https://api.github.com/repos/owner/instance/releases/assets/123"
NAME = "20260818-101530.jpg"
DATA = b"\xff\xd8\xff\xe0 jpeg bytes"
ITEM_ID = hashlib.sha1(f"media/{NAME}".encode()).hexdigest()[:6]  # noqa: S324 — content key, mirrors inbox
REL = f"media/{ITEM_ID}/{NAME}"

RELEASE_URL = f"/repos/{REPO}/releases/tags/inbox"


class FakeGithub:
    """Scriptable API + download + git seams, with call recording."""

    def __init__(self, *, asset_status=200, release_status=200, assets=None, lfs_ok=True):
        self.asset_status = asset_status
        self.release_status = release_status
        self.assets = assets or []
        self.lfs_ok = lfs_ok
        self.deleted: list[str] = []
        self.api_calls: list[tuple[str, str]] = []
        self.git_calls: list[list[str]] = []
        # Every seam call in one interleaved log, so ordering across seams
        # (git-add BEFORE asset delete) is assertable.
        self.ops: list[tuple] = []
        self.lines: list[str] = []
        self.downloads = 0
        self.download_data = DATA

    # -- seams ------------------------------------------------------------

    def api(self, url, token, method="GET", data=None):  # noqa: ARG002 — the fake ignores POST bodies
        assert token, "api must never be called without auth"
        self.api_calls.append((method, url))
        self.ops.append(("api", method, url))
        if method == "DELETE":
            self.deleted.append(url)
            asset_id = int(url.rsplit("/", 1)[-1])
            self.assets = [a for a in self.assets if a.get("id") != asset_id]
            return 204, None
        if url == ASSET_URL:
            if self.asset_status != 200:
                return self.asset_status, None
            return 200, {"name": NAME, "size": len(self.download_data)}
        if url == RELEASE_URL:
            if self.release_status != 200:
                return self.release_status, None
            return 200, {"id": 9, "assets": self.assets}
        if url == f"/repos/{REPO}/releases" and method == "POST":
            return 201, {"id": 9}
        raise AssertionError(f"unexpected api call {method} {url}")

    def download(self, url, token):
        assert token
        assert url == ASSET_URL
        self.downloads += 1
        return self.download_data

    def git(self, args):
        self.git_calls.append(list(args))
        self.ops.append(("git", *args))
        if args[0] == "remote":
            return 0, f"git@github.com:{REPO}.git\n"
        if args[0] == "check-attr":
            return 0, f"{args[-1]}: filter: {'lfs' if self.lfs_ok else 'unspecified'}\n"
        if args[0] == "add":
            return 0, ""
        if args[0] == "cat-file":
            return (0, "version https://git-lfs.github.com/spec/v1\noid sha256:...\n")
        raise AssertionError(f"unexpected git call {args}")

    def seams(self, token="tok") -> GithubSeams:  # noqa: S107 — a fake token, not a credential
        return GithubSeams(
            api=self.api,
            download=self.download,
            git=self.git,
            token=lambda: token,
            echo=self.lines.append,
        )

    @property
    def out(self) -> str:
        return "\n".join(self.lines)


def write_capture(instance, name="20260818-101530.md", *, asset=ASSET_URL, note="the note"):
    fm = f"---\nasset: {asset}\nname: {NAME}\n---\n\n" if asset else ""
    path = instance.root / "inbox" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(fm + note + "\n")
    return path


class TestParseCapture:
    def test_plain_body(self):
        fm, body = parse_capture("https://example.test/a\n\nwhy I saved it\n")
        assert fm == {}
        assert "why I saved it" in body

    def test_frontmatter_and_body(self):
        fm, body = parse_capture(f"---\nasset: {ASSET_URL}\nname: {NAME}\n---\n\nnote\n")
        assert fm == {"asset": ASSET_URL, "name": NAME}
        assert body == "note\n"

    def test_bom_and_crlf_tolerated(self):
        fm, body = parse_capture(f"﻿---\r\nasset: {ASSET_URL}\r\n---\r\nnote\r\n")
        assert fm == {"asset": ASSET_URL}
        assert body == "note\n"

    def test_unterminated_frontmatter_is_all_body(self):
        fm, body = parse_capture("---\nasset: x\nno closing fence\n")
        assert fm == {}
        assert body.startswith("---")


class TestMaterialize:
    def test_full_sequence_in_order(self, instance):
        capture = write_capture(instance)
        gh = FakeGithub()
        code = reconcile(instance, gh.seams())
        assert code == 0
        # The binary landed at the derived path.
        assert (instance.root / REL).read_bytes() == DATA
        # LFS verification ran BEFORE the delete (order is load-bearing: a
        # file that failed to stage must keep its only remote copy).
        add_at = gh.ops.index(("git", "add", "--", REL))
        delete_at = gh.ops.index(("api", "DELETE", ASSET_URL))
        assert add_at < delete_at
        assert gh.deleted == [ASSET_URL]
        # The pointer was rewritten to media:.
        fm, body = parse_capture(capture.read_text())
        assert fm == {"media": REL}
        assert body == "the note\n"
        assert f"ok {capture.name} -> {REL}" in gh.out
        assert "1/1 staged asset(s) materialized" in gh.out
        assert "commit and push" in gh.out
        assert "only copy" in gh.out

    def test_document_capture_notes_extraction_routing(self, instance):
        gh = FakeGithub()
        gh.download_data = fixture_bytes("report.docx")
        write_capture(instance)
        reconcile(instance, gh.seams())
        assert "document — queues for extraction" in gh.out

    def test_image_capture_has_no_extraction_note(self, instance):
        gh = FakeGithub()
        write_capture(instance)
        reconcile(instance, gh.seams())
        assert "queues for extraction" not in gh.out

    def test_malformed_asset_url_fails_loud(self, instance):
        write_capture(instance, asset="https://evil.example/assets/1")
        gh = FakeGithub()
        assert reconcile(instance, gh.seams()) == 1
        assert "malformed asset URL" in gh.out
        assert "may be lost" in gh.out

    def test_gone_asset_with_local_file_updates_pointer(self, instance):
        capture = write_capture(instance)
        dest = instance.root / REL
        dest.parent.mkdir(parents=True)
        dest.write_bytes(DATA)
        gh = FakeGithub(asset_status=404)
        assert reconcile(instance, gh.seams()) == 0
        assert "asset already ingested; pointer updated" in gh.out
        assert parse_capture(capture.read_text())[0] == {"media": REL}

    def test_gone_asset_without_local_file_says_pull_first(self, instance):
        write_capture(instance)
        gh = FakeGithub(asset_status=404)
        assert reconcile(instance, gh.seams()) == 1
        assert "git pull first" in gh.out

    def test_lookup_failure_fails_that_capture(self, instance):
        write_capture(instance)
        gh = FakeGithub(asset_status=500)
        assert reconcile(instance, gh.seams()) == 1
        assert "asset lookup returned 500" in gh.out

    def test_size_mismatch_fails_and_deletes_nothing(self, instance):
        write_capture(instance)
        gh = FakeGithub()

        def short_download(url, token):  # noqa: ARG001 — scripted result
            return DATA[:3]

        seams = GithubSeams(
            api=gh.api, download=short_download, git=gh.git,
            token=lambda: "tok", echo=gh.lines.append,
        )
        assert reconcile(instance, seams) == 1
        assert "expected" in gh.out
        assert gh.deleted == []

    def test_lfs_failure_keeps_the_asset(self, instance):
        write_capture(instance)
        gh = FakeGithub(lfs_ok=False)
        assert reconcile(instance, gh.seams()) == 1
        assert "did not stage as an LFS pointer" in gh.out
        assert "Asset NOT deleted" in gh.out
        assert gh.deleted == []

    def test_existing_file_of_right_size_skips_the_download(self, instance):
        write_capture(instance)
        dest = instance.root / REL
        dest.parent.mkdir(parents=True)
        dest.write_bytes(DATA)
        gh = FakeGithub()
        assert reconcile(instance, gh.seams()) == 0
        assert gh.downloads == 0

    def test_one_bad_capture_never_aborts_the_loop(self, instance):
        write_capture(instance, "20260818-000001.md", asset="https://evil.example/a/1")
        write_capture(instance, "20260818-000002.md")
        gh = FakeGithub()
        assert reconcile(instance, gh.seams()) == 1
        assert "1/2 staged asset(s) materialized" in gh.out


class TestReconcileModes:
    def test_no_inbox_dir_is_a_notice(self, instance):
        gh = FakeGithub()
        assert reconcile(instance, gh.seams()) == 0
        assert "no inbox/ here" in gh.out

    def test_text_captures_pass_through_untouched(self, instance):
        capture = write_capture(instance, asset=None, note="https://example.test/a\n\nnote")
        before = capture.read_text()
        gh = FakeGithub()
        assert reconcile(instance, gh.seams()) == 0
        assert capture.read_text() == before
        assert "1 capture(s), none with staged assets" in gh.out
        assert "commit and push" not in gh.out

    def test_staged_without_auth_exits_loud(self, instance):
        write_capture(instance)
        gh = FakeGithub()
        assert reconcile(instance, gh.seams(token=None)) == 1
        assert "staged assets need GitHub auth" in gh.out

    def test_no_auth_notes_skipped_checks(self, instance):
        write_capture(instance, asset=None)
        gh = FakeGithub()
        assert reconcile(instance, gh.seams(token=None)) == 0
        assert "no GitHub auth here" in gh.out
        assert "skipped, not passed" in gh.out

    def test_missing_origin_remote_is_named_not_blamed_on_auth(self, instance):
        # Auth is fine; the repo has no origin remote — the note must send
        # the owner to `git remote add origin`, not `gh auth login`.
        write_capture(instance, asset=None)
        gh = FakeGithub()

        def no_remote(args):
            if args[0] == "remote":
                return 1, ""
            return gh.git(args)

        seams = GithubSeams(
            api=gh.api, download=gh.download, git=no_remote,
            token=lambda: "tok", echo=gh.lines.append,
        )
        assert reconcile(instance, seams) == 0
        assert "no origin remote" in gh.out
        assert "no GitHub auth" not in gh.out
        assert "skipped, not passed" in gh.out

    def test_missing_release_self_heals(self, instance):
        write_capture(instance, asset=None)
        gh = FakeGithub(release_status=404)
        assert reconcile(instance, gh.seams()) == 0
        assert "created inbox release" in gh.out

    def test_orphaned_assets_reported(self, instance):
        write_capture(instance)  # references asset 123
        gh = FakeGithub(
            assets=[
                {"id": 123, "name": NAME, "size": 3, "url": ASSET_URL},
                {"id": 999, "name": "stray.png", "size": 7,
                 "url": "https://api.github.com/repos/owner/instance/releases/assets/999"},
            ]
        )
        reconcile(instance, gh.seams())
        assert "1 asset(s) on the inbox release have no capture pointer" in gh.out
        assert "stray.png" in gh.out


class TestEnsure:
    def test_ensure_without_auth_fails_loud(self, instance):
        gh = FakeGithub()
        assert ensure(instance, gh.seams(token=None)) == 1
        assert "need GitHub auth" in gh.out

    def test_ensure_present_release(self, instance):
        gh = FakeGithub()
        assert ensure(instance, gh.seams()) == 0
        assert "inbox release present" in gh.out

    def test_ensure_creates_missing_release(self, instance):
        gh = FakeGithub(release_status=404)
        assert ensure(instance, gh.seams()) == 0
        assert "created inbox release" in gh.out

    def test_ensure_surfaces_lookup_failures(self, instance):
        gh = FakeGithub(release_status=500)
        assert ensure(instance, gh.seams()) == 1
        assert "release lookup" in gh.out


class TestParser:
    def test_bare_and_ensure(self):
        assert build_parser().parse_args([]).command is None
        assert build_parser().parse_args(["ensure"]).command == "ensure"

    def test_unknown_command_is_loud(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["reconcile-harder"])


class TestCaptureShape:
    def test_capture_protocol_shape_round_trips(self, instance):
        # docs/capture.md's binary-capture shape, byte-for-byte as the
        # shortcut writes it — the frontmatter rewrite must preserve the note.
        raw = f"---\nasset: {ASSET_URL}\nname: {NAME}\n---\n\nwhy this caught my eye\n"
        path = instance.root / "inbox" / "20260818-101530.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(raw)
        gh = FakeGithub()
        reconcile(instance, gh.seams())
        fm, body = parse_capture(path.read_text())
        assert fm == {"media": REL}
        assert body == "why this caught my eye\n"

    def test_extra_frontmatter_keys_survive_the_rewrite(self, instance):
        raw = f"---\nasset: {ASSET_URL}\nname: {NAME}\nfrom: phone\n---\n\nnote\n"
        path = instance.root / "inbox" / "20260818-101530.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(raw)
        gh = FakeGithub()
        reconcile(instance, gh.seams())
        fm, _ = parse_capture(path.read_text())
        assert fm == {"media": REL, "from": "phone"}
