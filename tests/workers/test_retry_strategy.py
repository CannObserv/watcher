"""Unit tests for the Procrastinate RetryStrategy on check_watch.

The check_watch task must retry on SDK transient errors (httpx connection or
timeout, ServerError) in addition to Python builtins. Operator-fixable errors
(AuthError, NotFound, ValidationError) are NOT retried; those propagate.
"""

import httpx
from information_client.errors import ServerError

from src.workers.tasks import check_watch


def test_check_watch_retry_strategy_includes_sdk_errors() -> None:
    """check_watch retries on SDK transient failures, not on operator-fixable ones."""
    retry_exceptions = check_watch.retry_strategy.retry_exceptions
    assert ConnectionError in retry_exceptions
    assert TimeoutError in retry_exceptions
    assert httpx.ConnectError in retry_exceptions
    assert httpx.TimeoutException in retry_exceptions
    assert ServerError in retry_exceptions


def test_check_watch_retry_strategy_excludes_operator_errors() -> None:
    """Auth/NotFound/Validation must NOT be retried — operator-fixable, propagate loud."""
    from information_client.errors import AuthError, NotFound, ValidationError

    retry_exceptions = check_watch.retry_strategy.retry_exceptions
    assert AuthError not in retry_exceptions
    assert NotFound not in retry_exceptions
    assert ValidationError not in retry_exceptions


def test_check_watch_retry_strategy_max_attempts_is_three() -> None:
    """Bounded retry budget — 3 attempts then dead-letter."""
    assert check_watch.retry_strategy.max_attempts == 3
