"""Tests for ServiceRegistry in src/core/registry.py."""

from unittest.mock import MagicMock

from co_core.pure.extract.csv_excel import CsvExcelExtractor
from co_core.pure.extract.html import HtmlExtractor
from co_core.pure.extract.pdf import PdfExtractor

from src.core.registry import ServiceRegistry, get_registry, set_registry_for_testing


class TestServiceRegistryDefaults:
    def test_get_extractor_html(self):
        registry = ServiceRegistry()
        extractor = registry.get_extractor("text/html")
        assert isinstance(extractor, HtmlExtractor)

    def test_get_extractor_pdf(self):
        registry = ServiceRegistry()
        extractor = registry.get_extractor("application/pdf")
        assert isinstance(extractor, PdfExtractor)

    def test_get_extractor_csv(self):
        registry = ServiceRegistry()
        extractor = registry.get_extractor("text/csv")
        assert isinstance(extractor, CsvExcelExtractor)

    def test_get_extractor_xlsx(self):
        registry = ServiceRegistry()
        extractor = registry.get_extractor(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert isinstance(extractor, CsvExcelExtractor)

    def test_get_extractor_returns_fresh_instance(self):
        registry = ServiceRegistry()
        e1 = registry.get_extractor("text/html")
        e2 = registry.get_extractor("text/html")
        assert e1 is not e2

    def test_get_extractor_unknown_essence_falls_back_to_html(self):
        """Total dispatch (#168): open-world media types resolve to HTML, never raise."""
        registry = ServiceRegistry()
        assert isinstance(registry.get_extractor("application/json"), HtmlExtractor)
        assert isinstance(registry.get_extractor(None), HtmlExtractor)
        assert isinstance(registry.get_extractor("application/octet-stream"), HtmlExtractor)


class TestServiceRegistryCustomInjection:
    def test_custom_extractor_map(self):
        mock_cls = MagicMock(return_value=MagicMock())
        registry = ServiceRegistry(extractor_map={"custom": mock_cls})
        extractor = registry.get_extractor("custom")
        mock_cls.assert_called_once()
        assert extractor is mock_cls.return_value


class TestSetRegistryForTesting:
    def test_set_registry_replaces_singleton(self):
        custom = ServiceRegistry()
        try:
            set_registry_for_testing(custom)
            assert get_registry() is custom
        finally:
            set_registry_for_testing(None)

    def test_set_registry_none_resets_to_fresh_default(self):
        sentinel = ServiceRegistry()
        set_registry_for_testing(sentinel)
        try:
            assert get_registry() is sentinel
        finally:
            set_registry_for_testing(None)
        rebuilt = get_registry()
        assert rebuilt is not sentinel
        assert isinstance(rebuilt, ServiceRegistry)
        # Cleanup so other tests start from a fresh default.
        set_registry_for_testing(None)
