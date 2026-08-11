"""Lightweight registry for swappable protocol implementations.

Held no SDK client since #254: the Archiver SDK was removed with Watcher's last
outbound HTTP call to Archiver, and the registry is now purely the extractor
dispatch table plus its test seam.
"""

from co_core.pure.extract import Extractor
from co_core.pure.extract.csv_excel import CsvExcelExtractor
from co_core.pure.extract.html import HtmlExtractor
from co_core.pure.extract.pdf import PdfExtractor

# Keyed by media-type essence (#168 slice 2). Dispatch is total: anything not
# listed (including None, application/json, and ambiguous types) falls back to the
# HTML extractor — the historical default — rather than raising.
_DEFAULT_EXTRACTOR_MAP: dict[str, type[Extractor]] = {
    "text/html": HtmlExtractor,
    "application/xhtml+xml": HtmlExtractor,
    "application/pdf": PdfExtractor,
    "text/csv": CsvExcelExtractor,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": CsvExcelExtractor,
}


class ServiceRegistry:
    """Lightweight registry for swappable protocol implementations."""

    def __init__(self, extractor_map: dict[str, type[Extractor]] | None = None) -> None:
        """Initialise the registry with optional custom implementations.

        All parameters default to the production implementations when omitted.

        There is no fetcher: Watcher stopped making origin requests at the
        Phase-4 cutover (#241) — bytes now arrive as blobs Replicator fetched.
        And no Archiver client: the registry announcement replaced the last call
        that needed one (#254).
        """
        self._extractor_map: dict[str, type[Extractor]] = (
            extractor_map if extractor_map is not None else _DEFAULT_EXTRACTOR_MAP
        )

    def get_extractor(self, media_type_essence: str | None) -> Extractor:
        """Return a fresh extractor for a media-type essence (total; HTML fallback).

        Raw observed media is open-world, so an unrecognised or missing essence
        resolves to the HTML extractor rather than raising — preserving the
        pre-#168 behaviour for everything that isn't explicitly PDF/CSV.
        """
        extractor_cls = self._extractor_map.get(media_type_essence or "", HtmlExtractor)
        return extractor_cls()


_default_registry: "ServiceRegistry | None" = None


def get_registry() -> "ServiceRegistry":
    """Return the process-level ServiceRegistry singleton, creating it on first call."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ServiceRegistry()
    return _default_registry


def set_registry_for_testing(registry: "ServiceRegistry | None") -> None:
    """Replace the process-level ServiceRegistry singleton (test seam).

    Pass ``None`` to reset; the next ``get_registry()`` call will rebuild a
    fresh default. Tests use this to inject a registry with a custom extractor
    map without poking the private global directly.
    """
    global _default_registry
    _default_registry = registry
