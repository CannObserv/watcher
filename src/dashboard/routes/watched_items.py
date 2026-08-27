"""WatchedItem dashboard routes — list, detail, lifecycle, inline fields, tags."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.api.routes.watched_items import (
    archive_watched_item as _api_archive_watched_item,
)
from src.api.routes.watched_items import (
    check_now as _api_check_now,
)
from src.api.routes.watched_items import (
    delete_watched_item as _api_delete_watched_item,
)
from src.api.routes.watched_items import (
    mark_reviewed as _api_mark_reviewed,
)
from src.api.routes.watched_items import (
    restore_watched_item as _api_restore_watched_item,
)
from src.core.domains import (
    ensure_domain_and_resolve_suspension,
)
from src.core.models.audit_log import EventType, audit
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watched_item import WatchedItem, WatchHealthStatus
from src.core.scheduling.schedule import resolve_schedule_display
from src.core.watched_items import (
    ArchivedItemActivationError,
    RegistryOwnedActivationError,
    SuspendedDomainResumeError,
    resolve_watch_target,
    set_item_schedule_interval,
    set_watched_item_active,
)
from src.dashboard.context import (
    WATCHED_ITEM_EVENT_CHOICES,
    build_schedule_map,
    get_active_profiles_by_item,
    get_domain_default_templates,
    get_global_default_templates,
    get_watched_item_detail,
    get_watched_item_list,
    get_watched_item_notifications,
    get_watched_item_profiles,
    get_watched_items_total_count,
    unacknowledged_spec_change,
)
from src.dashboard.deps import clamp_pagination, is_htmx
from src.dashboard.routes.audit import audit_table_context
from src.dashboard.templating import templates

router = APIRouter()


# --- WatchedItem inline field editing ---


def _format_interval(wi: WatchedItem) -> str:
    cfg = wi.default_schedule_config or {}
    return cfg.get("interval") or ""


def _format_content_type(wi: WatchedItem) -> str:
    return wi.content_media_type or ""


def _interval_display(
    wi: WatchedItem, value: str, *, profiles: list[dict] | None = None
) -> tuple[str, str | None]:
    """View-mode display for the interval field; returns ``(display, marker)``.

    Thin adapter over ``resolve_schedule_display`` (#206). ``marker`` is the source
    word rendered after a "·" — ``None`` for an explicit item interval (no marker),
    ``"domain"``/``"default"`` for an inherited tier (#205), or ``"profile"`` when a
    temporal profile is currently overriding the base cadence. Pass ``profiles`` to
    honor the profile override; omit for a base-cadence-only view. ``value`` is
    accepted for the field-meta ``display`` contract but the resolution is
    authoritative.
    """
    d = resolve_schedule_display(wi, now=datetime.now(UTC), profiles=profiles)
    return d.interval_text, d.marker


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
    "content_media_type": {
        "label": "Content Type",
        "hint": "Auto-detected from the first fetch. Override only to correct a "
        "mislabeled origin (e.g. a PDF served as application/octet-stream).",
        "type": "text",
        "source": "column",
        "cast": lambda v: v.strip() or None,
        "format": _format_content_type,
    },
}

EDITABLE_WATCHED_ITEM_FIELDS = set(WATCHED_ITEM_FIELD_META.keys())


def _watched_item_field_context(
    request: Request,
    wi: WatchedItem,
    field_name: str,
    mode: str = "view",
    profiles: list[dict] | None = None,
) -> dict:
    meta = WATCHED_ITEM_FIELD_META[field_name]
    value = meta["format"](wi)
    # A field may supply a "display" callable returning (view_value, inherited) —
    # e.g. the interval field resolves the inherited system default for view mode
    # while edit mode still binds the explicit override. Others view == edit.
    display_fn = meta.get("display")
    # display_fn returns (view_value, marker) where marker is None or the source
    # word ("domain"/"default"/"profile") rendered after a "·" (#205, #206).
    # ``profiles`` lets the interval field honor an active temporal-profile override.
    display_value, inherited = (
        display_fn(wi, value, profiles=profiles) if display_fn else (value, None)
    )
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
        # Shared with the API PATCH (#254 CR-1): the setter validates the shape
        # and releases any reduce_frequency throttle, so an operator editing the
        # interval is once again the way out of a floor.
        set_item_schedule_interval(wi, typed_value)


def _watched_item_extra_params(q: str | None, include_archived: bool) -> dict[str, str]:
    return {
        k: v
        for k, v in {"q": q, "include_archived": "true" if include_archived else None}.items()
        if v
    }


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
    page, page_size = clamp_pagination(page, page_size)
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
    profiles_by_wi = await get_active_profiles_by_item(session, [wi.id for wi in watched_items])
    return templates.TemplateResponse(
        request,
        "pages/watched_items.html",
        {
            "request": request,
            "active_page": "watched-items",
            "watched_items": watched_items,
            "schedule_map": build_schedule_map(watched_items, now, profiles_by_wi),
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
    page, page_size = clamp_pagination(page, page_size)
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
    profiles_by_wi = await get_active_profiles_by_item(session, [wi.id for wi in watched_items])
    return templates.TemplateResponse(
        request,
        "partials/watched_items_table.html",
        {
            "watched_items": watched_items,
            "schedule_map": build_schedule_map(watched_items, now, profiles_by_wi),
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "base_url": "/partials/watched-items-table",
            "hx_target": "#watched-items-table-container",
            "hx_include": "[name='q'],[name='include_archived']",
            "extra_params": _watched_item_extra_params(q, include_archived),
        },
    )


def _active_profile_dicts(profiles: list[TemporalProfile]) -> list[dict]:
    """Active profiles as resolution dicts — the shape resolve_schedule_display and
    schedule_tick consume (#206). One conversion rule, shared by the detail route
    and the inline interval field partial.
    """
    return [p.to_resolution_dict() for p in profiles if p.is_active]


@router.get("/watched-items/{watched_item_id}")
async def watched_item_detail_page(
    request: Request,
    watched_item_id: str,
    event_type: list[str] = Query(default_factory=list),
    page: int = 1,
    page_size: int = 25,
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
    # #274: the registry re-announced the specs and nobody has acknowledged it.
    spec_change_at = await unacknowledged_spec_change(session, wi)

    # Recent Activity reuses the shared audit-log table + chip filter, scoped to
    # this item (#215). HTMX drives filtering/paging, but the route also honors
    # the ?event_type/page query params so the no-JS Apply button and deep-links
    # work (CR-1). event_type is repeatable and OR-matched.
    activity_ctx = await audit_table_context(
        session,
        event_types=[e for e in event_type if e],
        watched_item_id=str(wi.id),
        page=page,
        page_size=page_size,
    )

    # Resolution dicts for the active profiles drive the interval field's
    # profile-aware display (#206) — same shape schedule_tick consumes.
    profile_dicts = _active_profile_dicts(profiles)
    field_contexts = {
        name: _watched_item_field_context(request, wi, name, mode="view", profiles=profile_dicts)
        for name in ("name", "description", "default_schedule_interval", "content_media_type")
    }

    context = {
        "request": request,
        "active_page": "watched-items",
        "watched_item": wi,
        "flash": None,
        "field_contexts": field_contexts,
        "templates": item_templates,
        "global_templates": global_templates,
        "domain_templates": domain_templates,
        "profiles": profiles,
        "spec_change_at": spec_change_at,
        # Recent Activity chip-filter context (table context — incl.
        # selected_event_types — merged below).
        "event_choices": WATCHED_ITEM_EVENT_CHOICES,
        "chips_target": "#wi-activity-table",
        "chips_watched_item_id": str(wi.id),
        "clear_href": f"/watched-items/{wi.id}",
    }
    context.update(activity_ctx)
    return templates.TemplateResponse(request, "pages/watched_item_detail.html", context)


@router.post("/watched-items/{watched_item_id}/archive")
async def watched_item_archive(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Dashboard archive — cascades to child Watches (delegates to shared logic)."""
    await _api_archive_watched_item(watched_item_id, session)
    if is_htmx(request):
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
    if is_htmx(request):
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/watched-items/{watched_item_id}"},
        )
    return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)


@router.post("/watched-items/{watched_item_id}/delete")
async def watched_item_delete(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Permanently delete an archived WatchedItem (delegates to the API route, #210).

    The API enforces the guards (404 not found, 409 not archived, 409
    registry-owned — #254 CR-7). On success the
    item is gone, so we redirect to the list rather than the now-missing detail
    page. A 409 (un-archived) surfaces as an OOB error flash for HTMX, or a
    redirect back to the still-present detail page for non-HTMX clients.
    """
    hx = is_htmx(request)
    try:
        await _api_delete_watched_item(watched_item_id, session)
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
    if hx:
        return Response(status_code=200, headers={"HX-Redirect": "/watched-items"})
    return RedirectResponse(url="/watched-items", status_code=303)


@router.post("/watched-items/{watched_item_id}/mark-reviewed")
async def watched_item_mark_reviewed(
    request: Request,
    watched_item_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Acknowledge the current ``source_specs`` — stamps ``last_reviewed_at``.

    The **Acknowledge** button in the Source Specs panel's "Specs changed"
    callout is the caller (#274). It had none between #185 Phase A, which removed
    the sub_aspect banner holding the only form, and #274, which gave the stamp a
    meaning worth setting: the operator has seen the specs the registry most
    recently announced.
    """
    await _api_mark_reviewed(watched_item_id, session)
    if is_htmx(request):
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
    hx = is_htmx(request)
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

    new_active = active == "true"
    try:
        changed = set_watched_item_active(session, wi, active=new_active, source="dashboard")
    except ArchivedItemActivationError:
        return _respond(("error", "Watched Item is archived — restore it to change its status."))
    except RegistryOwnedActivationError:
        return _respond(
            (
                "warning",
                "Pause and resume for this item live in Archiver — a local toggle "
                "would be reverted by the next registry announcement.",
            )
        )
    except SuspendedDomainResumeError:
        return _respond(("warning", "Cannot resume while the domain is suspended."))
    if changed:
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

    The API enforces the pre-flight guards (409 archived / paused / domain
    suspended / command already in flight, 422 empty effective_url). For HTMX,
    success and guard failures surface as an OOB flash; non-HTMX clients get a
    redirect back to the detail page.
    """
    hx = is_htmx(request)
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

    A registry-owned item (``applied_generation`` set) is forced to view mode
    (#254 CR-27). The template already drops the Edit button, but this route is
    reachable directly, and handing back a form whose POST is guaranteed to
    flash a refusal wastes the operator's typing.
    """
    wi = await get_watched_item_detail(session, watched_item_id)
    if not wi:
        raise HTTPException(status_code=404, detail="WatchedItem not found")
    if not is_htmx(request):
        return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)
    if wi.applied_generation is not None:
        mode = "view"
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
    hx = is_htmx(request)
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

    # #254 CR-22: the URL is announcement-owned on a reconciled item, and local
    # drift on it is permanent (the same-generation snapshot is ignored as
    # stale). Same rule as the API PATCH guard, in this surface's shape.
    if wi.applied_generation is not None:
        return _flash(
            "This item's URL is registry-owned — edit the InfoSource in Archiver "
            "instead. A local change would diverge until the next announcement.",
            "warning",
        )

    url_raw = url.strip()
    if not url_raw:
        return _flash("URL is required.", "error")

    # No re-probe (#241): the item re-enters PROBING and the apply path
    # resolves any redirect from the next fact.
    try:
        effective_url, new_domain, health_status = resolve_watch_target(url_raw)
    except ValueError as exc:
        return _flash(str(exc), "error")

    # Upsert the domain and re-evaluate suspension against the (possibly new) target
    # so moving a WatchedItem onto a suspended/archived domain doesn't bypass the
    # kill-switch. Shared with the API create/PATCH paths (#196).
    domain_state = await ensure_domain_and_resolve_suspension(session, new_domain)

    wi.effective_url = effective_url
    wi.domain_name = new_domain
    wi.domain_suspended = domain_state.suspended
    wi.domain_default_schedule_config = domain_state.default_schedule_config
    if health_status == WatchHealthStatus.PROBING:
        wi.health_status = WatchHealthStatus.PROBING
    audit(
        session,
        EventType.WATCHED_ITEM_UPDATED,
        watched_item_id=str(wi.id),
        updated_fields=["effective_url", "domain_name"],
        source="dashboard",
    )
    await session.commit()

    if domain_state.suspended and hx:
        return _flash(
            f"URL updated, but '{new_domain}' is suspended — "
            "this Watched Item will not be checked until the domain is reactivated.",
            "warning",
        )

    if hx:
        return Response(
            status_code=200,
            headers={"HX-Redirect": f"/watched-items/{watched_item_id}"},
        )
    return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)


async def _interval_field_profiles(
    session: AsyncSession, wi: WatchedItem, field_name: str
) -> list[dict] | None:
    """Active profile dicts for the interval field's profile-aware display (#206).

    Only the interval field consults profiles, so other fields skip the query and
    get ``None``. Keeps the inline-edit re-render consistent with the full detail
    page (both show ``· profile`` when a profile overrides the base cadence).
    """
    if field_name != "default_schedule_interval":
        return None
    profiles = await get_watched_item_profiles(session, wi.id)
    return _active_profile_dicts(profiles)


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

    if not is_htmx(request):
        return RedirectResponse(url=f"/watched-items/{watched_item_id}", status_code=303)

    profiles = await _interval_field_profiles(session, wi, field_name)
    ctx = _watched_item_field_context(request, wi, field_name, mode=mode, profiles=profiles)
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

    if is_htmx(request):
        profiles = await _interval_field_profiles(session, wi, field_name)
        ctx = _watched_item_field_context(request, wi, field_name, mode="view", profiles=profiles)
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
