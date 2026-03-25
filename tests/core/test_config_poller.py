"""Tests for config poller — periodic domain config reload."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

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


async def test_poll_excludes_archived_domains():
    """Archived domains must not be synced into the rate limiter."""
    limiter = DomainRateLimiter()
    archived = Domain(
        name="archived.com",
        min_interval=1.0,
        max_concurrency=1,
        archived_at=datetime.now(UTC),
    )

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [archived]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)

    last_poll = datetime.now(UTC) - timedelta(seconds=60)
    await poll_domain_configs(limiter, mock_factory, last_poll)

    # Because the DB filter is applied in SQL, the mock returns the archived
    # domain anyway — this test instead verifies the WHERE clause is built
    # with archived_at.is_(None) by inspecting the SQL statement sent.
    call_args = mock_session.execute.call_args
    stmt = call_args[0][0]
    compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    assert "archived_at IS NULL" in str(compiled)


@pytest.mark.integration
async def test_poll_excludes_archived_domains_integration(db_session):
    """Archived domains must not appear in poll results."""
    from contextlib import asynccontextmanager

    active = Domain(name="active.com", min_interval=2.0)
    archived = Domain(name="archived.com", min_interval=1.0, archived_at=datetime.now(UTC))
    db_session.add_all([active, archived])
    await db_session.flush()

    limiter = DomainRateLimiter()
    last_poll = datetime(2020, 1, 1, tzinfo=UTC)

    @asynccontextmanager
    async def session_factory():
        yield db_session

    await poll_domain_configs(limiter, session_factory, last_poll)

    assert "active.com" in limiter._domains
    assert "archived.com" not in limiter._domains


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
