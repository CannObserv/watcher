"""Dashboard page routes — server-rendered HTML via Jinja2 + HTMX."""

import html as html_lib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import TemplateError
from notifier_client.errors import NotifierError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session, get_probe_fn
from src.api.routes.helpers import parse_ulid
from src.api.routes.watched_items import (
    archive_watched_item as _api_archive_watched_item,
)
from src.api.routes.watched_items import (
    check_now as _api_check_now,
)
from src.api.routes.watched_items import (
    mark_reviewed as _api_mark_reviewed,
)
from src.api.routes.watched_items import (
    restore_watched_item as _api_restore_watched_item,
)
from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.api.schemas.validators import validate_event_list
from src.core.domains import ensure_domain_and_resolve_suspension
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import Domain
from src.core.models.notification_template import (
    VISIBILITY_DOMAIN,
    VISIBILITY_GLOBAL,
    VISIBILITY_WATCHED_ITEM,
    NotificationTemplate,
)
from src.core.models.watched_item import ContentType, WatchedItem
from src.core.notifications.content import build_body, build_title, resolve_options
from src.core.notifications.default_templates import (
    compose_body_prefill,
    compose_title_prefill,
)
from src.core.notifications.events import EVENT_TITLES, WatchEvent, WatchEventType
from src.core.notifications.notify import (
    DispatchCandidate,
    dispatch_via_notifier,
)
from src.core.notifications.preview_fixtures import (
    build_preview_event,
    compute_preview_unified_diff,
)
from src.core.notifier_client import get_notifier_client
from src.core.probe import ProbeResult
from src.core.scheduler import compute_next_check, parse_interval
from src.core.watches.resolution import resolved_schedule_config
from src.dashboard import templates
from src.dashboard.context import (
    get_audit_entries,
    get_dashboard_stats,
    get_domain_default_templates,
    get_domain_watched_items,
    get_domains_total_count,
    get_domains_with_watched_item_counts,
    get_global_default_templates,
    get_queue_health,
    get_watched_item_activity,
    get_watched_item_detail,
    get_watched_item_list,
    get_watched_item_notifications,
    get_watched_item_profiles,
    get_watched_items_total_count,
)
from src.dashboard.deps import get_dashboard_user

router = APIRouter(tags=["dashboard"], dependencies=[Depends(get_dashboard_user)])
logger = get_logger(__name__)

_ALL_EVENT_TYPE_VALUES: list[str] = [e.value for e in WatchEventType]


def _status_to_is_active(status: str | None) -> bool | None:
    """Convert status string param to is_active bool for DB queries."""
    if status == "active":
        return True
    if status == "inactive":
        return False
    return None


def _parse_content_config_from_form(form) -> dict | None:
    """Extract content_config fields from a flat form POST dict."""
    _lines_raw = form.get("content_config__diff_snippet_lines", "25")
    try:
        _lines = max(1, min(200, int(_lines_raw)))
    except (ValueError, TypeError):
        _lines = 25
    title_template = form.get("content_config__title_template", "").strip() or None
    body_template = form.get("content_config__body_template", "").strip() or None
    opts = ContentOptions(
        include_diff_snippet="content_config__include_diff_snippet" in form,
        include_diff_full="content_config__include_diff_full" in form,
        include_temporal_context="content_config__include_temporal_context" in form,
        include_domain="content_config__include_domain" in form,
        diff_snippet_lines=_lines,
        include_last_changed_at="content_config__include_last_changed_at" in form,
        include_significance="content_config__include_significance" in form,
        include_change_dashboard_url="content_config__include_change_dashboard_url" in form,
        include_tags="content_config__include_tags" in form,
        include_description="content_config__include_description" in form,
        title_template=title_template,
        body_template=body_template,
    )
    # Only store if at least one toggle is enabled or a template string is provided.
    any_enabled = (
        opts.include_diff_snippet
        or opts.include_diff_full
        or opts.include_temporal_context
        or opts.include_domain
        or opts.include_last_changed_at
        or opts.include_significance
        or opts.include_change_dashboard_url
        or opts.include_tags
        or opts.include_description
        or opts.title_template
        or opts.body_template
    )
    # Parse per-event overrides
    overrides: dict[str, ContentOptions] = {}
    for et_value in _ALL_EVENT_TYPE_VALUES:
        prefix = f"content_config__override__{et_value}__"
        et_opts = ContentOptions(
            include_diff_snippet=f"{prefix}include_diff_snippet" in form,
            include_diff_full=f"{prefix}include_diff_full" in form,
            include_temporal_context=f"{prefix}include_temporal_context" in form,
            include_domain=f"{prefix}include_domain" in form,
            include_last_changed_at=f"{prefix}include_last_changed_at" in form,
            include_significance=f"{prefix}include_significance" in form,
            include_change_dashboard_url=f"{prefix}include_change_dashboard_url" in form,
            include_tags=f"{prefix}include_tags" in form,
            include_description=f"{prefix}include_description" in form,
        )
        if any(
            x
            for x in (
                et_opts.include_diff_snippet,
                et_opts.include_diff_full,
                et_opts.include_temporal_context,
                et_opts.include_domain,
                et_opts.include_last_changed_at,
                et_opts.include_significance,
                et_opts.include_change_dashboard_url,
                et_opts.include_tags,
                et_opts.include_description,
            )
        ):
            overrides[et_value] = et_opts

    if not any_enabled and not overrides:
        return None
    return ContentConfig(default=opts, overrides=overrides).model_dump()


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


# --- WatchedItem inline field editing ---


def _format_interval(wi: WatchedItem) -> str:
    cfg = wi.default_schedule_config or {}
    return cfg.get("interval") or ""


def _format_content_type(wi: WatchedItem) -> str:
    return wi.default_content_type or ""


def _interval_display(wi: WatchedItem, value: str) -> tuple[str, bool]:
    """View-mode display for the interval field; returns ``(display, inherited)``.

    An explicit interval shows verbatim. No config at all (``None``) shows the
    resolved system default, flagged inherited. A non-None config that carries no
    interval (e.g. ``{}``) shows a literal empty-config marker rather than a blank
    value beside an "inherited" tag — that pairing read as confusing UX (#202 CR).
    """
    if value:
        return value, False
    if wi.default_schedule_config is None:
        return resolved_schedule_config(wi).get("interval", ""), True
    return "{ }", False


WATCHED_ITEM_FIELD_META: dict[str, dict] = {
    "name": {
        "label": "Name",
        "hint": None,
        "type": "text",
        "source": "column",
        "cast": lambda v: v.strip(),
        "format": lambda wi: wi.name,
    },
    "description": {
        "label": "Description",
        "hint": "Optional notes for operators",
        "type": "textarea",
        "source": "column",
        "cast": lambda v: v.strip() or None,
        "format": lambda wi: wi.description or "",
    },
    "default_schedule_interval": {
        "label": "Interval",
        "hint": "e.g. 30s, 15m, 6h, 1d. Leave blank to inherit the system default. "
        "reduce_frequency post-actions may slow this independently.",
        "type": "text",
        "source": "schedule_interval",
        "cast": lambda v: v.strip(),
        "format": _format_interval,
        "display": _interval_display,
    },
    "default_content_type": {
        "label": "Content Type",
        "hint": None,
        "type": "select",
        "source": "column",
        "cast": lambda v: v.strip() or None,
        "format": _format_content_type,
        "options": [("", "—"), ("html", "HTML"), ("pdf", "PDF")],
    },
}

EDITABLE_WATCHED_ITEM_FIELDS = set(WATCHED_ITEM_FIELD_META.keys())


def _watched_item_field_context(
    request: Request, wi: WatchedItem, field_name: str, mode: str = "view"
) -> dict:
    meta = WATCHED_ITEM_FIELD_META[field_name]
    value = meta["format"](wi)
    # A field may supply a "display" callable returning (view_value, inherited) —
    # e.g. the interval field resolves the inherited system default for view mode
    # while edit mode still binds the explicit override. Others view == edit.
    display_fn = meta.get("display")
    display_value, inherited = display_fn(wi, value) if display_fn else (value, False)
    return {
        "watched_item": wi,
        "field_name": field_name,
        "field_label": meta["label"],
        "field_hint": meta.get("hint"),
        "field_value": value,
        "field_display_value": display_value,
        "field_inherited": inherited,
        "field_type": meta["type"],
        "field_options": meta.get("options"),
        "field_mode": mode,
    }


def _apply_watched_item_field_update(wi: WatchedItem, field_name: str, raw_value: str) -> None:
    meta = WATCHED_ITEM_FIELD_META[field_name]
    cast_fn = meta["cast"]
    typed_value = cast_fn(raw_value)
    source = meta["source"]
    if source == "column":
        setattr(wi, field_name, typed_value)
    elif source == "schedule_interval":
        if not typed_value:
            wi.default_schedule_config = None
        else:
            # Validate interval shape
            parse_interval(typed_value)
            wi.default_schedule_config = {
                **(wi.default_schedule_config or {}),
                "interval": typed_value,
            }


@router.get("/watched-items/new")
async def watched_item_create_form(request: Request):
    """Standalone WatchedItem create form."""
    return templates.TemplateResponse(
        request,
        "pages/watched_item_form.html",
        {
            "active_page": "watched-items",
            "flash": None,
            "content_types": list(ContentType),
        },
    )


@router.post("/watched-items/new")
async def watched_item_create_submit(
    request: Request,
    url: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    default_schedule_interval: str = Form(""),
    default_content_type: str = Form(""),
    default_tags: str = Form(""),
    is_active: str = Form(""),
    probe_fn: Callable[[str], Awaitable[ProbeResult]] = Depends(get_probe_fn),
    session: AsyncSession = Depends(get_db_session),
):
    """Create a standalone WatchedItem from the dashboard form.

    Accepts a URL directly; probes it for effective_url + domain_name.
    Unchecking ``is_active`` provisions the item paused (#188). Audit row uses
    ``source="dashboard"``.
    """

    async def _render_with_flash(message: str, level: str = "error"):
        return templates.TemplateResponse(
            request,
            "pages/watched_item_form.html",
            {
                "active_page": "watched-items",
                "flash": {"type": level, "message": message},
                "content_types": list(ContentType),
            },
        )

    url_raw = url.strip()
    if not url_raw:
        return await _render_with_flash("URL is required")

    interval_raw = default_schedule_interval.strip()
    if interval_raw:
        try:
            parse_interval(interval_raw)
        except ValueError as exc:
            return await _render_with_flash(str(exc))

    ct_raw = default_content_type.strip() or None
    if ct_raw is not None:
        try:
            ContentType(ct_raw)
        except ValueError:
            return await _render_with_flash(f"Invalid content type: {ct_raw!r}")

    tags = [t.strip() for t in default_tags.split(",") if t.strip()] or None
    if tags and any(len(t) > 255 for t in tags):
        return await _render_with_flash("Tag too long (max 255 characters each)")

    try:
        probe_result = await probe_fn(url_raw)
    except httpx.HTTPError as exc:
        return await _render_with_flash(f"URL unreachable: {exc}")

    # #196 Finding 1: upsert the domain and seed domain_suspended from its state —
    # schedule_tick gates solely on WatchedItem.domain_suspended, so a create on an
    # already-suspended domain must not silently arm fetching. Shared with the API paths.
    domain_suspended = await ensure_domain_and_resolve_suspension(
        session, probe_result.effective_domain or None
    )

    wi_name = name.strip() or probe_result.effective_domain or url_raw
    wi = WatchedItem(
        effective_url=probe_result.effective_url,
        domain_name=probe_result.effective_domain or None,
        domain_suspended=domain_suspended,
        name=wi_name,
        description=description.strip() or None,
        default_schedule_config={"interval": interval_raw} if interval_raw else None,
        default_content_type=ct_raw,
        default_tags=tags,
        is_active=(is_active == "true"),
    )
    session.add(wi)
    await session.flush()
    audit(
        session,
        EventType.WATCHED_ITEM_CREATED,
        watched_item_id=str(wi.id),
        name=wi.name,
        source="dashboard",
    )
    await session.commit()
    return RedirectResponse(url=f"/watched-items/{wi.id}", status_code=303)


def _watched_item_extra_params(q: str | None, include_archived: bool) -> dict[str, str]:
    return {
        k: v
        for k, v in {"q": q, "include_archived": "true" if include_archived else None}.items()
        if v
    }


def _build_next_check_map(
    watched_items: list[WatchedItem], now: datetime
) -> dict[str, datetime | None]:
    """Next-check datetime per item, or None when never checked (#204).

    Resolves the schedule through ``resolved_schedule_config`` (WatchedItem
    default → system default) so an item with no explicit config still yields a
    next-check time instead of a blank — matching the detail page.
    """
    result: dict[str, datetime | None] = {}
    for wi in watched_items:
        if wi.last_checked_at is not None:
            result[str(wi.id)] = compute_next_check(
                resolved_schedule_config(wi), wi.last_checked_at, now=now
            )
        else:
            result[str(wi.id)] = None
    return result


def _build_interval_map(
    watched_items: list[WatchedItem],
) -> dict[str, tuple[str, bool]]:
    """Resolved interval display per item as ``(text, inherited)`` (#204).

    Routes through the same resolution + display logic as the detail page
    (``_interval_display``) so the list shows the inherited system default
    rather than a blank when an item carries no explicit schedule config.
    """
    return {str(wi.id): _interval_display(wi, _format_interval(wi)) for wi in watched_items}


@router.get("/watched-items")
async def watched_items_page(
    request: Request,
    q: str | None = None,
    include_archived: bool = False,
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_db_session),
):
    """List page for WatchedItems."""
    watched_items = await get_watched_item_list(
        session,
        search=q,
        include_archived=include_archived,
        page=page,
        page_size=page_size,
    )
    total_count = await get_watched_items_total_count(
        session, search=q, include_archived=include_archived
    )
    now = datetime.now(UTC)
    return templates.TemplateResponse(
        request,
        "pages/watched_items.html",
        {
            "request": request,
            "active_page": "watched-items",
            "watched_items": watched_items,
            "next_check_map": _build_next_check_map(watched_items, now),
            "interval_map": _build_interval_map(watched_items),
            "include_archived": include_archived,
            "q": q or "",
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "base_url": "/partials/watched-items-table",
            "hx_target": "#watched-items-table-container",
            "hx_include": "[name='q'],[name='include_archived']",
            "extra_params": _watched_item_extra_params(q, include_archived),
            "flash": None,
        },
    )


@router.get("/partials/watched-items-table")
async def partial_watched_items_table(
    request: Request,
    q: str | None = None,
    include_archived: bool = False,
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: watched-items table with search, filter, and pagination."""
    watched_items = await get_watched_item_list(
        session,
        search=q,
        include_archived=include_archived,
        page=page,
        page_size=page_size,
    )
    total_count = await get_watched_items_total_count(
        session, search=q, include_archived=include_archived
    )
    now = datetime.now(UTC)
    return templates.TemplateResponse(
        request,
        "partials/watched_items_table.html",
        {
            "watched_items": watched_items,
            "next_check_map": _build_next_check_map(watched_items, now),
            "interval_map": _build_interval_map(watched_items),
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "base_url": "/partials/watched-items-table",
            "hx_target": "#watched-items-table-container",
            "hx_include": "[name='q'],[name='include_archived']",
            "extra_params": _watched_item_extra_params(q, include_archived),
        },
    )


@router.get("/watched-items/{watched_item_id}")
async def watched_item_detail_page(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Detail page for a WatchedItem."""
    wi = await get_watched_item_detail(session, watched_item_id)
    if wi is None:
        return templates.TemplateResponse(
            request, "pages/404.html", {"request": request}, status_code=404
        )

    item_templates = await get_watched_item_notifications(session, wi.id)
    global_templates = await get_global_default_templates(session)
    domain_templates = await get_domain_default_templates(session, wi.domain_name)
    profiles = await get_watched_item_profiles(session, wi.id)
    activity = await get_watched_item_activity(session, watched_item_id)

    field_contexts = {
        name: _watched_item_field_context(request, wi, name, mode="view")
        for name in ("name", "description", "default_schedule_interval", "default_content_type")
    }

    return templates.TemplateResponse(
        request,
        "pages/watched_item_detail.html",
        {
            "request": request,
            "active_page": "watched-items",
            "watched_item": wi,
            "flash": None,
            "field_contexts": field_contexts,
            "templates": item_templates,
            "global_templates": global_templates,
            "domain_templates": domain_templates,
            "profiles": profiles,
            "activity": activity,
        },
    )


@router.post("/watched-items/{watched_item_id}/archive")
async def watched_item_archive(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Dashboard archive — cascades to child Watches (delegates to shared logic)."""
    await _api_archive_watched_item(watched_item_id, session)
    if request.headers.get("HX-Request") == "true":
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/watched-items/{watched_item_id}"},
        )
    return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)


@router.post("/watched-items/{watched_item_id}/restore")
async def watched_item_restore(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Dashboard restore — parent only."""
    await _api_restore_watched_item(watched_item_id, session)
    if request.headers.get("HX-Request") == "true":
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/watched-items/{watched_item_id}"},
        )
    return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)


@router.post("/watched-items/{watched_item_id}/mark-reviewed")
async def watched_item_mark_reviewed(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Stamp last_reviewed_at = now() on a WatchedItem.

    Dashboard UI for this is pending (#185 Phase A removed the sub_aspect banner
    that contained the only form). Callable via direct POST or the API route.
    """
    await _api_mark_reviewed(watched_item_id, session)
    if request.headers.get("HX-Request") == "true":
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/watched-items/{watched_item_id}"},
        )
    return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)


@router.post("/watched-items/{watched_item_id}/toggle-active")
async def watched_item_toggle_active(
    request: Request,
    watched_item_id: str,
    active: str = Form(""),
    toggle_id: str = Form("watched-item-status-toggle"),
    compact: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Pause/resume a WatchedItem via the dashboard toggle (#188/#189).

    Mirrors the API PATCH ``is_active`` semantics: an archived item rejects the
    toggle (restore owns activation), and resume is blocked while the domain is
    suspended (kill-switch parity with the Watch toggle). Emits the dedicated
    ``WATCHED_ITEM_PAUSED`` / ``WATCHED_ITEM_RESUMED`` audit events. Guard
    rejections re-render the toggle in its true state with an OOB flash (HTMX)
    or redirect back to detail (non-HTMX).
    """
    hx = request.headers.get("HX-Request") == "true"
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")

    def _respond(flash: tuple[str, str] | None = None):
        if not hx:
            return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)
        # On the detail page (non-compact), OOB-sync the Check-now button so its
        # disabled state tracks the new pause/resume status.
        ctx = {
            "watched_item": wi,
            "toggle_id": toggle_id,
            "compact": bool(compact),
            "oob_check_now": not bool(compact),
        }
        if flash:
            ctx["flash_oob_level"], ctx["flash_oob_message"] = flash
        return templates.TemplateResponse(request, "partials/watched_item_status_toggle.html", ctx)

    if wi.archived_at is not None:
        return _respond(("error", "Watched Item is archived — restore it to change its status."))

    new_active = active == "true"
    if wi.domain_suspended and new_active:
        return _respond(("warning", "Cannot resume while the domain is suspended."))

    if wi.is_active != new_active:
        wi.is_active = new_active
        audit(
            session,
            EventType.WATCHED_ITEM_RESUMED if new_active else EventType.WATCHED_ITEM_PAUSED,
            watched_item_id=str(wi.id),
            source="dashboard",
        )
        await session.commit()
        await session.refresh(wi)

    return _respond()


@router.post("/watched-items/{watched_item_id}/check-now")
async def watched_item_check_now(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Enqueue an immediate check for a WatchedItem (delegates to the API route).

    The API enforces the pre-flight guards (409 archived/paused, 422 empty
    effective_url). For HTMX, success and guard failures surface as an OOB
    flash; non-HTMX clients get a redirect back to the detail page.
    """
    hx = request.headers.get("HX-Request") == "true"
    try:
        await _api_check_now(watched_item_id, session)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
        if not hx:
            return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)
        return templates.TemplateResponse(
            request,
            "partials/flash_oob.html",
            {"flash_oob_level": "error", "flash_oob_message": str(exc.detail)},
        )
    if not hx:
        return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)
    return templates.TemplateResponse(
        request,
        "partials/flash_oob.html",
        {"flash_oob_level": "success", "flash_oob_message": "Check queued."},
    )


@router.get("/watched-items/{watched_item_id}/effective-url/field")
async def watched_item_url_field_partial(
    request: Request,
    watched_item_id: str,
    mode: Literal["view", "edit"] = "view",
    session: AsyncSession = Depends(get_db_session),
):
    """Serve the WatchedItem URL field partial in view or edit mode.

    Powers the inline Edit affordance on the detail page's URL row; the edit
    form posts to the sibling ``/effective-url`` route which re-probes.
    """
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_url_field.html",
        {"watched_item": wi, "url_mode": mode},
    )


@router.post("/watched-items/{watched_item_id}/effective-url")
async def watched_item_update_url(
    request: Request,
    watched_item_id: str,
    url: str = Form(""),
    probe_fn: Callable[[str], Awaitable[ProbeResult]] = Depends(get_probe_fn),
    session: AsyncSession = Depends(get_db_session),
):
    """Re-probe a new URL and update the WatchedItem's effective_url + domain_name.

    Mirrors the create-time probe path: the submitted URL is probed for its
    canonical effective_url and domain, the Domain row is created if new, and
    ``source_specs`` are left untouched. Rejects archived items. ``domain_suspended``
    is re-evaluated against the target Domain so a re-probe can't silently re-arm
    fetching against a suspended domain — and if the target is suspended the
    operator gets a warning flash instead of the success reload. Probe failures
    surface as a flash.
    """
    hx = request.headers.get("HX-Request") == "true"
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")

    def _flash(message: str, level: str):
        return templates.TemplateResponse(
            request,
            "partials/flash_oob.html",
            {"flash_oob_level": level, "flash_oob_message": message},
        )

    if wi.archived_at is not None:
        return _flash("Cannot change the URL of an archived item.", "error")

    url_raw = url.strip()
    if not url_raw:
        return _flash("URL is required.", "error")

    try:
        probe_result = await probe_fn(url_raw)
    except httpx.HTTPError as exc:
        return _flash(f"URL unreachable: {exc}", "error")

    # Upsert the domain and re-evaluate suspension against the (possibly new) target
    # so moving a WatchedItem onto a suspended/archived domain doesn't bypass the
    # kill-switch. Shared with the API create/PATCH paths (#196).
    domain_suspended = await ensure_domain_and_resolve_suspension(
        session, probe_result.effective_domain or None
    )

    wi.effective_url = probe_result.effective_url
    wi.domain_name = probe_result.effective_domain or None
    wi.domain_suspended = domain_suspended
    audit(
        session,
        EventType.WATCHED_ITEM_UPDATED,
        watched_item_id=str(wi.id),
        updated_fields=["effective_url", "domain_name"],
        source="dashboard",
    )
    await session.commit()

    if domain_suspended and hx:
        return _flash(
            f"URL updated, but '{probe_result.effective_domain}' is suspended — "
            "this Watched Item will not be checked until the domain is reactivated.",
            "warning",
        )

    if hx:
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/watched-items/{watched_item_id}"},
        )
    return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)


@router.get("/watched-items/{watched_item_id}/field/{field_name}")
async def watched_item_field_partial(
    request: Request,
    watched_item_id: str,
    field_name: str,
    mode: Literal["view", "edit"] = "view",
    session: AsyncSession = Depends(get_db_session),
):
    """Serve a single WatchedItem field partial in view or edit mode."""
    if field_name not in EDITABLE_WATCHED_ITEM_FIELDS:
        raise HTTPException(status_code=400, detail=f"Field '{field_name}' is not editable")

    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")

    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)

    ctx = _watched_item_field_context(request, wi, field_name, mode=mode)
    return templates.TemplateResponse(request, "partials/watched_item_field.html", ctx)


@router.post("/watched-items/{watched_item_id}/field/{field_name}")
async def watched_item_field_update(
    request: Request,
    watched_item_id: str,
    field_name: str,
    value: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Update a single WatchedItem field (HTMX inline edit)."""
    if field_name not in EDITABLE_WATCHED_ITEM_FIELDS:
        raise HTTPException(status_code=400, detail=f"Field '{field_name}' is not editable")

    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")

    if field_name == "name" and not value.strip():
        raise HTTPException(status_code=400, detail="Name cannot be empty")

    try:
        _apply_watched_item_field_update(wi, field_name, value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid value: {exc}") from exc

    audit(
        session,
        EventType.WATCHED_ITEM_UPDATED,
        watched_item_id=str(wi.id),
        updated_fields=[field_name],
        source="dashboard",
    )
    await session.commit()
    await session.refresh(wi)

    if request.headers.get("HX-Request") == "true":
        ctx = _watched_item_field_context(request, wi, field_name, mode="view")
        return templates.TemplateResponse(request, "partials/watched_item_field.html", ctx)
    return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)


@router.get("/watched-items/{watched_item_id}/tags")
async def watched_item_tags_partial(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    return templates.TemplateResponse(
        request,
        "partials/watched_item_tags_editor.html",
        {"watched_item": wi},
    )


@router.post("/watched-items/{watched_item_id}/tags")
async def watched_item_tag_add(
    request: Request,
    watched_item_id: str,
    tag: str = Form(...),
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    tags_raw = [t.strip() for t in tag.split(",") if t.strip()]
    if not tags_raw:
        raise HTTPException(status_code=400, detail="No valid tags provided")
    if any(len(t) > 255 for t in tags_raw):
        raise HTTPException(status_code=400, detail="Tag too long (max 255 characters each)")
    current = list(wi.default_tags or [])
    added = [t for t in tags_raw if t not in current]
    if added:
        current.extend(added)
        wi.default_tags = sorted(current)
        audit(
            session,
            EventType.WATCHED_ITEM_UPDATED,
            watched_item_id=str(wi.id),
            updated_fields=["default_tags"],
            tags_added=added,
            source="dashboard",
        )
        await session.commit()
        await session.refresh(wi)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_tags_editor.html",
        {"watched_item": wi},
    )


@router.delete("/watched-items/{watched_item_id}/tags/{tag}")
async def watched_item_tag_remove(
    request: Request,
    watched_item_id: str,
    tag: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    current = list(wi.default_tags or [])
    if tag in current:
        current.remove(tag)
        wi.default_tags = current or None
        audit(
            session,
            EventType.WATCHED_ITEM_UPDATED,
            watched_item_id=str(wi.id),
            updated_fields=["default_tags"],
            tag_removed=tag,
            source="dashboard",
        )
        await session.commit()
        await session.refresh(wi)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_tags_editor.html",
        {"watched_item": wi},
    )


async def _item_template_or_404(
    session: AsyncSession, wi: WatchedItem, tpl_id: str
) -> NotificationTemplate:
    """Fetch an item-scoped NotificationTemplate, 404 if absent or not on this item."""
    tpl = await session.get(NotificationTemplate, parse_ulid(tpl_id))
    if not tpl or tpl.visibility != VISIBILITY_WATCHED_ITEM or tpl.watched_item_id != wi.id:
        raise HTTPException(status_code=404)
    return tpl


@router.get("/partials/watched-item-templates/{watched_item_id}")
async def watched_item_templates_partial(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    item_templates = await get_watched_item_notifications(session, wi.id)
    global_templates = await get_global_default_templates(session)
    domain_templates = await get_domain_default_templates(session, wi.domain_name)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_templates.html",
        {
            "watched_item": wi,
            "templates": item_templates,
            "global_templates": global_templates,
            "domain_templates": domain_templates,
        },
    )


@router.get("/watched-items/{watched_item_id}/templates/new")
async def watched_item_template_new_form(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_template_form.html",
        {"watched_item": wi, "tpl": None, "event_titles": EVENT_TITLES},
    )


@router.post("/watched-items/{watched_item_id}/templates")
async def watched_item_template_create(
    request: Request,
    watched_item_id: str,
    title: str = Form(""),
    channel_hint: str = Form(...),
    events: str = Form("change_detected"),
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404)
    event_list = [e.strip() for e in events.split(",") if e.strip()]
    try:
        event_list = validate_event_list(event_list)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tpl = NotificationTemplate(
        visibility=VISIBILITY_WATCHED_ITEM,
        watched_item_id=wi.id,
        title=title.strip() or channel_hint.strip(),
        channel_hint=channel_hint.strip(),
        events=event_list,
    )
    session.add(tpl)
    await session.flush()
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_CREATED,
        watched_item_id=str(wi.id),
        template_id=str(tpl.id),
        source="dashboard",
    )
    await session.commit()

    refreshed = await get_watched_item_notifications(session, wi.id)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_template_rows.html",
        {"watched_item": wi, "templates": refreshed},
    )


@router.get("/watched-items/{watched_item_id}/templates/{tpl_id}/edit")
async def watched_item_template_edit_form(
    request: Request,
    watched_item_id: str,
    tpl_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404)
    tpl = await _item_template_or_404(session, wi, tpl_id)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_template_form.html",
        {"watched_item": wi, "tpl": tpl, "event_titles": EVENT_TITLES},
    )


@router.post("/watched-items/{watched_item_id}/templates/{tpl_id}")
async def watched_item_template_update(
    request: Request,
    watched_item_id: str,
    tpl_id: str,
    title: str = Form(""),
    channel_hint: str = Form(...),
    events: str = Form("change_detected"),
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404)
    tpl = await _item_template_or_404(session, wi, tpl_id)
    event_list = [e.strip() for e in events.split(",") if e.strip()]
    try:
        event_list = validate_event_list(event_list)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tpl.title = title.strip() or channel_hint.strip()
    tpl.channel_hint = channel_hint.strip()
    tpl.events = event_list
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_UPDATED,
        watched_item_id=str(wi.id),
        template_id=str(tpl.id),
        source="dashboard",
    )
    await session.commit()

    refreshed = await get_watched_item_notifications(session, wi.id)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_template_rows.html",
        {"watched_item": wi, "templates": refreshed},
    )


@router.delete("/watched-items/{watched_item_id}/templates/{tpl_id}")
async def watched_item_template_delete(
    request: Request,
    watched_item_id: str,
    tpl_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404)
    tpl = await _item_template_or_404(session, wi, tpl_id)
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_DELETED,
        watched_item_id=str(wi.id),
        template_id=str(tpl.id),
        source="dashboard",
    )
    await session.delete(tpl)
    await session.commit()

    refreshed = await get_watched_item_notifications(session, wi.id)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_template_rows.html",
        {"watched_item": wi, "templates": refreshed},
    )


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

    if request.headers.get("HX-Request") == "true":
        watched_items = await get_domain_watched_items(
            session, name, search=q, sort=sort, order=order, status=status
        )
        return templates.TemplateResponse(
            request,
            "partials/domain_toggle_oob.html",
            {
                "domain": domain,
                "watched_items": watched_items,
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

    if request.headers.get("HX-Request") != "true":
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

    if request.headers.get("HX-Request") == "true":
        ctx = _field_context(request, domain, field, mode="view")
        return templates.TemplateResponse(request, "partials/domain_field.html", ctx)
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

    watched_items = await get_domain_watched_items(
        session, name, search=q, sort=sort, order=order, status=status
    )
    all_wi_count_result = await session.execute(
        select(func.count(WatchedItem.id)).where(WatchedItem.domain_name == name)
    )
    all_watched_items_count = all_wi_count_result.scalar_one()

    field_contexts = {
        fname: _field_context(request, domain, fname, mode="view") for fname in DOMAIN_FIELD_META
    }

    context = {
        "active_page": "domains",
        "domain": domain,
        "watched_items": watched_items,
        "all_watched_items_count": all_watched_items_count,
        "q": q or "",
        "sort": sort,
        "order": order,
        "status": status or "",
        "flash": None,
        "field_contexts": field_contexts,
    }
    return templates.TemplateResponse(request, "pages/domain_detail.html", context)


async def _render_domain_nc_defaults(request: Request, domain_name: str, session: AsyncSession):
    """Render the domain_nc_defaults partial for *domain_name* (#200).

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
        "partials/domain_nc_defaults.html",
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

    _cc = _parse_content_config_from_form(form)
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

    tpl = NotificationTemplate(
        title=title,
        channel_hint=channel_hint,
        events=events,
        visibility=VISIBILITY_DOMAIN,
        domain_name=domain_name,
        is_active=True,
        content_config=_cc,
        remote_channel_id=remote_channel_id,
    )
    session.add(tpl)
    await session.flush()
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_CREATED,
        template_id=str(tpl.id),
        title=title,
        channel_hint=channel_hint,
        source="domain_dashboard",
        domain_name=domain_name,
    )
    await session.commit()
    return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)


@router.get("/domains/{domain_name}/nc-defaults")
async def domain_nc_defaults_partial(
    request: Request,
    domain_name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: notification defaults assigned to a domain."""
    return await _render_domain_nc_defaults(request, domain_name, session)


@router.post("/domains/{domain_name}/nc-defaults/remove/{template_id}")
async def domain_nc_default_remove(
    request: Request,
    domain_name: str,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a domain-scoped notification template (#200: removal = delete the row)."""
    if request.headers.get("HX-Request") != "true":
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
    return await _render_domain_nc_defaults(request, domain_name, session)


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
    return templates.TemplateResponse(
        request,
        "partials/domain_watched_items_table.html",
        {
            "domain": domain,
            "watched_items": watched_items,
            "q": q or "",
            "sort": sort,
            "order": order,
            "status": status or "",
        },
    )


@router.get("/audit")
async def audit_log_page(
    request: Request,
    event_type: str | None = None,
    watched_item_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Audit log page with filtering."""
    event_type = event_type or None
    entries = await get_audit_entries(
        session, event_type=event_type, watched_item_id=watched_item_id
    )
    context = {
        "active_page": "audit",
        "entries": entries,
        "event_type": event_type,
    }
    return templates.TemplateResponse(request, "pages/audit_log.html", context)


@router.get("/partials/audit-table")
async def partial_audit_table(
    request: Request,
    event_type: str | None = None,
    watched_item_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: filtered audit log table."""
    event_type = event_type or None
    entries = await get_audit_entries(
        session, event_type=event_type, watched_item_id=watched_item_id
    )
    return templates.TemplateResponse(request, "partials/audit_table.html", {"entries": entries})


# ---------------------------------------------------------------------------
# Notification Template Library — /notifications/*
# ---------------------------------------------------------------------------


@router.get("/partials/notification-templates-list")
async def partial_notification_templates_list(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: notification template table rows (tbody content)."""
    result = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "partials/notification_template_list.html",
        {"notification_templates": notification_templates},
    )


@router.get("/notifications/overrides/add-picker")
async def notifications_override_add_picker(request: Request):
    """Return the override picker (a <select> of subscribed-but-not-overridden events).

    Called via HTMX from the [+ Add override] button; reads current form state
    to figure out which events are subscribed and which already have overrides.
    """
    params = request.query_params
    form_id = params.get("form_id") or "new"
    subscribed = set(params.getlist("events"))
    # Events already overridden — infer from presence of any
    # content_config__override__<et>__* key in the form state.
    overridden = set()
    for key in params.keys():
        if key.startswith("content_config__override__"):
            # content_config__override__<et>__<field>
            rest = key[len("content_config__override__") :]
            if "__" in rest:
                et_value = rest.split("__", 1)[0]
                overridden.add(et_value)
    pickable = [
        (v, EVENT_TITLES[v])
        for v in _ALL_EVENT_TYPE_VALUES
        if v in subscribed and v not in overridden
    ]
    return templates.TemplateResponse(
        request,
        "partials/notification_form_override_picker.html",
        {"form_id": form_id, "pickable": pickable},
    )


@router.get("/notifications/overrides/card")
async def notifications_override_card(request: Request):
    """Return a new override card, pre-populated by copying current default state.

    Called via HTMX after the user picks an event in the add-picker. Reads the
    current form's default `content_config__*` fields to seed the override's
    options; user then tweaks.
    """
    params = request.query_params
    form_id = params.get("form_id") or "new"
    event_type = params.get("event_type") or ""
    try:
        WatchEventType(event_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid event_type: {event_type}")
    # Parse current defaults; treat result as the starting options for the new override.
    cc_dict = _parse_content_config_from_form(params)
    config = ContentConfig.model_validate(cc_dict) if cc_dict else None
    seed_opts = config.default if config else ContentOptions()
    return templates.TemplateResponse(
        request,
        "partials/notification_form_override_card.html",
        {
            "form_id": form_id,
            "event_type": event_type,
            "prefix": f"content_config__override__{event_type}__",
            "opts": seed_opts,
            "scope": f"override-{event_type}",
        },
    )


@router.get("/notifications/compose-title-prefill")
async def notifications_compose_title_prefill(request: Request):
    """Return the default title Jinja template for the current preview_event.

    Used by the [Edit template] control on the Default title block to pre-fill
    the textarea with runnable Jinja the user can tweak.
    """
    preview_event_raw = request.query_params.get("preview_event") or "change_detected"
    try:
        et = WatchEventType(preview_event_raw)
    except ValueError:
        et = WatchEventType.CHANGE_DETECTED
    prefill = compose_title_prefill(et.value)
    return HTMLResponse(prefill)


@router.get("/notifications/compose-body-prefill")
async def notifications_compose_body_prefill(request: Request):
    """Return the default body Jinja template for the selected preview_event.

    Used by the "Show default template" control on the Default body block so the
    user can copy the skeleton into a custom body_template and edit from there.
    Toggles are not applied to the seed — they drive Python-side interleaving
    in `build_body`, not the template the user customises.
    """
    preview_event_raw = request.query_params.get("preview_event") or "change_detected"
    try:
        et = WatchEventType(preview_event_raw)
    except ValueError:
        et = WatchEventType.CHANGE_DETECTED
    return HTMLResponse(compose_body_prefill(et.value))


@router.post("/notifications/preview")
async def notifications_preview(request: Request):
    """Stateless live preview of a notification body + title.

    Consumes the full notification form via `hx-include="closest form"` plus a
    `preview_event` field selecting which WatchEventType to simulate. Renders
    title and body through the same pipeline as the dispatcher, but under
    strict-Jinja mode so template errors surface.

    Returns the `partials/notification_preview.html` fragment with either a
    rendered preview or an error card on template failure.
    """
    form = await request.form()
    preview_event_raw = form.get("preview_event") or "change_detected"
    try:
        et = WatchEventType(preview_event_raw)
    except ValueError:
        et = WatchEventType.CHANGE_DETECTED

    cc_dict = _parse_content_config_from_form(form)
    config = ContentConfig.model_validate(cc_dict) if cc_dict else None
    options = resolve_options(config, et.value)

    event = build_preview_event(et.value)
    preview_diff = compute_preview_unified_diff(et.value)

    try:
        title = build_title(event, options, strict=True)
    except TemplateError as exc:
        return templates.TemplateResponse(
            request,
            "partials/notification_preview.html",
            {"error": {"where": "title template", "message": str(exc)}},
        )

    try:
        body = build_body(event, options, strict=True, unified_diff=preview_diff)
    except TemplateError as exc:
        return templates.TemplateResponse(
            request,
            "partials/notification_preview.html",
            {"error": {"where": "body template", "message": str(exc)}},
        )

    return templates.TemplateResponse(
        request,
        "partials/notification_preview.html",
        {
            "preview": {
                "title": title,
                "body": body,
                "event_label": EVENT_TITLES[et.value],
            }
        },
    )


@router.get("/notifications")
async def notifications_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Notification template library page."""
    result = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "pages/notifications.html",
        {
            "active_page": "settings",
            "notification_templates": notification_templates,
        },
    )


@router.get("/notifications/new")
async def notification_template_new_page(
    request: Request,
):
    """Full page: create a new global notification template.

    The library page creates ``visibility='global'`` templates; domain- and
    item-scoped templates are created from the domain and item detail pages (#200).
    """
    return templates.TemplateResponse(
        request,
        "pages/notification_new.html",
        {
            "active_page": "settings",
            "title": None,
            "events": None,
            "content_config": None,
            "error": None,
        },
    )


@router.post("/notifications/new")
async def notification_template_create(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Create notification template from dashboard form.

    Redirects on success, rerenders page on error.
    """
    form = await request.form()
    events = form.getlist("events")
    title = str(form.get("title") or "").strip()
    remote_channel_id = str(form.get("remote_channel_id") or "").strip()
    channel_hint = str(form.get("channel_hint") or "").strip() or "remote"

    def _page_error(error_msg: str):
        _cc = _parse_content_config_from_form(form)
        return templates.TemplateResponse(
            request,
            "pages/notification_new.html",
            {
                "active_page": "settings",
                "title": str(form.get("title") or ""),
                "events": form.getlist("events"),
                "content_config": ContentConfig.model_validate(_cc) if _cc else None,
                "error": error_msg,
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

    tpl = NotificationTemplate(
        title=title,
        channel_hint=channel_hint,
        events=events,
        visibility=VISIBILITY_GLOBAL,
        content_config=_parse_content_config_from_form(form),
        remote_channel_id=remote_channel_id,
    )
    session.add(tpl)
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_CREATED,
        template_id=str(tpl.id),
        title=title,
        channel_hint=channel_hint,
        source="dashboard",
    )
    await session.commit()
    return RedirectResponse(url="/notifications", status_code=303)


@router.get("/notifications/{template_id}/edit")
async def notification_template_edit_page(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Full page: edit an existing notification template."""
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    content_config = (
        ContentConfig.model_validate(tpl.content_config) if tpl.content_config else None
    )
    return templates.TemplateResponse(
        request,
        "pages/notification_edit.html",
        {
            "active_page": "settings",
            "tpl": tpl,
            "submitted_title": tpl.title,
            "content_config": content_config,
            "error": None,
        },
    )


@router.post("/notifications/{template_id}/edit")
async def notification_template_edit(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Save changes to a notification template. Redirects on success, rerenders page on error."""
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    form = await request.form()
    remote_channel_id = str(form.get("remote_channel_id") or "").strip()
    channel_hint = str(form.get("channel_hint") or "").strip() or tpl.channel_hint
    events = form.getlist("events")
    title = str(form.get("title") or "").strip() or tpl.title

    async def _edit_error(error_msg: str) -> Response:
        # Re-derive content_config from submitted form so checkboxes stay checked on error
        _content_config_err = _parse_content_config_from_form(form)
        content_config_err = (
            ContentConfig.model_validate(_content_config_err) if _content_config_err else None
        )
        return templates.TemplateResponse(
            request,
            "pages/notification_edit.html",
            {
                "active_page": "settings",
                "tpl": tpl,
                "submitted_title": title,
                "submitted_events": events,
                "content_config": content_config_err,
                "error": error_msg,
            },
        )

    if not remote_channel_id:
        return await _edit_error("Remote channel ID is required.")

    try:
        validate_event_list(events)
    except ValueError as exc:
        return await _edit_error(str(exc))

    tpl.title = title
    tpl.remote_channel_id = remote_channel_id
    tpl.channel_hint = channel_hint
    tpl.events = events
    tpl.content_config = _parse_content_config_from_form(form)
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_UPDATED,
        template_id=str(tpl.id),
        title=tpl.title,
        channel_hint=tpl.channel_hint,
        source="dashboard",
    )
    await session.commit()
    return RedirectResponse(url="/notifications", status_code=303)


@router.post("/notifications/{template_id}/toggle")
async def notification_template_toggle(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Toggle is_active on a notification template. Returns refreshed list."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/notifications", status_code=303)
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    tpl.is_active = not tpl.is_active
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_UPDATED,
        template_id=str(tpl.id),
        title=tpl.title,
        is_active=tpl.is_active,
        source="dashboard",
    )
    await session.commit()
    result2 = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result2.scalars().all()
    response = templates.TemplateResponse(
        request,
        "partials/notification_template_list.html",
        {"notification_templates": notification_templates},
    )
    response.headers["HX-Trigger"] = "refreshTemplates"
    return response


@router.delete("/notifications/{template_id}/delete")
async def notification_template_delete(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a notification template. Returns refreshed list.

    Templates are standalone post-#200 — no junction refs to block deletion.
    """
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/notifications", status_code=303)
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_DELETED,
        template_id=str(tpl.id),
        title=tpl.title,
        source="dashboard",
    )
    await session.delete(tpl)
    await session.commit()
    result2 = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result2.scalars().all()
    response = templates.TemplateResponse(
        request,
        "partials/notification_template_list.html",
        {"notification_templates": notification_templates},
    )
    response.headers["HX-Trigger"] = "refreshTemplates"
    return response


@router.post("/notifications/{template_id}/duplicate")
async def notification_template_duplicate(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Duplicate a notification template. Returns refreshed list."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/notifications", status_code=303)
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    copy = NotificationTemplate(
        title=f"{tpl.title} (copy)",
        channel_hint=tpl.channel_hint,
        events=list(tpl.events),
        visibility=tpl.visibility,
        domain_name=tpl.domain_name,
        watched_item_id=tpl.watched_item_id,
        content_config=tpl.content_config,
        remote_channel_id=tpl.remote_channel_id,
    )
    session.add(copy)
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_CREATED,
        template_id="(duplicate)",
        title=copy.title,
        source="dashboard",
    )
    await session.commit()
    result2 = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result2.scalars().all()
    response = templates.TemplateResponse(
        request,
        "partials/notification_template_list.html",
        {"notification_templates": notification_templates},
    )
    response.headers["HX-Trigger"] = "refreshTemplates"
    return response


@router.post("/notifications/{template_id}/test-result")
async def notification_template_test_result(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Send a test notification for a template and return an OOB flash."""
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    if not tpl.remote_channel_id:
        success = False
        reason = "no remote_channel_id configured"
    else:
        event = WatchEvent(
            event_type=WatchEventType.CHANGE_DETECTED,
            watched_item_id="00000000000000000000000000",
            item_name="Test Notification",
            item_url="https://example.com",
            occurred_at=datetime.now(UTC),
            metadata={"test": True},
        )
        _cc = ContentConfig.model_validate(tpl.content_config) if tpl.content_config else None
        opts = resolve_options(_cc, WatchEventType.CHANGE_DETECTED.value)
        candidate = DispatchCandidate(
            source="watch_template",
            source_id=str(tpl.id),
            content_config=tpl.content_config,
            remote_channel_id=tpl.remote_channel_id,
        )
        try:
            async with get_notifier_client() as client:
                outcome = await dispatch_via_notifier(
                    client,
                    candidate,
                    event,
                    rendered_title=build_title(event, opts),
                    rendered_body=build_body(event, opts),
                )
            success = outcome.success
            reason = outcome.reason
        except NotifierError as exc:
            success = False
            reason = f"notifier error: {exc}"
        except Exception:
            logger.exception("test notification error", extra={"template_id": template_id})
            reason = "Internal error during dispatch"
            success = False

    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_TESTED,
        template_id=str(tpl.id),
        title=tpl.title,
        channel_hint=tpl.channel_hint,
        success=success,
        reason=reason,
        source="dashboard",
    )
    await session.commit()
    level = "success" if success else "error"
    message = f"Test notification: {reason}"
    return templates.TemplateResponse(
        request,
        "partials/flash_oob.html",
        {
            "flash_oob_level": level,
            "flash_oob_message": message,
        },
    )
