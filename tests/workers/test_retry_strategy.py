"""Unit tests for the Procrastinate RetryStrategy on check_watched_item.

check_watched_item retries on transient network errors (httpx connection or
timeout, Python builtins). Since #185 Phase A removed the Archiver SDK call
from the pipeline path, ServerError is no longer a relevant retry exception.
"""

import httpx

from src.workers.tasks import check_watched_item


def test_check_watched_item_retry_strategy_includes_network_errors() -> None:
    """check_watched_item retries on transient network failures."""
    retry_exceptions = check_watched_item.retry_strategy.retry_exceptions
    assert ConnectionError in retry_exceptions
    assert TimeoutError in retry_exceptions
    assert httpx.ConnectError in retry_exceptions
    assert httpx.TimeoutException in retry_exceptions


def test_check_watched_item_retry_strategy_max_attempts_is_three() -> None:
    """Bounded retry budget — 3 attempts then dead-letter."""
    assert check_watched_item.retry_strategy.max_attempts == 3
