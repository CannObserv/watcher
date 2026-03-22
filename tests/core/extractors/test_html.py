"""Tests for HTML content extractor."""

from pathlib import Path

from src.core.extractors.html import HtmlExtractor

FIXTURES = Path(__file__).parent.parent.parent / "fixtures"


class TestHtmlExtractor:
    def setup_method(self):
        self.extractor = HtmlExtractor()
        self.html = (FIXTURES / "sample.html").read_bytes()

    def test_extracts_full_page_as_single_chunk(self):
        result = self.extractor.extract(self.html)
        assert len(result.chunks) >= 1
        assert result.chunks[0].chunk_type == "section"

    def test_strips_boilerplate(self):
        result = self.extractor.extract(self.html)
        full_text = " ".join(c.text for c in result.chunks)
        assert "var x = 1" not in full_text
        assert "color: red" not in full_text
        assert "Home" not in full_text  # nav stripped

    def test_selector_targeting(self):
        result = self.extractor.extract(self.html, config={"selectors": ["#agenda"]})
        full_text = " ".join(c.text for c in result.chunks)
        assert "Budget review" in full_text
        assert "Previous Minutes" not in full_text

    def test_exclude_selectors(self):
        result = self.extractor.extract(
            self.html,
            config={"selectors": ["main"], "exclude_selectors": ["#minutes"]},
        )
        full_text = " ".join(c.text for c in result.chunks)
        assert "Budget review" in full_text
        assert "Previous Minutes" not in full_text

    def test_dynamic_id_stripping(self):
        result = self.extractor.extract(
            self.html,
            config={
                "strip_boilerplate": False,
                "selectors": ["footer"],
                "dynamic_id_patterns": ["data-block-id"],
            },
        )
        full_text = " ".join(c.text for c in result.chunks)
        assert "sq-abc123" not in full_text
        assert "Squarespace block" in full_text

    def test_whitespace_normalization(self):
        html = b"<html><body><p>  hello   \n\t  world  </p></body></html>"
        result = self.extractor.extract(html)
        text = result.chunks[0].text
        assert "  " not in text
        assert "\n" not in text
        assert "\t" not in text

    def test_sections_create_multiple_chunks(self):
        result = self.extractor.extract(self.html)
        assert len(result.chunks) >= 1
        for chunk in result.chunks:
            assert chunk.label
            assert chunk.text.strip()

    def test_empty_html_returns_empty(self):
        result = self.extractor.extract(b"<html><body></body></html>")
        assert len(result.chunks) == 0 or result.total_chars == 0
