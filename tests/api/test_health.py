"""Tests for /health and /ready operational endpoints."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session

pytestmark = pytest.mark.anyio


async def _make_client(app):
    """Return an AsyncClient configured to hit the given ASGI app."""
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


class TestHealthEndpoint:
    async def test_health_returns_200(self):
        """GET /health returns 200 with status ok."""
        from src.api.main import app

        async with await _make_client(app) as c:
            response = await c.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "build" in body

    async def test_health_not_under_api_v1(self):
        """Health endpoint must NOT be mounted under /api/v1/."""
        from src.api.main import app

        async with await _make_client(app) as c:
            response = await c.get("/api/v1/health")

        assert response.status_code == 404


class TestReadyEndpoint:
    async def test_ready_returns_200_when_db_available(self):
        """/ready returns 200 with status ready when DB responds."""
        from src.api.main import app

        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.execute = AsyncMock(return_value=None)

        async def override_session() -> AsyncGenerator[AsyncSession]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        try:
            async with await _make_client(app) as c:
                response = await c.get("/ready")
        finally:
            app.dependency_overrides.pop(get_db_session, None)

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["db"] is True
        assert "queue" in data

    async def test_ready_returns_503_when_db_unavailable(self):
        """/ready returns 503 with status not_ready when DB raises."""
        from src.api.main import app

        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.execute = AsyncMock(
            side_effect=OperationalError("conn failed", {}, Exception("conn failed"))
        )

        async def override_session() -> AsyncGenerator[AsyncSession]:
            yield mock_session

        app.dependency_overrides[get_db_session] = override_session
        try:
            async with await _make_client(app) as c:
                response = await c.get("/ready")
        finally:
            app.dependency_overrides.pop(get_db_session, None)

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "not_ready"
        assert data["db"] is False
        assert "queue" in data

    async def test_ready_not_under_api_v1(self):
        """/ready must NOT be mounted under /api/v1/."""
        from src.api.main import app

        async with await _make_client(app) as c:
            response = await c.get("/api/v1/ready")

        assert response.status_code == 404
