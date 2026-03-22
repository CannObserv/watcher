"""Dashboard page routes — server-rendered HTML via Jinja2 + HTMX."""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session
from src.core.models.audit_log import AuditLog
from src.core.models.watch import ContentType, Watch
from src.core.storage import LocalStorage
from src.dashboard import templates
from src.dashboard.context import (
    generate_diff,
    get_change_detail,
    get_dashboard_stats,
    get_queue_health,
    get_rate_limiter_state,
    get_recent_changes,
    get_watch_changes,
    get_watch_detail,
    get_watch_list,
    get_watch_notifications,
    get_watch_profiles,
)
from src.workers.tasks import get_rate_limiter

STORAGE_BASE_DIR = Path(os.environ.get("WATCHER_DATA_DIR", "/var/lib/watcher/data"))

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
        {
            "request": request,
            "active_page": "watches",
            "watch": None,
            "flash": None,
            "content_types": list(ContentType),
        },
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
            {
                "request": request,
                "active_page": "watches",
                "watch": None,
                "flash": flash,
                "content_types": list(ContentType),
            },
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
    profiles = await get_watch_profiles(session, watch.id)
    notifications = await get_watch_notifications(session, watch.id)

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
        {
            "request": request,
            "active_page": "watches",
            "watch": watch,
            "flash": None,
            "content_types": list(ContentType),
        },
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
            {
                "request": request,
                "active_page": "watches",
                "watch": watch,
                "flash": flash,
                "content_types": list(ContentType),
            },
        )

    watch.name = name.strip()
    watch.url = url.strip()
    watch.content_type = content_type
    schedule_config = dict(watch.schedule_config or {})
    if interval.strip():
        schedule_config["interval"] = interval.strip()
    else:
        schedule_config.pop("interval", None)
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


@router.get("/changes/{change_id}")
async def change_detail_page(
    request: Request,
    change_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Change detail page with metadata, chunks, and diff."""
    detail = await get_change_detail(session, change_id)
    if not detail:
        return HTMLResponse(status_code=404, content="Change not found")

    # Generate diff from extracted text stored on disk
    storage = LocalStorage(base_dir=STORAGE_BASE_DIR)
    prev_text = ""
    curr_text = ""
    if detail["previous_snapshot"] and detail["previous_snapshot"].text_path:
        try:
            raw = storage.load(detail["previous_snapshot"].text_path)
            prev_text = raw.decode(errors="replace")
        except FileNotFoundError:
            pass
    if detail["current_snapshot"] and detail["current_snapshot"].text_path:
        try:
            raw = storage.load(detail["current_snapshot"].text_path)
            curr_text = raw.decode(errors="replace")
        except FileNotFoundError:
            pass

    diff = generate_diff(prev_text, curr_text)

    context = {
        "request": request,
        "active_page": "watches",
        **detail,
        "diff": diff,
    }
    return templates.TemplateResponse("pages/change_detail.html", context)


@router.get("/partials/diff/{change_id}")
async def partial_diff(
    request: Request,
    change_id: str,
    mode: str = "extracted",
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: diff view (extracted text or raw content)."""
    detail = await get_change_detail(session, change_id)
    if not detail:
        return HTMLResponse(status_code=404, content="Change not found")

    storage = LocalStorage(base_dir=STORAGE_BASE_DIR)
    prev_text = ""
    curr_text = ""

    if mode == "raw":
        path_attr = "storage_path"
    else:
        path_attr = "text_path"

    prev_snap = detail["previous_snapshot"]
    curr_snap = detail["current_snapshot"]
    if prev_snap and getattr(prev_snap, path_attr):
        try:
            raw = storage.load(getattr(prev_snap, path_attr))
            prev_text = raw.decode(errors="replace")
        except FileNotFoundError:
            pass
    if curr_snap and getattr(curr_snap, path_attr):
        try:
            raw = storage.load(getattr(curr_snap, path_attr))
            curr_text = raw.decode(errors="replace")
        except FileNotFoundError:
            pass

    diff = generate_diff(prev_text, curr_text)
    return templates.TemplateResponse("partials/diff_view.html", {"request": request, "diff": diff})
