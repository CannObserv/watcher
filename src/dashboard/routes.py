"""Dashboard page routes — server-rendered HTML via Jinja2 + HTMX."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.core.models.audit_log import AuditLog
from src.core.models.notification_config import NotificationConfig
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watch import Watch
from src.dashboard import templates
from src.dashboard.context import (
    get_dashboard_stats,
    get_queue_health,
    get_rate_limiter_state,
    get_recent_changes,
    get_watch_changes,
    get_watch_detail,
    get_watch_list,
)
from src.workers.tasks import get_rate_limiter

router = APIRouter(tags=["dashboard"])


@router.get("/")
async def dashboard_home(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Dashboard home page with stats, recent changes, and system health."""
    stats = await get_dashboard_stats(session)
    changes = await get_recent_changes(session, limit=20)
    queue = await get_queue_health(session)
    domains = get_rate_limiter_state(get_rate_limiter())

    context = {
        "request": request,
        "active_page": "dashboard",
        "stats": stats,
        "changes": changes,
        "queue": queue,
        "domains": domains,
    }
    return templates.TemplateResponse("pages/dashboard.html", context)


@router.get("/watches")
async def watches_page(
    request: Request,
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Watch list page."""
    watches = await get_watch_list(session, is_active=is_active)
    context = {"request": request, "active_page": "watches", "watches": watches}
    return templates.TemplateResponse("pages/watches.html", context)


@router.get("/watches/new")
async def watch_create_form(request: Request):
    """Watch creation form."""
    return templates.TemplateResponse(
        "pages/watch_form.html",
        {"request": request, "active_page": "watches", "watch": None, "flash": None},
    )


@router.post("/watches/new")
async def watch_create_submit(
    request: Request,
    name: str = Form(""),
    url: str = Form(""),
    content_type: str = Form("html"),
    interval: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Handle watch creation form submission."""
    errors = []
    if not name.strip():
        errors.append("Name is required")
    if not url.strip():
        errors.append("URL is required")

    if errors:
        flash = {"type": "error", "message": ". ".join(errors)}
        return templates.TemplateResponse(
            "pages/watch_form.html",
            {"request": request, "active_page": "watches", "watch": None, "flash": flash},
        )

    schedule_config = {}
    if interval.strip():
        schedule_config["interval"] = interval.strip()

    watch = Watch(
        name=name.strip(),
        url=url.strip(),
        content_type=content_type,
        schedule_config=schedule_config,
    )
    session.add(watch)
    session.add(
        AuditLog(
            event_type="watch.created",
            watch_id=watch.id,
            payload={"name": name, "url": url, "source": "dashboard"},
        )
    )
    await session.commit()
    return RedirectResponse(url=f"/watches/{watch.id}", status_code=303)


@router.get("/watches/{watch_id}")
async def watch_detail_page(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Watch detail page with profiles, notifications, and change history."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return HTMLResponse(status_code=404, content="Watch not found")
    changes = await get_watch_changes(session, watch_id)

    # Load profiles and notification configs
    profiles_result = await session.execute(
        select(TemporalProfile).where(TemporalProfile.watch_id == watch.id)
    )
    profiles = list(profiles_result.scalars().all())
    nc_result = await session.execute(
        select(NotificationConfig).where(NotificationConfig.watch_id == watch.id)
    )
    notifications = list(nc_result.scalars().all())

    context = {
        "request": request,
        "active_page": "watches",
        "watch": watch,
        "changes": changes,
        "profiles": profiles,
        "notifications": notifications,
    }
    return templates.TemplateResponse("pages/watch_detail.html", context)


@router.get("/watches/{watch_id}/edit")
async def watch_edit_form(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Watch edit form, prefilled with current values."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return HTMLResponse(status_code=404, content="Watch not found")
    return templates.TemplateResponse(
        "pages/watch_form.html",
        {"request": request, "active_page": "watches", "watch": watch, "flash": None},
    )


@router.post("/watches/{watch_id}/edit")
async def watch_edit_submit(
    request: Request,
    watch_id: str,
    name: str = Form(""),
    url: str = Form(""),
    content_type: str = Form("html"),
    interval: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Handle watch edit form submission."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return HTMLResponse(status_code=404, content="Watch not found")

    errors = []
    if not name.strip():
        errors.append("Name is required")
    if not url.strip():
        errors.append("URL is required")

    if errors:
        flash = {"type": "error", "message": ". ".join(errors)}
        return templates.TemplateResponse(
            "pages/watch_form.html",
            {"request": request, "active_page": "watches", "watch": watch, "flash": flash},
        )

    watch.name = name.strip()
    watch.url = url.strip()
    watch.content_type = content_type
    schedule_config = watch.schedule_config or {}
    if interval.strip():
        schedule_config["interval"] = interval.strip()
    watch.schedule_config = schedule_config

    session.add(
        AuditLog(
            event_type="watch.updated",
            watch_id=watch.id,
            payload={
                "updated_fields": ["name", "url", "content_type", "schedule_config"],
                "source": "dashboard",
            },
        )
    )
    await session.commit()
    return RedirectResponse(url=f"/watches/{watch.id}", status_code=303)


@router.post("/watches/{watch_id}/deactivate")
async def watch_deactivate(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Deactivate a watch via HTMX — returns updated row or status snippet."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return HTMLResponse(status_code=404, content="Watch not found")
    watch.is_active = False
    session.add(
        AuditLog(
            event_type="watch.deactivated",
            watch_id=watch.id,
            payload={"name": watch.name, "source": "dashboard"},
        )
    )
    await session.commit()
    await session.refresh(watch)

    # Detail page targets #watch-status; list page targets #watch-{id} row
    hx_target = request.headers.get("HX-Target", "")
    if hx_target == "watch-status":
        html = '<dt class="text-sm text-gray-600">Status</dt>'
        html += '<dd class="text-sm font-medium text-gray-500">Inactive</dd>'
        return HTMLResponse(content=html)
    return templates.TemplateResponse(
        "partials/watch_row.html", {"request": request, "watch": watch}
    )


@router.get("/partials/stats-cards")
async def partial_stats_cards(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: stats cards only."""
    stats = await get_dashboard_stats(session)
    return templates.TemplateResponse(
        "partials/stats_cards.html", {"request": request, "stats": stats}
    )


@router.get("/partials/recent-changes")
async def partial_recent_changes(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: recent changes table."""
    changes = await get_recent_changes(session, limit=20)
    return templates.TemplateResponse(
        "partials/recent_changes.html", {"request": request, "changes": changes}
    )


@router.get("/partials/system-health")
async def partial_system_health(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: queue health and rate limiter."""
    queue = await get_queue_health(session)
    domains = get_rate_limiter_state(get_rate_limiter())
    return templates.TemplateResponse(
        "partials/system_health.html",
        {"request": request, "queue": queue, "domains": domains},
    )


@router.get("/partials/watch-table")
async def partial_watch_table(
    request: Request,
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: watch table with optional filter."""
    watches = await get_watch_list(session, is_active=is_active)
    return templates.TemplateResponse(
        "partials/watch_table.html", {"request": request, "watches": watches}
    )


@router.get("/partials/watch-changes/{watch_id}")
async def partial_watch_changes(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: change history for a watch."""
    changes = await get_watch_changes(session, watch_id)
    return templates.TemplateResponse(
        "partials/watch_changes.html", {"request": request, "changes": changes}
    )
