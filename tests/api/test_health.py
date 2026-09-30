"""Tests for /health and /ready operational endpoints."""

from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from asyncpg.exceptions import CannotConnectNowError
from httpx import ASGITransport, AsyncClient, Response
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


@pytest.fixture
def worker(request):
    """Install a worker supervisor on ``app.state``, as the lifespan does.

    ASGITransport never runs the lifespan, so without this ``/ready`` sees no
    worker at all. Parametrize indirectly with ``False`` for a dead one.
    """
    from src.api.main import app

    app.state.worker_supervisor = SimpleNamespace(alive=getattr(request, "param", True))
    yield
    del app.state.worker_supervisor


def _session_raising(exc: BaseException):
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(side_effect=exc)

    async def override_session() -> AsyncGenerator[AsyncSession]:
        yield mock_session

    return override_session


def _session_ok():
    mock_session = AsyncMock(spec=AsyncSession)
    mock_session.execute = AsyncMock(return_value=None)

    async def override_session() -> AsyncGenerator[AsyncSession]:
        yield mock_session

    return override_session


async def _get_ready(app, override) -> Response:
    app.dependency_overrides[get_db_session] = override
    try:
        async with await _make_client(app) as c:
            return await c.get("/ready")
    finally:
        app.dependency_overrides.pop(get_db_session, None)


class TestReadyEndpoint:
    @pytest.mark.usefixtures("worker")
    async def test_ready_returns_200_when_db_available(self):
        """/ready returns 200 with status ready when DB responds."""
        from src.api.main import app

        response = await _get_ready(app, _session_ok())

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["db"] is True
        assert data["queue"] is True

    @pytest.mark.parametrize("worker", [False], indirect=True)
    @pytest.mark.usefixtures("worker")
    async def test_a_dead_worker_is_not_ready(self):
        """#340: ``queue`` used to be a hardcoded ``true``, so a worker stopped by
        a cluster restart left ``/ready`` at 200 with every periodic task gone."""
        from src.api.main import app

        response = await _get_ready(app, _session_ok())

        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "db": True, "queue": False}

    async def test_no_worker_is_not_ready(self):
        """A process whose lifespan never started a worker has no queue."""
        from src.api.main import app

        response = await _get_ready(app, _session_ok())

        assert response.status_code == 503
        assert response.json()["queue"] is False

    @pytest.mark.usefixtures("worker")
    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionRefusedError(111, "Connection refused"),
            ConnectionResetError(104, "reset"),
            CannotConnectNowError("the database system is shutting down"),
        ],
    )
    async def test_an_unwrapped_connect_error_is_503_not_500(self, exc):
        """#340: SQLAlchemy does not wrap what asyncpg raises while connecting —
        bare OS errors, and ``PostgresError``s such as the cluster refusing
        connections while it shuts down or starts — so an outage answered 500,
        a crash, instead of 503, not ready."""
        from src.api.main import app

        response = await _get_ready(app, _session_raising(exc))

        assert response.status_code == 503
        assert response.json()["db"] is False

    @pytest.mark.usefixtures("worker")
    async def test_ready_returns_503_when_db_unavailable(self):
        """/ready returns 503 with status not_ready when DB raises — with a live
        worker, so the 503 is the database's alone."""
        from src.api.main import app

        response = await _get_ready(
            app,
            _session_raising(OperationalError("conn failed", {}, Exception("conn failed"))),
        )

        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "db": False, "queue": True}

    async def test_ready_not_under_api_v1(self):
        """/ready must NOT be mounted under /api/v1/."""
        from src.api.main import app

        async with await _make_client(app) as c:
            response = await c.get("/api/v1/ready")

        assert response.status_code == 404
