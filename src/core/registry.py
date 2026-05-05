"""Lightweight registry for swappable protocol implementations."""

import os

from information_client import InformationClient

from src.core.extractors import CsvExcelExtractor, HtmlExtractor, PdfExtractor
from src.core.extractors.base import Extractor
from src.core.fetchers.base import Fetcher
from src.core.fetchers.http import HttpFetcher

_DEFAULT_EXTRACTOR_MAP: dict[str, type[Extractor]] = {
    "html": HtmlExtractor,
    "pdf": PdfExtractor,
    "file": CsvExcelExtractor,
}

_DEFAULT_INFORMATION_BASE_URL = "http://localhost:8020"


class ServiceRegistry:
    """Lightweight registry for swappable protocol implementations."""

    def __init__(
        self,
        fetcher: Fetcher | None = None,
        extractor_map: dict[str, type[Extractor]] | None = None,
        *,
        information_client: InformationClient | None = None,
    ) -> None:
        """Initialise the registry with optional custom implementations.

        All parameters default to the production implementations when omitted.
        ``information_client`` is keyword-only and, when provided, wins over env-driven
        construction (test seam).
        """
        self._fetcher: Fetcher | None = fetcher
        self._extractor_map: dict[str, type[Extractor]] = (
            extractor_map if extractor_map is not None else _DEFAULT_EXTRACTOR_MAP
        )
        self._information_client: InformationClient | None = information_client

    def get_fetcher(self) -> Fetcher:
        """Return the registered fetcher (HttpFetcher by default)."""
        if self._fetcher is None:
            self._fetcher = HttpFetcher()
        return self._fetcher

    def get_extractor(self, content_type: str) -> Extractor:
        """Return a fresh extractor instance for the given content type."""
        extractor_cls = self._extractor_map[content_type]
        return extractor_cls()

    def get_information_client(self) -> InformationClient:
        """Return the cached InformationClient, building from env on first call.

        ``INFORMATION_BASE_URL`` defaults to http://localhost:8020.
        ``INFORMATION_API_KEY`` is required; missing key raises RuntimeError so
        misconfiguration crashes the API on boot, not on first request.
        """
        if self._information_client is None:
            base_url = os.environ.get("INFORMATION_BASE_URL", _DEFAULT_INFORMATION_BASE_URL)
            api_key = os.environ.get("INFORMATION_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "INFORMATION_API_KEY is not set; cannot construct InformationClient"
                )
            self._information_client = InformationClient(base_url=base_url, api_key=api_key)
        return self._information_client

    async def aclose_information_client(self) -> None:
        """Close the cached InformationClient (no-op if not yet built).

        Resets internal state so a subsequent ``get_information_client`` call
        rebuilds from current env. Safe to call multiple times.
        """
        if self._information_client is not None:
            await self._information_client.aclose()
            self._information_client = None


_default_registry: "ServiceRegistry | None" = None


def get_registry() -> "ServiceRegistry":
    """Return the process-level ServiceRegistry singleton, creating it on first call."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ServiceRegistry()
    return _default_registry
