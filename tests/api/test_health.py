"""Tests for /health and /ready operational endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

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
        assert response.json() == {"status": "ok"}

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

        # Patch the session execute to succeed
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=None)

        with patch("src.api.routes.health.get_session_factory") as mock_factory:
            mock_factory.return_value.return_value = mock_session
            async with await _make_client(app) as c:
                response = await c.get("/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["db"] is True
        assert "queue" in data

    async def test_ready_returns_503_when_db_unavailable(self):
        """/ready returns 503 with status not_ready when DB raises."""
        from src.api.main import app

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(
            side_effect=OperationalError("conn failed", {}, Exception("conn failed"))
        )

        with patch("src.api.routes.health.get_session_factory") as mock_factory:
            mock_factory.return_value.return_value = mock_session
            async with await _make_client(app) as c:
                response = await c.get("/ready")

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
