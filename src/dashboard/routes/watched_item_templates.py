"""Item-scoped notification-template panel routes (WatchedItem detail page)."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.api.routes.helpers import parse_ulid
from src.api.schemas.validators import validate_event_list
from src.core.logging import get_logger
from src.core.models.notification_template import (
    VISIBILITY_WATCHED_ITEM,
    NotificationTemplate,
)
from src.core.models.watched_item import WatchedItem
from src.core.notifications.events import EVENT_TITLES
from src.core.notifications.templates import (
    create_template,
    delete_template,
    update_template_fields,
)
from src.dashboard.context import (
    get_domain_default_templates,
    get_global_default_templates,
    get_watched_item_detail,
    get_watched_item_notifications,
)
from src.dashboard.templating import templates

router = APIRouter()
logger = get_logger(__name__)


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

    await create_template(
        session,
        visibility=VISIBILITY_WATCHED_ITEM,
        watched_item_id=wi.id,
        title=title.strip() or channel_hint.strip(),
        channel_hint=channel_hint.strip(),
        events=event_list,
        audit_fields={"watched_item_id": str(wi.id), "source": "dashboard"},
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
    update_template_fields(
        session,
        tpl,
        {
            "title": title.strip() or channel_hint.strip(),
            "channel_hint": channel_hint.strip(),
            "events": event_list,
        },
        audit_fields={"watched_item_id": str(wi.id), "source": "dashboard"},
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
    await delete_template(
        session, tpl, audit_fields={"watched_item_id": str(wi.id), "source": "dashboard"}
    )
    await session.commit()

    refreshed = await get_watched_item_notifications(session, wi.id)
    return templates.TemplateResponse(
        request,
        "partials/watched_item_template_rows.html",
        {"watched_item": wi, "templates": refreshed},
    )
