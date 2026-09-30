"""Health and readiness check endpoints."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.core.config import BUILD_ID

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Liveness probe — confirms the app process is running. No DB call."""
    return {"status": "ok", "build": BUILD_ID}


@router.get("/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    """Readiness probe — checks DB connectivity and the embedded worker.

    Returns 200 when both are up, 503 otherwise. ``queue`` is the worker
    supervisor's liveness (#340): false while a dead worker waits out its
    restart backoff, and in a process that never started one. It lags a death
    by procrastinate's own shutdown — running jobs, then an unregister that
    needs a pool connection — since the run has not returned until that ends.

    A failed ping is 503 whatever it raised, never 500. SQLAlchemy does not
    wrap what asyncpg raises while connecting — bare ``OSError``s (refused,
    reset) and ``PostgresError``s (``CannotConnectNowError`` while the cluster
    shuts down or starts) — and those escaped as a 500 during exactly the
    outage this probe exists to report.
    """
    db_ok = False
    supervisor = getattr(request.app.state, "worker_supervisor", None)
    queue_ok = bool(supervisor is not None and supervisor.alive)

    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # any failed ping is "not ready" — see the docstring
        db_ok = False

    if db_ok and queue_ok:
        return JSONResponse(
            status_code=200,
            content={"status": "ready", "db": db_ok, "queue": queue_ok},
        )

    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "db": db_ok, "queue": queue_ok},
    )
