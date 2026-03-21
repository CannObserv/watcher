"""Fetcher protocol and result dataclass."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class FetchResult:
    """Result of a fetch operation."""

    content: bytes
    status_code: int
    headers: dict
    duration_ms: int
    fetcher_used: str

    @property
    def is_success(self) -> bool:
        """True if status code indicates success (2xx or 3xx)."""
        return 200 <= self.status_code < 400


class Fetcher(Protocol):
    """Protocol for URL fetchers."""

    async def fetch(self, url: str, config: dict | None = None) -> FetchResult:
        """Fetch content from a URL."""
        ...
