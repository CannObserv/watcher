"""Audit Log page and the shared audit-table HTMX partial (#215)."""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.core.logging import get_logger
from src.dashboard.context import (
    get_audit_entries,
    get_audit_entries_count,
    get_distinct_audit_event_types,
)
from src.dashboard.deps import clamp_pagination
from src.dashboard.templating import templates

router = APIRouter()
logger = get_logger(__name__)


async def audit_table_context(
    session: AsyncSession,
    *,
    event_types: list[str],
    watched_item_id: str | None,
    page: int,
    page_size: int,
) -> dict:
    """Build the shared audit-table render context (#215).

    Serves both /audit (no ``watched_item_id`` → Watched Item column shown) and
    the WatchedItem detail "Recent Activity" section (scoped → column hidden,
    different HTMX target). ``event_types`` is OR-matched, so several selected
    chips broaden the result set. Carries the pagination wiring
    ``partials/pagination`` expects, including an ``hx_include`` that preserves the
    active filter when the page size changes. ``page`` / ``page_size`` are clamped
    (``clamp_pagination``) so a hand-crafted query (e.g. ``page_size=-5``) can't
    reach the DB as a negative ``LIMIT`` or load an unbounded result set.
    """
    page, page_size = clamp_pagination(page, page_size)
    offset = (page - 1) * page_size
    entries = await get_audit_entries(
        session,
        event_types=event_types,
        watched_item_id=watched_item_id,
        limit=page_size,
        offset=offset,
    )
    total_count = await get_audit_entries_count(
        session, event_types=event_types, watched_item_id=watched_item_id
    )
    item_scoped = watched_item_id is not None
    # extra_params drives the pager links; event_type is multi-valued (one query
    # param per selected chip) so pagination.html expands list values.
    extra_params: dict[str, str | list[str]] = {}
    if event_types:
        extra_params["event_type"] = event_types
    if watched_item_id:
        extra_params["watched_item_id"] = watched_item_id
    return {
        "entries": entries,
        "show_watched_item": not item_scoped,
        "selected_event_types": event_types,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "base_url": "/partials/audit-table",
        "extra_params": extra_params,
        # Full-page /audit gets the viewport-anchored sticky footer; the item-scoped
        # detail "Recent Activity" pager is a flush footer inside its card, so it
        # anchors to the card instead of floating mid-page.
        "sticky": not item_scoped,
        "hx_target": "#wi-activity-table" if item_scoped else "#audit-table",
        "hx_include": (
            "[name='event_type'],[name='watched_item_id']" if item_scoped else "[name='event_type']"
        ),
    }


@router.get("/audit")
async def audit_log_page(
    request: Request,
    event_type: list[str] = Query(default_factory=list),
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_db_session),
):
    """Audit log page with chip filtering + pagination.

    ``event_type`` is repeatable (``?event_type=a&event_type=b``) and OR-matched.
    """
    selected = [e for e in event_type if e]
    context = await audit_table_context(
        session,
        event_types=selected,
        watched_item_id=None,
        page=page,
        page_size=page_size,
    )
    # Chips are derived from the event types actually present, so the filter always
    # matches the data — no dead chips, no missing chips (#217). Union in any active
    # (selected) type so a deep-linked filter with no rows still has a chip the
    # operator can see and uncheck.
    present = await get_distinct_audit_event_types(session)
    choices = sorted(set(present) | set(selected))
    context.update(
        {
            "active_page": "audit",
            "event_choices": [(e, e) for e in choices],
            "chips_target": "#audit-table",
            "chips_watched_item_id": None,
            "clear_href": "/audit",
        }
    )
    return templates.TemplateResponse(request, "pages/audit_log.html", context)


@router.get("/partials/audit-table")
async def partial_audit_table(
    request: Request,
    event_type: list[str] = Query(default_factory=list),
    watched_item_id: str | None = None,
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: filtered, paginated audit table (shared by /audit + detail)."""
    context = await audit_table_context(
        session,
        event_types=[e for e in event_type if e],
        watched_item_id=watched_item_id or None,
        page=page,
        page_size=page_size,
    )
    return templates.TemplateResponse(request, "partials/audit_table.html", context)
