"""Fetcher protocol and implementations for retrieving web content."""

from src.core.fetchers.base import Fetcher, FetchResult
from src.core.fetchers.http import HttpFetcher

__all__ = ["FetchResult", "Fetcher", "HttpFetcher"]
