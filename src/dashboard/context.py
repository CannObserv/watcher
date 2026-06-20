"""Dashboard context helpers — DB queries for stats, changes, queue health."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.notification_template import (
    VISIBILITY_DOMAIN,
    VISIBILITY_GLOBAL,
    VISIBILITY_WATCHED_ITEM,
    NotificationTemplate,
)
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watched_item import WatchedItem

_DOMAIN_WI_SORT_COLS: dict[str, Any] = {
    "name": WatchedItem.name,
    "last_checked_at": WatchedItem.last_checked_at,
}


async def get_dashboard_stats(session: AsyncSession) -> dict:
    """Aggregate counts for dashboard stat cards.

    Post-#191 the WatchedItem is the single monitored entity, so the "watches"
    stats count WatchedItems. ``changes_today`` is always 0 — Change table
    dropped in Phase 5 (#156).
    """
    total = await session.scalar(
        select(func.count(WatchedItem.id)).where(WatchedItem.archived_at.is_(None))
    )
    active = await session.scalar(
        select(func.count(WatchedItem.id)).where(
            WatchedItem.archived_at.is_(None),
            WatchedItem.is_active.is_(True),
        )
    )

    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    checks_today = await session.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.event_type.in_(
                [
                    EventType.CHECK_SNAPSHOT_CREATED,
                    EventType.CHECK_NO_CHANGE,
                    EventType.CHECK_FETCH_FAILED,
                ]
            ),
            AuditLog.created_at >= today_start,
        )
    )

    return {
        "total_watches": total or 0,
        "active_watches": active or 0,
        "changes_today": 0,
        "checks_today": checks_today or 0,
    }


async def get_recent_changes(session: AsyncSession, limit: int = 20) -> list[dict]:
    """Return empty list — Change table removed in Phase 5 (#156)."""
    return []


async def get_queue_health(session: AsyncSession) -> dict:
    """Query procrastinate_jobs table for queue status counts.

    Returns zeros if the procrastinate_jobs table doesn't exist (e.g., test
    environments without procrastinate migrations applied).
    """
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    default = {"todo": 0, "doing": 0, "failed": 0, "succeeded_today": 0}

    try:
        result = await session.execute(
            text("SELECT status, count(*) FROM procrastinate_jobs GROUP BY status")
        )
        counts = {row[0]: row[1] for row in result.all()}

        succeeded_today = await session.scalar(
            text(
                "SELECT count(*) FROM procrastinate_jobs "
                "WHERE status = 'succeeded' AND scheduled_at >= :today_start"
            ),
            {"today_start": today_start},
        )
    except ProgrammingError:
        await session.rollback()
        return default

    return {
        "todo": counts.get("todo", 0),
        "doing": counts.get("doing", 0),
        "failed": counts.get("failed", 0),
        "succeeded_today": succeeded_today or 0,
    }


def get_rate_limiter_state(limiter=None) -> list[dict]:
    """Get current rate limiter domain states.

    Args:
        limiter: A DomainRateLimiter instance. If None, returns empty list
                 (caller is responsible for providing the limiter).
    """
    if limiter is None:
        return []
    return limiter.get_domain_states()


async def get_audit_entries(
    session: AsyncSession,
    event_type: str | None = None,
    watched_item_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditLog]:
    """Fetch audit log entries with optional filters.

    The WatchedItem association lives in the JSONB ``payload`` (the ``watch_id``
    FK column was retired with the Watch table in #191).
    """
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    if watched_item_id:
        stmt = stmt.where(AuditLog.payload["watched_item_id"].astext == watched_item_id)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_watched_item_profiles(
    session: AsyncSession, watched_item_id: ULID
) -> list[TemporalProfile]:
    """Fetch the temporal profile(s) for a WatchedItem (#191: 1:1)."""
    result = await session.execute(
        select(TemporalProfile).where(TemporalProfile.watched_item_id == watched_item_id)
    )
    return list(result.scalars().all())


async def get_active_profiles_by_item(
    session: AsyncSession, watched_item_ids: list[ULID]
) -> dict[str, list[dict]]:
    """Batch-load active temporal profiles for the given items, keyed by item id.

    Returns the same ``{item_id: [resolution_dict, …]}`` shape the schedule-display
    helper expects (``resolve_schedule_display(profiles=…)``), mirroring the
    ``schedule_tick`` batch load so list/table display honors the profile override
    the scheduler actually applies (#206). Empty input → empty map.
    """
    if not watched_item_ids:
        return {}
    result = await session.execute(
        select(TemporalProfile).where(
            TemporalProfile.is_active.is_(True),
            TemporalProfile.watched_item_id.in_(watched_item_ids),
        )
    )
    by_item: dict[str, list[dict]] = {}
    for p in result.scalars().all():
        by_item.setdefault(str(p.watched_item_id), []).append(p.to_resolution_dict())
    return by_item


async def get_watched_item_notifications(
    session: AsyncSession, watched_item_id: ULID
) -> list[NotificationTemplate]:
    """Fetch the item-scoped notification templates for a WatchedItem (#200)."""
    result = await session.execute(
        select(NotificationTemplate)
        .where(
            NotificationTemplate.visibility == VISIBILITY_WATCHED_ITEM,
            NotificationTemplate.watched_item_id == watched_item_id,
        )
        .order_by(NotificationTemplate.created_at)
    )
    return list(result.scalars().all())


def _apply_domain_filters(stmt, *, search: str | None = None, status: str | None = None):
    """Apply search and status filters to a domain query."""
    if search:
        escaped = search.replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Domain.name.ilike(f"%{escaped}%"))
    # Mirror Domain.status precedence: archived > inactive > backoff > active.
    if status == "active":
        stmt = stmt.where(
            Domain.archived_at.is_(None),
            Domain.is_active.is_(True),
            Domain.current_interval <= Domain.min_interval,
        )
    elif status == "inactive":
        stmt = stmt.where(
            Domain.archived_at.is_(None),
            Domain.is_active.is_(False),
        )
    elif status == "archived":
        stmt = stmt.where(Domain.archived_at.isnot(None))
    elif status == "backoff":
        stmt = stmt.where(
            Domain.archived_at.is_(None),
            Domain.is_active.is_(True),
            Domain.current_interval > Domain.min_interval,
        )
    return stmt


async def get_domains_with_watched_item_counts(
    session: AsyncSession,
    *,
    search: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int | None = None,
) -> list[dict]:
    """Fetch domains with watched item count, last_checked, search, filter, and pagination.

    Args:
        search: Substring match on domain name.
        status: Filter — "active", "archived", "backoff", or None (all).
        page: 1-based page number (only used when page_size is set).
        page_size: Results per page. None means no pagination (return all).
    """
    stmt = (
        select(
            Domain,
            func.count(WatchedItem.id.distinct()).label("watched_item_count"),
            func.max(WatchedItem.last_checked_at).label("last_checked"),
        )
        .outerjoin(WatchedItem, WatchedItem.domain_name == Domain.name)
        .group_by(Domain.id)
    )

    stmt = _apply_domain_filters(stmt, search=search, status=status)

    stmt = stmt.order_by(Domain.name)
    if page_size is not None:
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    result = await session.execute(stmt)
    rows = result.all()
    return [
        {
            "id": str(domain.id),
            "name": domain.name,
            "min_interval": domain.min_interval,
            "current_interval": domain.current_interval,
            "decay_window": domain.decay_window,
            "max_concurrency": domain.max_concurrency,
            "last_request_at": domain.last_request_at,
            "in_backoff": domain.current_interval > domain.min_interval,
            "watched_item_count": watched_item_count,
            "last_checked": last_checked,
            "status": domain.status,
            "notes": domain.notes,
            "archived_at": domain.archived_at,
        }
        for domain, watched_item_count, last_checked in rows
    ]


async def get_domains_total_count(
    session: AsyncSession,
    *,
    search: str | None = None,
    status: str | None = None,
) -> int:
    """Count total domains matching search/filter (for pagination)."""
    stmt = select(func.count(Domain.id))
    stmt = _apply_domain_filters(stmt, search=search, status=status)
    result = await session.execute(stmt)
    return result.scalar_one()


async def get_domain_watched_items(
    session: AsyncSession,
    domain_name: str,
    *,
    search: str | None = None,
    sort: str = "name",
    order: str = "asc",
    status: str | None = None,
) -> list[WatchedItem]:
    """Fetch WatchedItems for a domain with optional search, sort, and status filter."""
    col = _DOMAIN_WI_SORT_COLS.get(sort, WatchedItem.name)
    order_expr = col.asc().nulls_first() if order == "asc" else col.desc().nulls_last()
    stmt = select(WatchedItem).where(WatchedItem.domain_name == domain_name).order_by(order_expr)
    if search:
        escaped = search.replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(WatchedItem.name.ilike(f"%{escaped}%"))
    if status == "active":
        stmt = stmt.where(
            WatchedItem.archived_at.is_(None),
            WatchedItem.domain_suspended.is_(False),
            WatchedItem.is_active.is_(True),
        )
    elif status == "inactive":
        stmt = stmt.where(
            WatchedItem.archived_at.is_(None),
            WatchedItem.domain_suspended.is_(False),
            WatchedItem.is_active.is_(False),
        )
    elif status == "archived":
        stmt = stmt.where(WatchedItem.archived_at.isnot(None))
    elif status == "suspended":
        stmt = stmt.where(WatchedItem.domain_suspended.is_(True))
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _apply_watched_item_filters(
    stmt: Select, *, search: str | None, include_archived: bool
) -> Select:
    if not include_archived:
        stmt = stmt.where(WatchedItem.archived_at.is_(None))
    if search:
        escaped = search.replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(WatchedItem.name.ilike(f"%{escaped}%"))
    return stmt


async def get_watched_item_list(
    session: AsyncSession,
    *,
    include_archived: bool = False,
    search: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> list[WatchedItem]:
    """Fetch WatchedItems for dashboard list display with search and pagination."""
    stmt = select(WatchedItem).order_by(WatchedItem.name)
    stmt = _apply_watched_item_filters(stmt, search=search, include_archived=include_archived)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_watched_items_total_count(
    session: AsyncSession,
    *,
    include_archived: bool = False,
    search: str | None = None,
) -> int:
    """Count WatchedItems matching the given filters (for pagination)."""
    stmt = select(func.count(WatchedItem.id))
    stmt = _apply_watched_item_filters(stmt, search=search, include_archived=include_archived)
    result = await session.execute(stmt)
    return result.scalar_one()


async def get_watched_item_detail(
    session: AsyncSession, watched_item_id: str
) -> WatchedItem | None:
    """Fetch a single WatchedItem; returns None on invalid ID or not-found."""
    try:
        wi_ulid = ULID.from_str(watched_item_id)
    except (ValueError, TypeError):
        return None
    return await session.get(WatchedItem, wi_ulid)


_WI_ACTIVITY_SUMMARY: dict[str, str] = {
    EventType.CHECK_SNAPSHOT_CREATED: "Checked — change captured",
    EventType.CHECK_NO_CHANGE: "Checked — no change",
    EventType.CHECK_FETCH_FAILED: "Fetch failed",
    EventType.WATCHED_ITEM_CHECK_REQUESTED: "Manual check requested",
    EventType.WATCHED_ITEM_CREATED: "Watched Item created",
    EventType.WATCHED_ITEM_UPDATED: "Watched Item updated",
    EventType.WATCHED_ITEM_PAUSED: "Paused",
    EventType.WATCHED_ITEM_RESUMED: "Resumed",
    EventType.WATCHED_ITEM_ARCHIVED: "Archived",
    EventType.WATCHED_ITEM_RESTORED: "Restored",
    EventType.WATCHED_ITEM_REVIEWED: "Marked reviewed",
}


async def get_watched_item_activity(
    session: AsyncSession, watched_item_id: str, limit: int = 20
) -> list[dict]:
    """Recent audit activity for a WatchedItem (checks + lifecycle), newest first.

    Check events are WatchedItem-scoped post-#185; their identifier lives in the
    audit payload (``watched_item_id``), so this filters on the JSONB field.
    """
    stmt = (
        select(AuditLog)
        .where(AuditLog.payload["watched_item_id"].astext == str(watched_item_id))
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    out: list[dict] = []
    for row in rows:
        summary = _WI_ACTIVITY_SUMMARY.get(row.event_type, row.event_type)
        if row.event_type == EventType.CHECK_FETCH_FAILED:
            status = (row.payload or {}).get("status_code")
            if status:
                summary = f"Fetch failed — HTTP {status}"
        out.append({"event_type": row.event_type, "timestamp": row.created_at, "summary": summary})
    return out


async def get_global_default_templates(
    session: AsyncSession,
) -> list[NotificationTemplate]:
    """Global templates (``visibility='global'``) — they fire for every item (#200).

    Not filtered by ``is_active``, so an operator can see an inactive global
    (status badge shows it won't fire). Dispatch additionally filters on
    ``is_active`` and per-event membership.
    """
    result = await session.execute(
        select(NotificationTemplate)
        .where(NotificationTemplate.visibility == VISIBILITY_GLOBAL)
        .order_by(NotificationTemplate.title)
    )
    return list(result.scalars().all())


async def get_domain_default_templates(
    session: AsyncSession, domain_name: str | None
) -> list[NotificationTemplate]:
    """Domain templates (``visibility='domain'``) for the item's domain (#200).

    These fire at dispatch for items whose ``domain_name`` matches. Returns an
    empty list when the item has no domain.
    """
    if not domain_name:
        return []
    result = await session.execute(
        select(NotificationTemplate)
        .where(
            NotificationTemplate.visibility == VISIBILITY_DOMAIN,
            NotificationTemplate.domain_name == domain_name,
        )
        .order_by(NotificationTemplate.title)
    )
    return list(result.scalars().all())
