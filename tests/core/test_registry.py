"""Tests for ServiceRegistry in src/core/registry.py."""

from unittest.mock import MagicMock

import httpx
import pytest

from src.core.extractors import CsvExcelExtractor, HtmlExtractor, PdfExtractor
from src.core.fetchers.http import HttpFetcher
from src.core.notifications import EmailChannel, SlackChannel, WebhookChannel
from src.core.registry import ServiceRegistry


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
        extractor = registry.get_extractor("html")
        assert isinstance(extractor, HtmlExtractor)

    def test_get_extractor_pdf(self):
        registry = ServiceRegistry()
        extractor = registry.get_extractor("pdf")
        assert isinstance(extractor, PdfExtractor)

    def test_get_extractor_file(self):
        registry = ServiceRegistry()
        extractor = registry.get_extractor("file")
        assert isinstance(extractor, CsvExcelExtractor)

    def test_get_extractor_returns_fresh_instance(self):
        registry = ServiceRegistry()
        e1 = registry.get_extractor("html")
        e2 = registry.get_extractor("html")
        assert e1 is not e2

    def test_get_extractor_unknown_type_raises(self):
        registry = ServiceRegistry()
        with pytest.raises(KeyError):
            registry.get_extractor("unknown")

    def test_get_channels_returns_all_three(self):
        registry = ServiceRegistry()
        client = httpx.AsyncClient()
        channels = registry.get_channels(client)
        assert "webhook" in channels
        assert "email" in channels
        assert "slack" in channels

    def test_get_channels_webhook_type(self):
        registry = ServiceRegistry()
        client = httpx.AsyncClient()
        channels = registry.get_channels(client)
        assert isinstance(channels["webhook"], WebhookChannel)

    def test_get_channels_email_type(self):
        registry = ServiceRegistry()
        client = httpx.AsyncClient()
        channels = registry.get_channels(client)
        assert isinstance(channels["email"], EmailChannel)

    def test_get_channels_slack_type(self):
        registry = ServiceRegistry()
        client = httpx.AsyncClient()
        channels = registry.get_channels(client)
        assert isinstance(channels["slack"], SlackChannel)


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

    def test_custom_channel_map(self):
        mock_channel_cls = MagicMock(return_value=MagicMock())
        registry = ServiceRegistry(channel_map={"custom": mock_channel_cls})
        client = httpx.AsyncClient()
        channels = registry.get_channels(client)
        assert "custom" in channels
