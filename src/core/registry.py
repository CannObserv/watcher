"""Lightweight registry for swappable protocol implementations.

Held no SDK client since #254: the Archiver SDK was removed with Watcher's last
outbound HTTP call to Archiver, and the registry is now purely the extractor
dispatch plus its test seam.

The table itself is co-core's ``EXTRACTOR_BY_ESSENCE`` (#342), held by
reference: Processor dispatches from the same object, so an essence co-core
adds (cannobserv#523's XML and feeds) reaches both sides with the pin, and
shadow mode compares like with like. A local copy would route the new essence
to the HTML fallback here while Processor used the new extractor.
"""

from collections.abc import Mapping
from types import MappingProxyType

from co_core.pure.extract import Extractor
from co_core.pure.extract.dispatch import DEFAULT_EXTRACTOR, EXTRACTOR_BY_ESSENCE


class ServiceRegistry:
    """Lightweight registry for swappable protocol implementations."""

    def __init__(self, extractor_map: Mapping[str, type[Extractor]] | None = None) -> None:
        """Initialise the registry with optional custom implementations.

        All parameters default to the production implementations when omitted.

        There is no fetcher: Watcher stopped making origin requests at the
        Phase-4 cutover (#241) — bytes now arrive as blobs Replicator fetched.
        And no Archiver client: the registry announcement replaced the last call
        that needed one (#254).
        """
        # A read-only live view, not the dict: co-core's table is process-wide,
        # so a write through the registry would reach every other caller of it.
        self._extractor_map: Mapping[str, type[Extractor]] = (
            extractor_map if extractor_map is not None else MappingProxyType(EXTRACTOR_BY_ESSENCE)
        )

    def get_extractor(self, media_type_essence: str | None) -> Extractor:
        """Return a fresh extractor for a media-type essence (total; co-core's fallback).

        Raw observed media is open-world, so an unrecognised or missing essence
        resolves to co-core's ``DEFAULT_EXTRACTOR`` (HTML) rather than raising.
        Not ``extractor_for_essence``: that reads the shared table directly and
        would bypass an injected ``extractor_map``.
        """
        extractor_cls = self._extractor_map.get(media_type_essence or "", DEFAULT_EXTRACTOR)
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
