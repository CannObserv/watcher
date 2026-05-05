"""FastAPI dependencies for the Information service."""

import os
from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from src.information.core.database import get_session_factory
from src.information.core.tools.fetch_and_render import HttpFetcherProtocol


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


def get_http_fetcher(request: Request) -> HttpFetcherProtocol:
    """Provide the lifespan-scoped HttpFetcher for tool routes.

    The fetcher is constructed once at app startup (see ``main.lifespan``) so
    its ``httpx.AsyncClient`` connection pool is shared across requests and
    closed cleanly on shutdown.

    Tests override this dependency with a no-arg callable, e.g.
    ``app.dependency_overrides[get_http_fetcher] = lambda: stub``. FastAPI
    invokes the override directly without re-resolving sub-deps, so the
    ``request: Request`` parameter is intentionally absent from the override
    signature — that's expected, not a mistake.
    """
    return request.app.state.http_fetcher


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(raw_key: str | None = Depends(api_key_header)) -> None:
    """Validate X-API-Key against INFORMATION_API_KEY env var.

    Raises 403 when the header is absent and 401 when it is present but invalid.
    """
    if raw_key is None:
        raise HTTPException(status_code=403, detail="Not authenticated")
    expected = os.environ.get("INFORMATION_API_KEY")
    if not expected or raw_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")
