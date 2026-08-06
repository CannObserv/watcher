"""Domain dashboard routes — list, detail, lifecycle, cadence, notification defaults."""

import html as html_lib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, get_probe_fn
from src.api.routes.helpers import parse_ulid
from src.api.schemas.content_config import ContentConfig
from src.api.schemas.validators import validate_event_list
from src.core.domains import (
    backfill_domain_schedule_config,
)
from src.core.fetch_policy import clear_tombstone, record_tombstone
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import Domain
from src.core.models.notification_template import (
    VISIBILITY_DOMAIN,
    VISIBILITY_GLOBAL,
    NotificationTemplate,
)
from src.core.models.watched_item import WatchedItem
from src.core.notifications.templates import create_template
from src.core.probe import ProbeResult
from src.core.scheduling.cadence import (
    validate_optional_schedule_config,
)
from src.core.scheduling.resolution import SYSTEM_DEFAULT_SCHEDULE_CONFIG
from src.dashboard.context import (
    build_schedule_map,
    get_active_profiles_by_item,
    get_domain_watched_items,
    get_domains_total_count,
    get_domains_with_watched_item_counts,
)
from src.dashboard.deps import clamp_pagination, is_htmx
from src.dashboard.forms import parse_content_config_from_form
from src.dashboard.templating import templates

# NOTE (#245): dashboard domain mutations do NOT defer a fetch-policy republish
# — the dashboard is decoupled from the task queue (test_import_decoupling), so
# policy changes made here travel on the periodic full-set republish instead
# (publish_fetch_policy, every 5 minutes). Only the tombstone/clear bookkeeping,
# which must land atomically with the Domain row, happens in-request.

router = APIRouter()


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
    page, page_size = clamp_pagination(page, page_size)
    domains = await get_domains_with_watched_item_counts(
        session,
        search=q,
        status=status,
        page=page,
        page_size=page_size,
    )
    total_count = await get_domains_total_count(session, search=q, status=status)
    context = {
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
    return templates.TemplateResponse(request, "pages/domains.html", context)


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
    page, page_size = clamp_pagination(page, page_size)
    domains = await get_domains_with_watched_item_counts(
        session,
        search=q,
        status=status,
        page=page,
        page_size=page_size,
    )
    total_count = await get_domains_total_count(session, search=q, status=status)
    return templates.TemplateResponse(
        request,
        "partials/domains_table.html",
        {
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
        request,
        "pages/domain_form.html",
        {"active_page": "domains", "flash": None, "url": ""},
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
            request,
            "pages/domain_form.html",
            {"active_page": "domains", "flash": flash, "url": url},
        )

    try:
        result = await probe_fn(url.strip())
    except Exception:
        flash = {
            "type": "error",
            "message": "Could not reach URL. Check the address and try again.",
        }
        return templates.TemplateResponse(
            request,
            "pages/domain_form.html",
            {"active_page": "domains", "flash": flash, "url": url},
        )

    domain_name = result.effective_domain
    if not domain_name:
        flash = {"type": "error", "message": "Could not extract domain from URL."}
        return templates.TemplateResponse(
            request,
            "pages/domain_form.html",
            {"active_page": "domains", "flash": flash, "url": url},
        )

    # Check if domain already exists
    existing = await session.execute(select(Domain).where(Domain.name == domain_name))
    if existing.scalar_one_or_none():
        return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)

    domain = Domain(name=domain_name)
    session.add(domain)
    # The host is live again: stop republishing its tombstone, if any (#245).
    await clear_tombstone(session, domain_name)
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
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

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
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

    domain.archived_at = None
    audit(session, EventType.DOMAIN_RESTORED, domain_name=name, source="dashboard")
    await session.commit()

    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.post("/domains/{name}/toggle-active")
async def domain_toggle_active(
    request: Request,
    name: str,
    active: str = Form(""),
    q: str | None = Query(None),
    status: str | None = Query(None),
    sort: str = Query("name"),
    order: str = Query("asc"),
    session: AsyncSession = Depends(get_db_session),
):
    """Toggle domain active status.

    Deactivating suspends every WatchedItem on the domain (``domain_suspended``);
    reactivating clears the flag. ``domain_suspended`` gates scheduling and the
    pause/resume toggle directly — the WatchedItem is the single monitored entity.
    """
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    if domain.archived_at is not None:
        raise HTTPException(status_code=409, detail="Cannot toggle archived domain")

    new_active = active == "true"
    domain.is_active = new_active

    wi_result = await session.execute(select(WatchedItem).where(WatchedItem.domain_name == name))
    for wi in wi_result.scalars().all():
        wi.domain_suspended = not new_active
    audit(
        session,
        EventType.DOMAIN_ACTIVATED if new_active else EventType.DOMAIN_DEACTIVATED,
        domain_name=name,
        source="dashboard",
    )

    await session.commit()
    await session.refresh(domain)

    if is_htmx(request):
        watched_items = await get_domain_watched_items(
            session, name, search=q, sort=sort, order=order, status=status
        )
        now = datetime.now(UTC)
        profiles_by_wi = await get_active_profiles_by_item(session, [wi.id for wi in watched_items])
        return templates.TemplateResponse(
            request,
            "partials/domain_toggle_oob.html",
            {
                "domain": domain,
                "watched_items": watched_items,
                "schedule_map": build_schedule_map(watched_items, now, profiles_by_wi),
                "q": q or "",
                "sort": sort,
                "order": order,
                "status": status or "",
            },
        )
    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.post("/domains/{name}/delete")
async def domain_delete(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Hard-delete an archived domain with no watches.

    Returns 200 + HX-Redirect on success so HTMX navigates the full page rather
    than swapping the redirect response into the #danger-zone-error element.
    Error cases return HTML fragments suitable for innerHTML swap into that target.
    """
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

    if domain.archived_at is None:
        msg = '<p class="text-red-600 text-sm mt-2">Archive the domain before deleting it.</p>'
        return HTMLResponse(status_code=409, content=msg)

    wi_result = await session.execute(
        select(WatchedItem).where(WatchedItem.domain_name == name).limit(1)
    )
    if wi_result.scalar_one_or_none():
        msg = (
            f'<p class="text-red-600 text-sm mt-2">'
            f"Cannot delete: watched items still reference domain '{html_lib.escape(name)}'.</p>"
        )
        return HTMLResponse(status_code=409, content=msg)

    audit(session, EventType.DOMAIN_DELETED, domain_name=name, source="dashboard")
    # Tombstone lands atomically with the delete; the producer keeps
    # republishing it so LWW consumers can revoke the host's policy (#245).
    await record_tombstone(session, name)
    await session.delete(domain)
    await session.commit()

    return HTMLResponse(status_code=200, content="", headers={"HX-Redirect": "/domains"})


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


def _field_context(request: Request, domain: Domain, field_name: str, mode: str = "view") -> dict:
    """Build template context for a single domain field partial."""
    meta = DOMAIN_FIELD_META[field_name]
    return {
        "domain": domain,
        "field_name": field_name,
        "field_label": meta["label"],
        "field_hint": meta["hint"],
        "field_value": meta["format"](getattr(domain, field_name)),
        "field_type": meta["type"],
        "field_step": meta["step"],
        "field_min": meta["min"],
        "field_unit": meta["unit"],
        "field_options": meta.get("options"),
        "field_mode": mode,
    }


def _cadence_field_context(domain: Domain, mode: str = "view") -> dict:
    """Build template context for the domain Default Interval field (#208).

    Mirrors the watched-item interval field's inherited-default display: when the
    domain sets no cadence, view mode shows the system default value with a
    ``· default`` source marker; edit mode binds the explicit override (blank when
    inheriting) and shows the suggestive placeholder.
    """
    interval = (domain.default_schedule_config or {}).get("interval", "")
    return {
        "domain": domain,
        "cadence_interval": interval,
        "cadence_display": interval or SYSTEM_DEFAULT_SCHEDULE_CONFIG["interval"],
        "cadence_inherited": None if interval else "default",
        "cadence_mode": mode,
    }


@router.get("/domains/{name}/cadence-field")
async def domain_cadence_field_partial(
    request: Request,
    name: str,
    mode: Literal["view", "edit"] = "view",
    session: AsyncSession = Depends(get_db_session),
):
    """Serve the domain Default Interval field partial in view or edit mode (#208)."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    if not is_htmx(request):
        return RedirectResponse(url=f"/domains/{name}", status_code=303)

    ctx = _cadence_field_context(domain, mode=mode)
    return templates.TemplateResponse(request, "partials/domain_cadence_field.html", ctx)


@router.get("/domains/{name}/field/{field_name}")
async def domain_field_partial(
    request: Request,
    name: str,
    field_name: str,
    mode: Literal["view", "edit"] = "view",
    session: AsyncSession = Depends(get_db_session),
):
    """Serve a single domain field partial in view or edit mode."""
    if field_name not in EDITABLE_DOMAIN_FIELDS:
        raise HTTPException(status_code=400, detail=f"Field '{field_name}' is not editable")

    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    if not is_htmx(request):
        return RedirectResponse(url=f"/domains/{name}", status_code=303)

    ctx = _field_context(request, domain, field_name, mode=mode)
    return templates.TemplateResponse(request, "partials/domain_field.html", ctx)


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

    if is_htmx(request):
        ctx = _field_context(request, domain, field, mode="view")
        return templates.TemplateResponse(request, "partials/domain_field.html", ctx)
    return RedirectResponse(url=f"/domains/{name}", status_code=303)


async def _render_domain_detail(
    request: Request,
    domain: Domain,
    session: AsyncSession,
    *,
    flash: dict | None = None,
    q: str | None = None,
    status: str | None = None,
    sort: str = "name",
    order: str = "asc",
    status_code: int = 200,
):
    """Render the domain detail page. Shared by the GET route and error re-renders."""
    watched_items = await get_domain_watched_items(
        session, domain.name, search=q, sort=sort, order=order, status=status
    )
    # Two counts: total (archived-inclusive) gates domain deletion — archived items
    # still hold the domain_name FK; live (non-archived) is the heading number, which
    # matches the Domains-list column (#209 CR).
    all_watched_items_count, live_watched_items_count = (
        await session.execute(
            select(
                func.count(WatchedItem.id),
                func.count(WatchedItem.id).filter(WatchedItem.archived_at.is_(None)),
            ).where(WatchedItem.domain_name == domain.name)
        )
    ).one()
    now = datetime.now(UTC)
    profiles_by_wi = await get_active_profiles_by_item(session, [wi.id for wi in watched_items])
    field_contexts = {
        fname: _field_context(request, domain, fname, mode="view") for fname in DOMAIN_FIELD_META
    }
    context = {
        "active_page": "domains",
        "domain": domain,
        "watched_items": watched_items,
        "schedule_map": build_schedule_map(watched_items, now, profiles_by_wi),
        "all_watched_items_count": all_watched_items_count,
        "live_watched_items_count": live_watched_items_count,
        "q": q or "",
        "sort": sort,
        "order": order,
        "status": status or "",
        "flash": flash,
        "field_contexts": field_contexts,
        **_cadence_field_context(domain),
    }
    return templates.TemplateResponse(
        request, "pages/domain_detail.html", context, status_code=status_code
    )


@router.post("/domains/{name}/default-schedule-config")
async def domain_default_schedule_config_update(
    request: Request,
    name: str,
    interval: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Set or clear a domain's default check cadence and back-fill its items (#205).

    A blank ``interval`` clears the cadence (items fall back to the system
    default). A non-blank value is stored as ``{"interval": <value>}`` after
    validation; a malformed interval re-renders the detail page with an error
    flash (status 400). Re-denormalizes onto every WatchedItem on the domain.
    """
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    interval = interval.strip()
    config = {"interval": interval} if interval else None
    try:
        config = validate_optional_schedule_config(config)
    except ValueError as exc:
        return await _render_domain_detail(
            request,
            domain,
            session,
            flash={"type": "error", "message": str(exc)},
            status_code=400,
        )

    domain.default_schedule_config = config
    await backfill_domain_schedule_config(session, name, config)
    audit(
        session,
        EventType.DOMAIN_UPDATED,
        domain_name=name,
        default_schedule_config=config,
        source="dashboard",
    )
    await session.commit()
    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.get("/domains/{name}")
async def domain_detail_page(
    request: Request,
    name: str,
    q: str | None = None,
    status: str | None = None,
    sort: str = "name",
    order: str = "asc",
    session: AsyncSession = Depends(get_db_session),
):
    """Domain detail page with config, watches, and danger zone."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

    return await _render_domain_detail(
        request, domain, session, q=q, status=status, sort=sort, order=order
    )


async def _render_domain_templates(request: Request, domain_name: str, session: AsyncSession):
    """Render the domain_domain_templates partial for *domain_name* (#200).

    Post-#200 a domain's templates are ``NotificationTemplate`` rows with
    ``visibility='domain'`` — there is no assign-existing flow (a template has one
    intrinsic scope). Globals are shown read-only as inherited context.
    """
    assigned_result = await session.execute(
        select(NotificationTemplate)
        .where(
            NotificationTemplate.visibility == VISIBILITY_DOMAIN,
            NotificationTemplate.domain_name == domain_name,
        )
        .order_by(NotificationTemplate.title)
    )
    assigned = assigned_result.scalars().all()

    global_result = await session.execute(
        select(NotificationTemplate)
        .where(NotificationTemplate.visibility == VISIBILITY_GLOBAL)
        .order_by(NotificationTemplate.title)
    )
    global_templates = global_result.scalars().all()

    return templates.TemplateResponse(
        request,
        "partials/domain_templates.html",
        {
            "domain_name": domain_name,
            "assigned": assigned,
            "global_templates": global_templates,
        },
    )


@router.get("/domains/{domain_name}/notifications/new")
async def domain_notification_new_page(
    request: Request,
    domain_name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Full page: create a new notification template for a domain."""
    domain = await session.scalar(select(Domain).where(Domain.name == domain_name))
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    return templates.TemplateResponse(
        request,
        "pages/domain_notification_new.html",
        {
            "domain_name": domain_name,
            "title": None,
            "events": None,
            "content_config": None,
            "error": None,
        },
    )


@router.post("/domains/{domain_name}/notifications/new")
async def domain_notification_create(
    request: Request,
    domain_name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a NotificationTemplate and link to domain. Redirects on success."""
    domain = await session.scalar(select(Domain).where(Domain.name == domain_name))
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    form = await request.form()
    title = str(form.get("title") or "").strip()
    events = form.getlist("events")
    remote_channel_id = str(form.get("remote_channel_id") or "").strip()
    channel_hint = str(form.get("channel_hint") or "").strip() or "remote"

    _cc = parse_content_config_from_form(form)
    _parsed_config = ContentConfig.model_validate(_cc) if _cc else None

    def _page_error(msg: str):
        return templates.TemplateResponse(
            request,
            "pages/domain_notification_new.html",
            {
                "domain_name": domain_name,
                "title": title,
                "events": events,
                "content_config": _parsed_config,
                "error": msg,
            },
        )

    if not title:
        return _page_error("Title is required.")
    if not remote_channel_id:
        return _page_error("Remote channel ID is required.")

    try:
        validate_event_list(events)
    except ValueError as exc:
        return _page_error(str(exc))

    await create_template(
        session,
        visibility=VISIBILITY_DOMAIN,
        domain_name=domain_name,
        title=title,
        channel_hint=channel_hint,
        events=events,
        content_config=_cc,
        remote_channel_id=remote_channel_id,
        audit_fields={
            "title": title,
            "channel_hint": channel_hint,
            "source": "domain_dashboard",
            "domain_name": domain_name,
        },
    )
    await session.commit()
    return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)


@router.get("/domains/{domain_name}/domain-templates")
async def domain_templates_partial(
    request: Request,
    domain_name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: notification defaults assigned to a domain."""
    return await _render_domain_templates(request, domain_name, session)


@router.post("/domains/{domain_name}/domain-templates/remove/{template_id}")
async def domain_template_remove(
    request: Request,
    domain_name: str,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a domain-scoped notification template (#200: removal = delete the row)."""
    if not is_htmx(request):
        return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)
    tpl = await session.get(NotificationTemplate, parse_ulid(template_id, "Template"))
    if tpl and tpl.visibility == VISIBILITY_DOMAIN and tpl.domain_name == domain_name:
        await session.delete(tpl)
        audit(
            session,
            EventType.NOTIFICATION_TEMPLATE_DELETED,
            domain_name=domain_name,
            template_id=template_id,
        )
        await session.commit()
    return await _render_domain_templates(request, domain_name, session)


@router.get("/partials/domain-watched-items/{name}")
async def partial_domain_watched_items(
    request: Request,
    name: str,
    q: str | None = None,
    status: str | None = None,
    sort: str = "name",
    order: str = "asc",
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: domain WatchedItems table with search, sort, and status filter."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404)
    watched_items = await get_domain_watched_items(
        session, name, search=q, sort=sort, order=order, status=status
    )
    now = datetime.now(UTC)
    profiles_by_wi = await get_active_profiles_by_item(session, [wi.id for wi in watched_items])
    return templates.TemplateResponse(
        request,
        "partials/domain_watched_items_table.html",
        {
            "domain": domain,
            "watched_items": watched_items,
            "schedule_map": build_schedule_map(watched_items, now, profiles_by_wi),
            "q": q or "",
            "sort": sort,
            "order": order,
            "status": status or "",
        },
    )
