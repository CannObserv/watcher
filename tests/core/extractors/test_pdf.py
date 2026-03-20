"""Tests for PDF content extractor."""

import io

import pytest
from pypdf import PdfWriter

from src.core.extractors.pdf import PdfExtractor


def _make_pdf_bytes(num_pages: int = 2) -> bytes:
    """Create a minimal PDF with blank pages for testing structure."""
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestPdfExtractor:
    def setup_method(self):
        self.extractor = PdfExtractor()

    def test_extracts_one_chunk_per_page(self):
        pdf_bytes = _make_pdf_bytes(3)
        result = self.extractor.extract(pdf_bytes)
        assert len(result.chunks) == 3
        for i, chunk in enumerate(result.chunks):
            assert chunk.index == i
            assert chunk.chunk_type == "page"
            assert chunk.label == f"Page {i + 1}"

    def test_empty_pages_excluded(self):
        pdf_bytes = _make_pdf_bytes(2)
        result = self.extractor.extract(pdf_bytes, config={"skip_empty_pages": True})
        assert len(result.chunks) == 0

    def test_chunk_type_is_page(self):
        pdf_bytes = _make_pdf_bytes(1)
        result = self.extractor.extract(pdf_bytes)
        assert result.chunks[0].chunk_type == "page"

    def test_invalid_pdf_raises(self):
        with pytest.raises(ValueError, match="Failed to parse PDF"):
            self.extractor.extract(b"not a pdf")
