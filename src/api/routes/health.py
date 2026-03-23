"""Health and readiness check endpoints."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.core.database import get_session_factory

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Liveness probe — confirms the app process is running. No DB call."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe — checks DB connectivity and queue accessibility.

    Returns 200 when all dependencies are reachable, 503 otherwise.
    The queue check is a best-effort stub; procrastinate does not expose a
    lightweight ping, so queue is always reported as True unless further
    introspection is added.
    """
    db_ok = False
    # queue: procrastinate has no lightweight ping; mark as available
    queue_ok = True  # noqa: SIM910 — intentional stub

    try:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except SQLAlchemyError:
        db_ok = False

    if db_ok:
        return JSONResponse(
            status_code=200,
            content={"status": "ready", "db": db_ok, "queue": queue_ok},
        )

    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "db": db_ok, "queue": queue_ok},
    )
