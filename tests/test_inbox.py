"""Tests for inbox.py's §14 detection hook (the rest of inbox.py is phase-5 work)."""

from dex_engine.inbox import _note_document_format
from tests.capabilities.conftest import fixture_bytes


class TestNoteDocumentFormat:
    def test_document_capture_is_noted(self, tmp_path, capsys):
        dest = tmp_path / "report.docx"
        dest.write_bytes(fixture_bytes("report.docx"))
        _note_document_format("media/abc123/report.docx", dest)
        out = capsys.readouterr().out
        assert "docx document" in out
        assert "queues for extraction" in out

    def test_non_document_is_silent(self, tmp_path, capsys):
        dest = tmp_path / "photo.jpg"
        dest.write_bytes(b"\xff\xd8\xff\xe0 jpeg")
        _note_document_format("media/abc123/photo.jpg", dest)
        assert capsys.readouterr().out == ""

    def test_unreadable_file_is_skipped_never_fatal(self, tmp_path, capsys):
        # The one expected failure: the file cannot be read. Anything else
        # is an engine bug and must stay loud — the hook has no broad except.
        _note_document_format("media/abc123/gone.pdf", tmp_path / "gone.pdf")
        assert capsys.readouterr().out == ""
