"""PDF content extractor — per-page text extraction."""

import io
import re

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from src.core.extractors.base import Chunk, ExtractionResult


class PdfExtractor:
    """Extract normalized text chunks from PDF content, one chunk per page."""

    def extract(self, raw: bytes, config: dict | None = None) -> ExtractionResult:
        """Extract text from each PDF page.

        Config keys:
            skip_empty_pages: bool — omit pages with no text (default: False)
        """
        config = config or {}
        skip_empty = config.get("skip_empty_pages", False)

        try:
            reader = PdfReader(io.BytesIO(raw))
        except PdfReadError as exc:
            raise ValueError(f"Failed to parse PDF: {exc}") from exc

        chunks = []
        chunk_index = 0
        for page_num, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            text = self._normalize_text(text)

            if skip_empty and not text.strip():
                continue

            chunks.append(
                Chunk(
                    index=chunk_index,
                    chunk_type="page",
                    label=f"Page {page_num + 1}",
                    text=text,
                )
            )
            chunk_index += 1

        return ExtractionResult(chunks=chunks)

    def _normalize_text(self, text: str) -> str:
        """Normalize whitespace in extracted text."""
        return re.sub(r"\s+", " ", text).strip()
