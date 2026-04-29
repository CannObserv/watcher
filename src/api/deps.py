"""FastAPI dependencies — database session, probe, and authentication."""

import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_session_factory
from src.core.models.api_key import ApiKey
from src.core.probe import ProbeResult, probe_url


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    """Yield an async database session."""
    async with get_session_factory()() as session:
        yield session


async def get_probe_fn() -> Callable[[str], Awaitable[ProbeResult]]:
    """Return the URL probe function. Override in tests to avoid real HTTP calls."""
    return probe_url


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    raw_key: str | None = Depends(api_key_header),
    session: AsyncSession = Depends(get_db_session),
) -> str:
    """Validate X-API-Key header; return user_id on success.

    Raises 403 when header is absent, 401 when key is invalid or not found.
    Updates last_used_at on each successful authentication.
    """
    if raw_key is None:
        raise HTTPException(status_code=403, detail="Not authenticated")
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    result = await session.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    api_key = result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    api_key.last_used_at = datetime.now(UTC)
    await session.commit()
    return api_key.user_id
