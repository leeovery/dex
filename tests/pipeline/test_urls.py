"""Tests for pipeline/urls.py: canonicalization primitives and work hashes."""

import urllib.parse

from hypothesis import given
from hypothesis import strategies as st

from dex_engine.pipeline.registry import DRIVERS
from dex_engine.pipeline.urls import base_canonical, ext_of, host_of, resolve_repo_path, work_hash


class TestHostOf:
    def test_lowercases_and_strips_www_and_m(self):
        assert host_of("https://WWW.Example.COM/x") == "example.com"
        assert host_of("https://m.youtube.com/watch?v=a") == "youtube.com"
        assert host_of("http://www.m.example.com/") == "example.com"

    def test_port_is_kept(self):
        assert host_of("http://example.com:8080/x") == "example.com:8080"


class TestBaseCanonical:
    def test_forces_https_and_strips_noise(self):
        url = "http://www.example.com/post/?utm_source=x&a=b&si=abc#frag"
        assert base_canonical(url) == "https://example.com/post?a=b"

    def test_keep_params_filters_to_the_allowlist(self):
        url = "https://youtube.com/watch?v=abc&t=120&pp=xyz"
        assert base_canonical(url, keep_params=frozenset({"v"})) == (
            "https://youtube.com/watch?v=abc"
        )

    def test_trailing_slashes_and_fragments_drop(self):
        assert base_canonical("https://example.com/a///") == "https://example.com/a"
        assert base_canonical("https://example.com/#top") == "https://example.com"

    def test_matches_the_pre_rewrite_hash_key(self):
        # Ledger continuity: this exact pair held under the old enricher.
        url = "https://www.example.com/blog/post?ref=twitter"
        assert base_canonical(url) == "https://example.com/blog/post"


def _build_host(prefixes, labels):
    return "".join(prefixes) + ".".join(labels)


def _build_url(scheme, host, segments, params, fragment):
    path = "/" + "/".join(segments) if segments else ""
    query = urllib.parse.urlencode(params)
    return urllib.parse.urlunsplit((scheme, host, path, query, fragment))


# URL strategy: hosts with optional www./m. prefixes (plus the real hosts
# whose drivers canonicalize specially), path segments, query params mixing
# kept and stripped keys, an optional fragment.
_label = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=8)
_host = st.one_of(
    st.builds(
        _build_host,
        st.lists(st.sampled_from(["www.", "m."]), max_size=2),
        st.lists(_label, min_size=1, max_size=3),
    ),
    st.sampled_from(
        ["youtu.be", "youtube.com", "m.youtube.com", "x.com", "github.com", "arxiv.org"]
    ),
)
_param = st.tuples(
    st.sampled_from(["a", "b", "v", "utm_source", "si", "ref", "q x"]),
    st.text(alphabet="abc %+&=/", max_size=6),
)
_url = st.builds(
    _build_url,
    st.sampled_from(["http", "https"]),
    _host,
    st.lists(_label, max_size=3),
    st.lists(_param, max_size=3),
    st.sampled_from(["", "top"]),
)


class TestCanonicalIdempotency:
    @given(_url)
    def test_base_canonical_is_idempotent(self, url):
        once = base_canonical(url)
        assert base_canonical(once) == once

    @given(_url)
    def test_keep_params_canonical_is_idempotent(self, url):
        keep = frozenset({"v"})
        once = base_canonical(url, keep_params=keep)
        assert base_canonical(once, keep_params=keep) == once

    @given(_url)
    def test_every_registered_driver_canonical_is_idempotent(self, url):
        # Canonicalization keys the ledger hash — a non-idempotent case IS a
        # duplicate-entry bug.
        for driver in DRIVERS:
            once = driver.canonical(url)
            assert driver.canonical(once) == once


class TestExtOf:
    def test_the_path_tail_names_the_extension(self):
        assert ext_of("https://cdn.test/a/episode.MP3?sig=1", default="mp3") == "mp3"
        assert ext_of("https://cdn.test/img.jpeg", default="jpg") == "jpeg"

    def test_no_usable_extension_takes_the_callers_default(self):
        # The default is the caller's, per media family: images fall back
        # to jpg, audio enclosures to mp3.
        assert ext_of("https://cdn.test/stream", default="mp3") == "mp3"
        assert ext_of("https://cdn.test/file.a-very-long-tail", default="jpg") == "jpg"
        assert ext_of("https://cdn.test/file.p%20g", default="jpg") == "jpg"


class TestWorkHash:
    def test_ten_lowercase_hex(self):
        digest = work_hash("https://example.com/a")
        assert len(digest) == 10
        assert digest == digest.lower()
        int(digest, 16)  # parses as hex

    def test_matches_the_pre_rewrite_ledger_keys(self):
        # sha1("https://example.com")[:10] — pin the exact algorithm so a
        # refactor cannot silently orphan every existing ledger entry.
        assert work_hash("https://example.com") == "327c3fda87"

    def test_file_keys_hash_too(self):
        assert len(work_hash("file:media/2026-08-19-x-55ad7b/doc.pdf")) == 10


class TestResolveRepoPath:
    def test_contained_path_resolves(self, tmp_path):
        (tmp_path / "media").mkdir()
        assert resolve_repo_path(tmp_path, "media/doc.pdf") == tmp_path / "media" / "doc.pdf"

    def test_escaping_path_is_none(self, tmp_path):
        assert resolve_repo_path(tmp_path, "../outside.pdf") is None

    def test_embedded_nul_byte_is_none_never_a_raise(self, tmp_path):
        # Repo paths are owner-editable data; a NUL byte cannot be a path
        # and must park like any other bad seed, not abort the caller.
        assert resolve_repo_path(tmp_path, "media/bad\x00name.pdf") is None
