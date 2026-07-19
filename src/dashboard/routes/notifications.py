"""Notification Template Library routes — /notifications/* pages, preview, prefill."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import TemplateError
from notifier_client.errors import NotifierError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.api.routes.helpers import parse_ulid
from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.api.schemas.validators import validate_event_list
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.notification_template import (
    VISIBILITY_GLOBAL,
    NotificationTemplate,
)
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
from src.core.notifications.preview_fixtures import build_preview_event
from src.core.notifications.templates import (
    create_template,
    delete_template,
    duplicate_template,
    update_template_fields,
)
from src.core.notifier_client import get_notifier_client
from src.dashboard.deps import is_htmx
from src.dashboard.forms import ALL_EVENT_TYPE_VALUES, parse_content_config_from_form
from src.dashboard.templating import templates

router = APIRouter()
logger = get_logger(__name__)


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
        for v in ALL_EVENT_TYPE_VALUES
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
    cc_dict = parse_content_config_from_form(params)
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

    cc_dict = parse_content_config_from_form(form)
    config = ContentConfig.model_validate(cc_dict) if cc_dict else None
    options = resolve_options(config, et.value)

    event = build_preview_event(et.value)

    try:
        title = build_title(event, options, strict=True)
    except TemplateError as exc:
        return templates.TemplateResponse(
            request,
            "partials/notification_preview.html",
            {"error": {"where": "title template", "message": str(exc)}},
        )

    try:
        body = build_body(event, options, strict=True)
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
        _cc = parse_content_config_from_form(form)
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

    await create_template(
        session,
        visibility=VISIBILITY_GLOBAL,
        title=title,
        channel_hint=channel_hint,
        events=events,
        content_config=parse_content_config_from_form(form),
        remote_channel_id=remote_channel_id,
        audit_fields={"title": title, "channel_hint": channel_hint, "source": "dashboard"},
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
        _content_config_err = parse_content_config_from_form(form)
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

    update_template_fields(
        session,
        tpl,
        {
            "title": title,
            "remote_channel_id": remote_channel_id,
            "channel_hint": channel_hint,
            "events": events,
            "content_config": parse_content_config_from_form(form),
        },
        audit_fields={"title": title, "channel_hint": channel_hint, "source": "dashboard"},
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
    if not is_htmx(request):
        return RedirectResponse(url="/notifications", status_code=303)
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    new_active = not tpl.is_active
    update_template_fields(
        session,
        tpl,
        {"is_active": new_active},
        audit_fields={"title": tpl.title, "is_active": new_active, "source": "dashboard"},
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
    if not is_htmx(request):
        return RedirectResponse(url="/notifications", status_code=303)
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    await delete_template(session, tpl, audit_fields={"title": tpl.title, "source": "dashboard"})
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
    if not is_htmx(request):
        return RedirectResponse(url="/notifications", status_code=303)
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    await duplicate_template(
        session, tpl, audit_fields={"title": f"{tpl.title} (copy)", "source": "dashboard"}
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
