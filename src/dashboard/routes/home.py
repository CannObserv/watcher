"""Dashboard home page and its stats/system-health partials."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.core.logging import get_logger
from src.dashboard.context import (
    get_dashboard_stats,
    get_domains_with_watched_item_counts,
    get_queue_health,
)
from src.dashboard.templating import templates

router = APIRouter()
logger = get_logger(__name__)


@router.get("/")
async def dashboard_home(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Dashboard home page with stats and system health.

    Phase 5 (#156): Recent Changes section removed — Change table dropped.
    """
    stats = await get_dashboard_stats(session)
    queue = await get_queue_health(session)
    domains = await get_domains_with_watched_item_counts(session)

    context = {
        "active_page": "dashboard",
        "stats": stats,
        "queue": queue,
        "domains": domains,
    }
    return templates.TemplateResponse(request, "pages/dashboard.html", context)


@router.get("/partials/stats-cards")
async def partial_stats_cards(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: stats cards only."""
    stats = await get_dashboard_stats(session)
    return templates.TemplateResponse(request, "partials/stats_cards.html", {"stats": stats})


@router.get("/partials/system-health")
async def partial_system_health(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: queue health and rate limiter."""
    queue = await get_queue_health(session)
    domains = await get_domains_with_watched_item_counts(session)
    return templates.TemplateResponse(
        request,
        "partials/system_health.html",
        {"queue": queue, "domains": domains},
    )
