"""Health and readiness check endpoints."""

import os

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session

BUILD_ID = os.environ.get("BUILD_ID", "dev")

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    """Liveness probe — confirms the app process is running. No DB call."""
    return {"status": "ok", "build": BUILD_ID}


@router.get("/ready")
async def ready(session: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    """Readiness probe — checks DB connectivity and queue accessibility.

    Returns 200 when all dependencies are reachable, 503 otherwise.
    The queue check is a best-effort stub; procrastinate does not expose a
    lightweight ping, so queue is always reported as True unless further
    introspection is added.
    """
    db_ok = False
    queue_ok = True  # procrastinate has no lightweight ping; always reported available

    try:
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
