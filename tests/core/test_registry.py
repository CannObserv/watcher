"""Tests for ServiceRegistry in src/core/registry.py."""

from unittest.mock import MagicMock

import pytest
from archiver_client import ArchiverClient
from co_core.pure.extract.csv_excel import CsvExcelExtractor
from co_core.pure.extract.html import HtmlExtractor
from co_core.pure.extract.pdf import PdfExtractor

from src.core.fetch import HttpFetcher
from src.core.registry import ServiceRegistry, get_registry, set_registry_for_testing


class TestServiceRegistryDefaults:
    def test_default_fetcher_is_http_fetcher(self):
        registry = ServiceRegistry()
        fetcher = registry.get_fetcher()
        assert isinstance(fetcher, HttpFetcher)

    def test_get_fetcher_returns_same_instance(self):
        registry = ServiceRegistry()
        assert registry.get_fetcher() is registry.get_fetcher()

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
    def test_custom_fetcher_is_returned(self):
        mock_fetcher = MagicMock()
        registry = ServiceRegistry(fetcher=mock_fetcher)
        assert registry.get_fetcher() is mock_fetcher

    def test_custom_extractor_map(self):
        mock_cls = MagicMock(return_value=MagicMock())
        registry = ServiceRegistry(extractor_map={"custom": mock_cls})
        extractor = registry.get_extractor("custom")
        mock_cls.assert_called_once()
        assert extractor is mock_cls.return_value


class TestServiceRegistryArchiverClient:
    def test_registry_provides_archiver_client(self, monkeypatch):
        monkeypatch.setenv("ARCHIVER_BASE_URL", "http://localhost:8020")
        monkeypatch.setenv("ARCHIVER_API_KEY", "test-key")
        reg = ServiceRegistry()
        client = reg.get_archiver_client()
        assert client is not None
        assert isinstance(client, ArchiverClient)
        assert client._base_url == "http://localhost:8020"

    def test_registry_archiver_client_singleton(self, monkeypatch):
        """Same instance returned on repeated calls (lazy + cached)."""
        monkeypatch.setenv("ARCHIVER_API_KEY", "test-key")
        reg = ServiceRegistry()
        a = reg.get_archiver_client()
        b = reg.get_archiver_client()
        assert a is b

    def test_registry_archiver_client_explicit_injection_wins(self):
        """If injected via constructor, env vars are ignored."""
        fake = MagicMock(spec=ArchiverClient)
        reg = ServiceRegistry(archiver_client=fake)
        assert reg.get_archiver_client() is fake

    def test_registry_raises_when_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("ARCHIVER_API_KEY", raising=False)
        reg = ServiceRegistry()
        with pytest.raises(RuntimeError, match="ARCHIVER_API_KEY"):
            reg.get_archiver_client()

    @pytest.mark.asyncio
    async def test_aclose_archiver_client_resets_lazy_state(self, monkeypatch):
        """After aclose, next get rebuilds from env."""
        monkeypatch.setenv("ARCHIVER_API_KEY", "test-key")
        reg = ServiceRegistry()
        client = reg.get_archiver_client()
        await reg.aclose_archiver_client()
        # Internal state cleared
        assert reg._archiver_client is None
        # Re-acquire builds a new instance
        monkeypatch.setenv("ARCHIVER_API_KEY", "different-key")
        new_client = reg.get_archiver_client()
        assert new_client is not client

    @pytest.mark.asyncio
    async def test_aclose_archiver_client_is_idempotent(self):
        """Calling aclose without a constructed client is a no-op."""
        reg = ServiceRegistry()
        # Should not raise even if no client was ever built.
        await reg.aclose_archiver_client()
        assert reg._archiver_client is None


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
