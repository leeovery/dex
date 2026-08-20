"""Tests for pipeline/detect.py: ordered patterns, HEAD sniff, byte-signature sniff."""

import pytest

from dex_engine.pipeline.detect import (
    CONTENT_TYPE_FORMATS,
    canonical_url,
    detect,
    detect_kind,
    sniff_format,
)
from dex_engine.pipeline.registry import DRIVERS
from dex_engine.pipeline.types import Format, Kind
from tests.capabilities.conftest import fixture_bytes


class RecordingSniff:
    def __init__(self, media_type: str | None) -> None:
        self.media_type = media_type
        self.calls: list[str] = []

    def __call__(self, url: str) -> str | None:
        self.calls.append(url)
        return self.media_type


class TestPatternDetection:
    @pytest.mark.parametrize(
        ("url", "kind"),
        [
            ("https://youtube.com/watch?v=a", Kind.YOUTUBE),
            ("https://youtu.be/a", Kind.YOUTUBE),
            ("https://m.youtube.com/watch?v=a", Kind.YOUTUBE),
            ("https://x.com/user/status/1", Kind.X),
            ("https://twitter.com/user/status/1", Kind.X),
            ("https://github.com/acme/repo", Kind.GITHUB),
            ("https://gist.github.com/user/abc", Kind.GITHUB),
            ("https://arxiv.org/abs/2408.12345", Kind.PAPER),
            ("https://openreview.net/forum?id=a", Kind.PAPER),
            ("https://huggingface.co/papers/2408.12345", Kind.PAPER),
            ("https://example.test/blog/post", Kind.WEB),
            ("https://huggingface.co/models/acme/thing", Kind.WEB),
        ],
    )
    def test_first_matching_driver_wins(self, url, kind):
        assert detect_kind(url, DRIVERS) is kind

    def test_m_youtube_divergence_is_dead(self):
        # The historical kind_of duplication disagreed on m.youtube.com —
        # one shared module means one answer, everywhere (§2).
        assert detect_kind("https://m.youtube.com/watch?v=a", DRIVERS) is Kind.YOUTUBE
        assert detect(("https://m.youtube.com/watch?v=a"), DRIVERS).kind is Kind.YOUTUBE

    def test_detect_kind_never_sniffs(self):
        # detect_kind is the offline path (normalize) — patterns only.
        assert detect_kind("https://example.test/whitepaper.pdf", DRIVERS) is Kind.WEB


class TestHeadSniff:
    def test_specialized_matches_are_conclusive_no_head_spent(self):
        sniff = RecordingSniff("application/pdf")
        detection = detect("https://github.com/acme/repo", DRIVERS, sniff=sniff)
        assert detection.kind is Kind.GITHUB
        assert sniff.calls == []

    def test_catch_all_urls_are_inconclusive_and_sniffed(self):
        sniff = RecordingSniff("application/pdf")
        detection = detect("https://example.test/whitepaper", DRIVERS, sniff=sniff)
        assert sniff.calls == ["https://example.test/whitepaper"]
        assert detection.kind is Kind.FILE
        assert detection.format is Format.PDF

    def test_html_content_type_stays_web(self):
        sniff = RecordingSniff("text/html; charset=utf-8")
        detection = detect("https://example.test/post", DRIVERS, sniff=sniff)
        assert detection.kind is Kind.WEB
        assert detection.format is None

    def test_failed_sniff_is_inconclusive_never_classified(self):
        # The GET that follows surfaces the real status via the classifier.
        detection = detect("https://example.test/post", DRIVERS, sniff=RecordingSniff(None))
        assert detection.kind is Kind.WEB

    def test_no_sniff_seam_means_pattern_only(self):
        assert detect("https://example.test/whitepaper.pdf", DRIVERS).kind is Kind.WEB

    @pytest.mark.parametrize(
        ("media_type", "fmt"),
        [
            ("application/pdf", Format.PDF),
            (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                Format.DOCX,
            ),
            ("text/csv", Format.CSV),
            ("application/epub+zip", Format.EPUB),
        ],
    )
    def test_content_type_format_mapping(self, media_type, fmt):
        detection = detect("https://example.test/doc", DRIVERS, sniff=RecordingSniff(media_type))
        assert detection.kind is Kind.FILE
        assert detection.format is fmt

    def test_every_format_is_reachable_from_some_media_type(self):
        assert set(CONTENT_TYPE_FORMATS.values()) == set(Format)


class TestCanonicalDelegation:
    def test_delegates_to_the_matched_driver(self):
        # youtu.be short links only rewrite when the YOUTUBE driver owns them.
        assert canonical_url("https://youtu.be/abc?si=x", DRIVERS) == (
            "https://youtube.com/watch?v=abc"
        )
        assert canonical_url("http://www.example.test/a/?ref=x", DRIVERS) == (
            "https://example.test/a"
        )


class TestFileKeys:
    def test_local_file_keys_are_file_work_by_construction(self):
        detection = detect("file:media/2026-08-19-x-55ad7b/doc.pdf", DRIVERS)
        assert detection.kind is Kind.FILE
        assert detection.format is None  # bytes decide the format, not the key


class TestSniffFormat:
    """Byte-signature detection (§1) — real fixture bytes, anydoc-backed."""

    @pytest.mark.parametrize(
        ("fixture", "fmt"),
        [
            ("report.docx", Format.DOCX),
            ("paper.pdf", Format.PDF),
            ("scanned.pdf", Format.PDF),
        ],
    )
    def test_fixture_signatures(self, fixture, fmt):
        assert sniff_format(fixture_bytes(fixture)) is fmt

    def test_csv_needs_the_extension_no_signature_exists(self):
        data = fixture_bytes("stars.csv")
        assert sniff_format(data) is None
        assert sniff_format(data, name="stars.csv") is Format.CSV

    def test_magic_numbers_without_anydoc(self):
        # The fallback signatures hold on their own (a platform whose anydoc
        # wheel is broken still detects the obvious ones).
        assert sniff_format(b"%PDF-1.7 ...") is Format.PDF
        assert sniff_format(b"{\\rtf1\\ansi hello}") is Format.RTF

    def test_images_and_unknowns_are_not_file_work(self):
        assert sniff_format(b"\x89PNG\r\n\x1a\n....") is None
        assert sniff_format(b"plain text note", name="note.txt") is None
        assert sniff_format(b"") is None

    def test_lfs_pointer_falls_back_to_the_extension(self):
        # An unsmudged LFS pointer is text bytes with an honest filename —
        # the extension keeps it seedable so the driver can park it loudly.
        pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 12\n"
        assert sniff_format(pointer, name="deck.pptx") is Format.PPTX

    def test_truncated_zip_container_is_unrecognized(self):
        assert sniff_format(b"PK\x03\x04 truncated") is None
