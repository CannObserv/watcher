"""Tests for extraction_config_from_spec — InfoSpec → HtmlExtractor config translation."""

from src.core.extraction_defaults import extraction_config_from_spec


class TestExtractionConfigFromSpec:
    def test_full_page_yields_empty_selectors(self):
        config = extraction_config_from_spec({"extraction": {"algorithm": "full_page"}})
        assert config == {"selectors": []}

    def test_css_selector_yields_single_selector_list(self):
        config = extraction_config_from_spec(
            {"extraction": {"algorithm": "css", "selector": ".target"}}
        )
        assert config == {"selectors": [".target"]}

    def test_missing_extraction_block_defaults_to_full_page(self):
        config = extraction_config_from_spec({})
        assert config == {"selectors": []}

    def test_css_with_empty_selector_yields_empty_list(self):
        config = extraction_config_from_spec({"extraction": {"algorithm": "css", "selector": ""}})
        assert config == {"selectors": []}
