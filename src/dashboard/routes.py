"""Dashboard page routes — server-rendered HTML via Jinja2 + HTMX."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db_session, get_probe_fn
from src.api.routes.watches import delete_watch as api_delete_watch
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import Domain
from src.core.models.watch import ContentType, Watch
from src.core.probe import ProbeResult
from src.core.storage import STORAGE_BASE_DIR, LocalStorage
from src.dashboard import templates
from src.dashboard.context import (
    generate_diff,
    get_audit_entries,
    get_change_detail,
    get_dashboard_stats,
    get_domain_watches,
    get_domains_total_count,
    get_domains_with_watch_counts,
    get_queue_health,
    get_recent_changes,
    get_watch_changes,
    get_watch_detail,
    get_watch_list,
    get_watch_notifications,
    get_watch_profiles,
)

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
    domains = await get_domains_with_watch_counts(session)

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
    context = {
        "request": request,
        "active_page": "watches",
        "watches": watches,
        "is_active": is_active,
    }
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
    audit(
        session,
        EventType.WATCH_CREATED,
        watch_id=watch.id,
        name=name,
        url=url,
        source="dashboard",
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
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)
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
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)
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
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

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

    audit(
        session,
        EventType.WATCH_UPDATED,
        watch_id=watch.id,
        updated_fields=["name", "url", "content_type", "schedule_config"],
        source="dashboard",
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
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)
    watch.is_active = False
    audit(
        session,
        EventType.WATCH_DEACTIVATED,
        watch_id=watch.id,
        name=watch.name,
        source="dashboard",
    )
    await session.commit()
    await session.refresh(watch)

    # Detail page targets #watch-status; list page targets #watch-{id} row
    hx_target = request.headers.get("HX-Target", "")
    if hx_target == "watch-status":
        html = '<dt class="text-sm text-gray-600 dark:text-gray-400">Status</dt>'
        html += '<dd class="text-sm font-medium text-gray-500 dark:text-gray-400">Inactive</dd>'
        return HTMLResponse(content=html)
    return templates.TemplateResponse(
        "partials/watch_row.html", {"request": request, "watch": watch}
    )


@router.delete("/watches/{watch_id}")
async def watch_delete(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete an inactive watch via HTMX — delegates to the API layer, adapts response for HTMX.

    Business logic (active-check, audit log, deletion, commit) lives in the API
    route. This handler's sole responsibility is translating the API outcome into
    an HTMX-compatible response: HX-Redirect on success, an inline error snippet
    on 409, or a 404 template if the watch is gone.
    """
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)
    try:
        await api_delete_watch(watch_id=watch_id, session=session)
    except HTTPException as exc:
        if exc.status_code == 409:
            msg = (
                '<p class="text-red-600 text-sm mt-2">Deactivate the watch before deleting it.</p>'
            )
            return HTMLResponse(status_code=409, content=msg)
        raise
    return HTMLResponse(status_code=200, content="", headers={"HX-Redirect": "/watches"})


@router.get("/domains")
async def domains_page(
    request: Request,
    q: str | None = None,
    status: str | None = "active",
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_db_session),
):
    """Domains list page with search, filter, and pagination."""
    domains = await get_domains_with_watch_counts(
        session,
        search=q,
        status=status,
        page=page,
        page_size=page_size,
    )
    total_count = await get_domains_total_count(session, search=q, status=status)
    context = {
        "request": request,
        "active_page": "domains",
        "domains": domains,
        "search": q,
        "status": status,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "base_url": "/partials/domains-table",
        "extra_params": {k: v for k, v in {"q": q, "status": status}.items() if v},
    }
    return templates.TemplateResponse("pages/domains.html", context)


@router.get("/partials/domains-table")
async def partial_domains_table(
    request: Request,
    q: str | None = None,
    status: str | None = "active",
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: domains table with search, filter, and pagination."""
    domains = await get_domains_with_watch_counts(
        session,
        search=q,
        status=status,
        page=page,
        page_size=page_size,
    )
    total_count = await get_domains_total_count(session, search=q, status=status)
    return templates.TemplateResponse(
        "partials/domains_table.html",
        {
            "request": request,
            "domains": domains,
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "base_url": "/partials/domains-table",
            "extra_params": {k: v for k, v in {"q": q, "status": status}.items() if v},
        },
    )


@router.get("/domains/new")
async def domain_create_form(request: Request):
    """Domain creation form."""
    return templates.TemplateResponse(
        "pages/domain_form.html",
        {"request": request, "active_page": "domains", "flash": None, "url": ""},
    )


@router.post("/domains")
async def domain_create_submit(
    request: Request,
    url: str = Form(""),
    probe_fn: Callable[[str], Awaitable[ProbeResult]] = Depends(get_probe_fn),
    session: AsyncSession = Depends(get_db_session),
):
    """Create domain by probing a URL to extract the effective domain."""
    if not url.strip():
        flash = {"type": "error", "message": "URL is required"}
        return templates.TemplateResponse(
            "pages/domain_form.html",
            {"request": request, "active_page": "domains", "flash": flash, "url": url},
        )

    try:
        result = await probe_fn(url.strip())
    except Exception:
        flash = {
            "type": "error",
            "message": "Could not reach URL. Check the address and try again.",
        }
        return templates.TemplateResponse(
            "pages/domain_form.html",
            {"request": request, "active_page": "domains", "flash": flash, "url": url},
        )

    domain_name = result.effective_domain
    if not domain_name:
        flash = {"type": "error", "message": "Could not extract domain from URL."}
        return templates.TemplateResponse(
            "pages/domain_form.html",
            {"request": request, "active_page": "domains", "flash": flash, "url": url},
        )

    # Check if domain already exists
    existing = await session.execute(select(Domain).where(Domain.name == domain_name))
    if existing.scalar_one_or_none():
        return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)

    domain = Domain(name=domain_name)
    session.add(domain)
    audit(session, EventType.DOMAIN_CREATED, domain_name=domain_name, source="dashboard")
    await session.commit()
    return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)


@router.post("/domains/{name}/archive")
async def domain_archive(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Archive a domain from the dashboard."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    if domain.archived_at is None:
        domain.archived_at = datetime.now(UTC)
        audit(session, EventType.DOMAIN_ARCHIVED, domain_name=name, source="dashboard")
        await session.commit()

    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.post("/domains/{name}/restore")
async def domain_restore(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Restore an archived domain from the dashboard."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    domain.archived_at = None
    audit(session, EventType.DOMAIN_RESTORED, domain_name=name, source="dashboard")
    await session.commit()

    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.post("/domains/{name}/delete")
async def domain_delete(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Hard-delete an archived domain with no watches."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    if domain.archived_at is None:
        raise HTTPException(status_code=409, detail="Archive the domain before deleting it")

    watch_result = await session.execute(
        select(Watch).where(Watch.effective_domain == name).limit(1)
    )
    if watch_result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: watches still reference domain '{name}'",
        )

    audit(session, EventType.DOMAIN_DELETED, domain_name=name, source="dashboard")
    await session.delete(domain)
    await session.commit()

    return RedirectResponse(url="/domains", status_code=303)


DOMAIN_FIELD_META: dict[str, dict] = {
    "min_interval": {
        "label": "Min Interval",
        "hint": "Minimum seconds between requests to this domain",
        "type": "number",
        "step": "0.1",
        "min": "0.1",
        "unit": "seconds",
        "format": lambda v: f"{v:.1f}",
        "cast": float,
    },
    "max_concurrency": {
        "label": "Max Concurrency",
        "hint": "Maximum simultaneous requests allowed",
        "type": "number",
        "step": None,
        "min": "1",
        "unit": None,
        "format": lambda v: str(v),
        "cast": int,
    },
    "decay_window": {
        "label": "Decay Window",
        "hint": "Seconds before backoff interval decays toward minimum",
        "type": "number",
        "step": "1",
        "min": "1",
        "unit": "seconds",
        "format": lambda v: f"{v:.0f}",
        "cast": float,
    },
    "notes": {
        "label": "Notes",
        "hint": None,
        "type": "textarea",
        "step": None,
        "min": None,
        "unit": None,
        "format": lambda v: v or "",
        "cast": str,
    },
}
EDITABLE_DOMAIN_FIELDS = set(DOMAIN_FIELD_META.keys())


@router.post("/domains/{name}")
async def domain_inline_update(
    request: Request,
    name: str,
    field: str = Form(""),
    value: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Update a single domain field (inline edit from detail view)."""
    if field not in EDITABLE_DOMAIN_FIELDS:
        raise HTTPException(status_code=400, detail=f"Field '{field}' is not editable")

    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    cast_fn = DOMAIN_FIELD_META[field]["cast"]
    try:
        typed_value: str | int | float = cast_fn(value) if field != "notes" else value
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid value for {field}")

    setattr(domain, field, typed_value)
    audit(session, EventType.DOMAIN_UPDATED, domain_name=name, field=field, source="dashboard")
    await session.commit()
    await session.refresh(domain)

    if request.headers.get("HX-Request") == "true":
        meta = DOMAIN_FIELD_META[field]
        return templates.TemplateResponse(
            "partials/domain_field.html",
            {
                "request": request,
                "domain": domain,
                "field_name": field,
                "field_label": meta["label"],
                "field_hint": meta["hint"],
                "field_value": meta["format"](getattr(domain, field)),
                "field_type": meta["type"],
                "field_step": meta["step"],
                "field_min": meta["min"],
                "field_unit": meta["unit"],
            },
        )
    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.get("/domains/{name}")
async def domain_detail_page(
    request: Request,
    name: str,
    watch_q: str | None = None,
    watch_status: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Domain detail page with config, watches, and danger zone."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    is_active = None
    if watch_status == "active":
        is_active = True
    elif watch_status == "inactive":
        is_active = False

    watches = await get_domain_watches(session, name, search=watch_q, is_active=is_active)

    context = {
        "request": request,
        "active_page": "domains",
        "domain": domain,
        "watches": watches,
        "watch_q": watch_q,
        "watch_status": watch_status,
        "flash": None,
    }
    return templates.TemplateResponse("pages/domain_detail.html", context)


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
    domains = await get_domains_with_watch_counts(session)
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


def _load_snapshot_text(storage: LocalStorage, snapshot, path_attr: str) -> str:
    """Load text content from a snapshot's storage path. Returns empty string on failure."""
    if not snapshot:
        return ""
    path = getattr(snapshot, path_attr, None)
    if not path:
        return ""
    try:
        return storage.load(path).decode(errors="replace")
    except FileNotFoundError:
        return ""


@router.get("/changes/{change_id}")
async def change_detail_page(
    request: Request,
    change_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Change detail page with metadata, chunks, and diff."""
    detail = await get_change_detail(session, change_id)
    if not detail:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    storage = LocalStorage(base_dir=STORAGE_BASE_DIR)
    prev_text = _load_snapshot_text(storage, detail["previous_snapshot"], "text_path")
    curr_text = _load_snapshot_text(storage, detail["current_snapshot"], "text_path")
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
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    storage = LocalStorage(base_dir=STORAGE_BASE_DIR)
    path_attr = "storage_path" if mode == "raw" else "text_path"
    prev_text = _load_snapshot_text(storage, detail["previous_snapshot"], path_attr)
    curr_text = _load_snapshot_text(storage, detail["current_snapshot"], path_attr)
    diff = generate_diff(prev_text, curr_text)
    return templates.TemplateResponse("partials/diff_view.html", {"request": request, "diff": diff})


@router.get("/audit")
async def audit_log_page(
    request: Request,
    event_type: str | None = None,
    watch_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Audit log page with filtering."""
    entries = await get_audit_entries(session, event_type=event_type, watch_id=watch_id)
    context = {
        "request": request,
        "active_page": "audit",
        "entries": entries,
        "event_type": event_type,
    }
    return templates.TemplateResponse("pages/audit_log.html", context)


@router.get("/partials/audit-table")
async def partial_audit_table(
    request: Request,
    event_type: str | None = None,
    watch_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: filtered audit log table."""
    entries = await get_audit_entries(session, event_type=event_type, watch_id=watch_id)
    return templates.TemplateResponse(
        "partials/audit_table.html", {"request": request, "entries": entries}
    )


@router.get("/system")
async def system_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """System monitoring page -- queue health and rate limiter state."""
    queue = await get_queue_health(session)
    domains = await get_domains_with_watch_counts(session)
    context = {
        "request": request,
        "active_page": "system",
        "queue": queue,
        "domains": domains,
    }
    return templates.TemplateResponse("pages/system.html", context)
