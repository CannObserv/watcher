"""Tests for pipeline helpers (_extraction_config_from_spec, _extract_with_spec).

TestProcessWatchedItem (the per-WatchedItem integration test class) deleted as
part of #185 Phase A — the pipeline is being rewritten in step 3 to remove the
Archiver SDK binding-fetch path entirely. New integration tests will be written
against the rewritten pipeline.
"""

from src.workers.pipeline import (
    _extract_with_spec,
    _extraction_config_from_spec,
)


class TestExtractionConfigFromSpec:
    def test_full_page_yields_empty_selectors(self):
        config = _extraction_config_from_spec({"extraction": {"algorithm": "full_page"}})
        assert config == {"selectors": []}

    def test_css_selector_yields_single_selector_list(self):
        config = _extraction_config_from_spec(
            {"extraction": {"algorithm": "css", "selector": ".target"}}
        )
        assert config == {"selectors": [".target"]}

    def test_missing_extraction_block_defaults_to_full_page(self):
        config = _extraction_config_from_spec({})
        assert config == {"selectors": []}


class TestExtractWithSpec:
    async def test_extracts_html_with_full_page_algorithm(self):
        document = {"extraction": {"algorithm": "full_page"}}
        result = await _extract_with_spec(b"<html><body><p>Hello</p></body></html>", document)
        assert len(result.chunks) >= 1
        assert any("Hello" in c.text for c in result.chunks)

    async def test_css_selector_filters_to_matching_section(self):
        document = {"extraction": {"algorithm": "css", "selector": ".target"}}
        result = await _extract_with_spec(
            b"<html><body><div class='target'>kept</div><div>dropped</div></body></html>",
            document,
        )
        joined = " ".join(c.text for c in result.chunks)
        assert "kept" in joined
        assert "dropped" not in joined
