"""Lightweight registry for swappable protocol implementations."""

import inspect

import httpx

from src.core.extractors import CsvExcelExtractor, HtmlExtractor, PdfExtractor
from src.core.extractors.base import Extractor
from src.core.fetchers.base import Fetcher
from src.core.fetchers.http import HttpFetcher
from src.core.notifications import EmailChannel, SlackChannel, WebhookChannel
from src.core.notifications.base import NotificationChannel

_DEFAULT_EXTRACTOR_MAP: dict[str, type[Extractor]] = {
    "html": HtmlExtractor,
    "pdf": PdfExtractor,
    "file": CsvExcelExtractor,
}

_DEFAULT_CHANNEL_MAP: dict[str, type[NotificationChannel]] = {
    "webhook": WebhookChannel,
    "email": EmailChannel,
    "slack": SlackChannel,
}


class ServiceRegistry:
    """Lightweight registry for swappable protocol implementations."""

    def __init__(
        self,
        fetcher: Fetcher | None = None,
        extractor_map: dict[str, type[Extractor]] | None = None,
        channel_map: dict[str, type[NotificationChannel]] | None = None,
    ) -> None:
        """Initialise the registry with optional custom implementations.

        All parameters default to the production implementations when omitted.
        """
        self._fetcher: Fetcher | None = fetcher
        self._extractor_map: dict[str, type[Extractor]] = (
            extractor_map if extractor_map is not None else _DEFAULT_EXTRACTOR_MAP
        )
        self._channel_map: dict[str, type[NotificationChannel]] = (
            channel_map if channel_map is not None else _DEFAULT_CHANNEL_MAP
        )

    def get_fetcher(self) -> Fetcher:
        """Return the registered fetcher (HttpFetcher by default)."""
        if self._fetcher is None:
            self._fetcher = HttpFetcher()
        return self._fetcher

    def get_extractor(self, content_type: str) -> Extractor:
        """Return a fresh extractor instance for the given content type."""
        extractor_cls = self._extractor_map[content_type]
        return extractor_cls()

    def get_channels(self, client: httpx.AsyncClient) -> dict[str, NotificationChannel]:
        """Return instantiated notification channels keyed by name.

        Channels that accept an httpx.AsyncClient are constructed with it;
        channels that do not (e.g. EmailChannel) are constructed without it.
        """
        channels: dict[str, NotificationChannel] = {}
        for name, cls in self._channel_map.items():
            sig = inspect.signature(cls.__init__)
            params = set(sig.parameters.keys()) - {"self"}
            if "client" in params:
                channels[name] = cls(client=client)
            else:
                channels[name] = cls()
        return channels
