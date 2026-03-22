"""FastAPI dependencies — database session injection."""

from collections.abc import AsyncGenerator, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_session_factory
from src.core.probe import ProbeResult, probe_url


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """Yield an async database session."""
    async with get_session_factory()() as session:
        yield session


async def get_probe_fn() -> Callable[[str], Awaitable[ProbeResult]]:
    """Return the URL probe function. Override in tests to avoid real HTTP calls."""
    return probe_url
