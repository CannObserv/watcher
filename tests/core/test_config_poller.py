"""Tests for config poller — periodic domain config reload."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from src.core.config_poller import poll_domain_configs
from src.core.models.domain import Domain
from src.core.rate_limiter import DomainRateLimiter


async def test_poll_updates_changed_domains():
    limiter = DomainRateLimiter()
    d1 = Domain(name="changed.com", min_interval=2.0, max_concurrency=3, current_interval=4.0)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [d1]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)

    last_poll = datetime.now(UTC) - timedelta(seconds=60)
    new_poll = await poll_domain_configs(limiter, mock_factory, last_poll)

    assert limiter._domains["changed.com"].min_interval == 2.0
    assert limiter._domains["changed.com"].current_interval == 4.0
    assert limiter._domains["changed.com"].semaphore._value == 3
    assert new_poll > last_poll


async def test_poll_no_changes():
    limiter = DomainRateLimiter()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)

    last_poll = datetime.now(UTC) - timedelta(seconds=60)
    new_poll = await poll_domain_configs(limiter, mock_factory, last_poll)

    assert len(limiter._domains) == 0
    assert new_poll > last_poll


async def test_poll_handles_db_error():
    limiter = DomainRateLimiter()

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=Exception("DB down"))
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)

    last_poll = datetime.now(UTC) - timedelta(seconds=60)
    new_poll = await poll_domain_configs(limiter, mock_factory, last_poll)

    assert new_poll == last_poll
