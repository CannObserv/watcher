"""Unit tests for rate limiter startup hydration."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.core.rate_limiter import DomainRateLimiter


async def test_hydrate_rate_limiter_loads_domains():
    from src.api.main import hydrate_rate_limiter
    from src.core.models.domain import Domain

    limiter = DomainRateLimiter()

    d1 = Domain(name="example.com", min_interval=2.0, max_concurrency=1, current_interval=4.0)
    d2 = Domain(name="other.gov", min_interval=5.0, max_concurrency=2, current_interval=5.0)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [d1, d2]

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)
    with patch("src.api.main.get_session_factory", return_value=mock_factory):
        await hydrate_rate_limiter(limiter)

    assert limiter._domains["example.com"].min_interval == 2.0
    assert limiter._domains["example.com"].current_interval == 4.0
    assert limiter._domains["example.com"].semaphore._value == 1
    assert limiter._domains["other.gov"].min_interval == 5.0
    assert limiter._domains["other.gov"].current_interval == 5.0


async def test_hydrate_rate_limiter_empty_db():
    from src.api.main import hydrate_rate_limiter

    limiter = DomainRateLimiter()

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_session)
    with patch("src.api.main.get_session_factory", return_value=mock_factory):
        await hydrate_rate_limiter(limiter)

    assert len(limiter._domains) == 0
