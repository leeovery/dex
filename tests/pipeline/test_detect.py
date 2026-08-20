"""Tests for pipeline/detect.py: ordered patterns, HEAD sniff, the file seam."""

import pytest

from dex_engine.pipeline.detect import CONTENT_TYPE_FORMATS, detect, detect_kind
from dex_engine.pipeline.registry import DRIVERS
from dex_engine.pipeline.types import Format, Kind


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


class TestFileSeam:
    def test_local_file_keys_raise_until_phase_3(self):
        with pytest.raises(NotImplementedError, match="phase 3"):
            detect("file:media/2026-08-19-x-55ad7b/doc.pdf", DRIVERS)
