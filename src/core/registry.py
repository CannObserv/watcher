"""Lightweight registry for swappable protocol implementations."""

from src.core.extractors import CsvExcelExtractor, HtmlExtractor, PdfExtractor
from src.core.extractors.base import Extractor
from src.core.fetchers.base import Fetcher
from src.core.fetchers.http import HttpFetcher

_DEFAULT_EXTRACTOR_MAP: dict[str, type[Extractor]] = {
    "html": HtmlExtractor,
    "pdf": PdfExtractor,
    "file": CsvExcelExtractor,
}


class ServiceRegistry:
    """Lightweight registry for swappable protocol implementations."""

    def __init__(
        self,
        fetcher: Fetcher | None = None,
        extractor_map: dict[str, type[Extractor]] | None = None,
    ) -> None:
        """Initialise the registry with optional custom implementations.

        All parameters default to the production implementations when omitted.
        """
        self._fetcher: Fetcher | None = fetcher
        self._extractor_map: dict[str, type[Extractor]] = (
            extractor_map if extractor_map is not None else _DEFAULT_EXTRACTOR_MAP
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


_default_registry: "ServiceRegistry | None" = None


def get_registry() -> "ServiceRegistry":
    """Return the process-level ServiceRegistry singleton, creating it on first call."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ServiceRegistry()
    return _default_registry
