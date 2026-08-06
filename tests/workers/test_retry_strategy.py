"""Unit tests for the Procrastinate RetryStrategy on check_watched_item.

Post-cutover (#241 step 5) the task makes no origin request, so the httpx
exceptions it used to retry on are unreachable. What remains retryable is the
transient infrastructure underneath the issue itself — the DB read and the
persist-before-publish write. A broker failure is deliberately NOT retried
here: ``_issue_fetch_command`` swallows it and leaves the row
``pending_publish`` for the every-minute sweep, which is the durable path.
"""

from src.workers.tasks import check_watched_item


def test_check_watched_item_retry_strategy_includes_transient_infra_errors() -> None:
    """check_watched_item retries on transient infrastructure failures."""
    retry_exceptions = check_watched_item.retry_strategy.retry_exceptions
    assert ConnectionError in retry_exceptions
    assert TimeoutError in retry_exceptions


def test_check_watched_item_retry_strategy_max_attempts_is_three() -> None:
    """Bounded retry budget — 3 attempts then dead-letter."""
    assert check_watched_item.retry_strategy.max_attempts == 3
