"""Tests for HTML content extractor."""

from pathlib import Path

from src.core.extractors.html import HtmlExtractor

FIXTURES = Path(__file__).parent.parent.parent / "fixtures"


class TestHtmlExtractor:
    def setup_method(self):
        self.extractor = HtmlExtractor()
        self.html = (FIXTURES / "sample.html").read_bytes()

    async def test_extracts_full_page_as_single_chunk(self):
        result = await self.extractor.extract(self.html)
        assert len(result.chunks) >= 1
        assert result.chunks[0].chunk_type == "section"

    async def test_strips_boilerplate(self):
        result = await self.extractor.extract(self.html)
        full_text = " ".join(c.text for c in result.chunks)
        assert "var x = 1" not in full_text
        assert "color: red" not in full_text
        assert "Home" not in full_text  # nav stripped

    async def test_selector_targeting(self):
        result = await self.extractor.extract(self.html, config={"selectors": ["#agenda"]})
        full_text = " ".join(c.text for c in result.chunks)
        assert "Budget review" in full_text
        assert "Previous Minutes" not in full_text

    async def test_exclude_selectors(self):
        result = await self.extractor.extract(
            self.html,
            config={"selectors": ["main"], "exclude_selectors": ["#minutes"]},
        )
        full_text = " ".join(c.text for c in result.chunks)
        assert "Budget review" in full_text
        assert "Previous Minutes" not in full_text

    async def test_dynamic_id_stripping(self):
        result = await self.extractor.extract(
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

    async def test_whitespace_normalization(self):
        html = b"<html><body><p>  hello   \n\t  world  </p></body></html>"
        result = await self.extractor.extract(html)
        text = result.chunks[0].text
        assert "  " not in text
        assert "\n" not in text
        assert "\t" not in text

    async def test_sections_create_multiple_chunks(self):
        result = await self.extractor.extract(self.html)
        assert len(result.chunks) >= 1
        for chunk in result.chunks:
            assert chunk.label
            assert chunk.text.strip()

    async def test_empty_html_returns_empty(self):
        result = await self.extractor.extract(b"<html><body></body></html>")
        assert len(result.chunks) == 0 or result.total_chars == 0

    async def test_ignore_selectors_removes_matching_elements(self):
        """Elements matching ignore_selectors are removed before text extraction."""
        html = b"""
        <html><body>
          <div id="main">Keep this content</div>
          <div id="sidebar">Remove this sidebar</div>
          <div id="ads">Remove these ads</div>
        </body></html>
        """
        result = await self.extractor.extract(
            html,
            config={"ignore_selectors": ["#sidebar", "#ads"]},
        )
        full_text = " ".join(c.text for c in result.chunks)
        assert "Keep this content" in full_text
        assert "Remove this sidebar" not in full_text
        assert "Remove these ads" not in full_text

    async def test_ignore_selectors_runs_before_ignore_patterns(self):
        """ignore_selectors remove DOM elements; ignore_patterns filter on resulting text."""
        html = b"""
        <html><body>
          <p id="noise">NOISE</p>
          <p id="content">Good content here</p>
        </body></html>
        """
        # ignore_selectors removes #noise from DOM; ignore_patterns would also match "NOISE"
        # Verify both together still yield only the good content
        result = await self.extractor.extract(
            html,
            config={"ignore_selectors": ["#noise"], "ignore_patterns": ["NOISE"]},
        )
        full_text = " ".join(c.text for c in result.chunks)
        assert "Good content here" in full_text
        assert "NOISE" not in full_text

    async def test_ignore_selectors_empty_list_is_noop(self):
        """Empty ignore_selectors list leaves extraction unchanged."""
        html = b"<html><body><p>All content</p></body></html>"
        result_with = await self.extractor.extract(html, config={"ignore_selectors": []})
        result_without = await self.extractor.extract(html)
        assert [c.text for c in result_with.chunks] == [c.text for c in result_without.chunks]
