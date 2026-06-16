"""Dashboard context helpers — DB queries for stats, changes, queue health."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.domain import Domain
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watch import Watch
from src.core.models.watched_item import WatchedItem
from src.core.models.watched_item_notification_template import (
    WatchedItemNotificationTemplate,
)

_WATCH_SORT_COLS: dict[str, Any] = {
    "name": Watch.name,
    "status": Watch.is_active,
    "created_at": Watch.created_at,
}

_DOMAIN_WI_SORT_COLS: dict[str, Any] = {
    "name": WatchedItem.name,
    "last_checked_at": WatchedItem.last_checked_at,
}


def _wi_subq(col):
    """Correlated subquery: SELECT col FROM watched_items WHERE id = watches.watched_item_id."""
    return select(col).where(WatchedItem.id == Watch.watched_item_id).scalar_subquery()


async def get_watch_list(
    session: AsyncSession,
    is_active: bool | None = None,
    include_archived: bool = False,
    search: str | None = None,
    domain: str | None = None,
    sort: str = "last_checked_at",
    order: str = "desc",
) -> list[Watch]:
    """Fetch watches for list display with optional filtering and sorting.

    Sorting by health/last_checked_at/last_changed_at uses correlated subqueries
    against the parent WatchedItem (these columns moved from Watch in #185 Phase A
    step 6). Default sort is ``last_checked_at desc``.
    """
    wi_sort: dict[str, Any] = {
        "health": _wi_subq(WatchedItem.health_status),
        "last_checked_at": _wi_subq(WatchedItem.last_checked_at),
        "last_changed_at": _wi_subq(WatchedItem.last_changed_at),
    }
    col = _WATCH_SORT_COLS.get(sort) or wi_sort.get(sort, _wi_subq(WatchedItem.last_checked_at))
    order_expr = col.asc().nulls_first() if order == "asc" else col.desc().nulls_last()
    stmt = select(Watch).order_by(order_expr)
    if is_active is not None:
        stmt = stmt.where(Watch.is_active == is_active)
    if not include_archived:
        stmt = stmt.where(Watch.is_archived.is_(False))
    if search:
        escaped = search.replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Watch.name.ilike(f"%{escaped}%"))
    if domain:
        escaped = domain.replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(
            Watch.watched_item_id.in_(
                select(WatchedItem.id).where(WatchedItem.domain_name.ilike(f"%{escaped}%"))
            )
        )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_dashboard_stats(session: AsyncSession) -> dict:
    """Aggregate counts for dashboard stat cards.

    Phase 5 (#156): changes_today is always 0 — Change table dropped.
    """
    total = await session.scalar(select(func.count(Watch.id)))
    active = await session.scalar(select(func.count(Watch.id)).where(Watch.is_active.is_(True)))

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
    watch_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditLog]:
    """Fetch audit log entries with optional filters."""
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc())
    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)
    if watch_id:
        try:
            parsed = ULID.from_str(watch_id)
            stmt = stmt.where(AuditLog.watch_id == parsed)
        except ValueError:
            pass
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_watch_detail(session: AsyncSession, watch_id: str) -> Watch | None:
    """Fetch a single watch by ID string. Returns None if not found or invalid."""
    try:
        parsed = ULID.from_str(watch_id)
    except ValueError:
        return None
    return await session.get(Watch, parsed)


_TIMELINE_SUMMARY: dict[str, str] = {
    EventType.WATCH_CREATED: "Watch created",
    EventType.WATCH_UPDATED: "Watch config updated",
    EventType.WATCH_DEACTIVATED: "Watch deactivated",
    EventType.WATCH_ARCHIVED: "Watch archived",
    EventType.WATCH_RESTORED: "Watch restored",
    EventType.WATCH_DELETED: "Watch deleted",
    EventType.CHECK_SNAPSHOT_CREATED: "Snapshot fetched",
    EventType.CHECK_NO_CHANGE: "Checked — no change",
    EventType.CHECK_FETCH_FAILED: "Fetch failed",
    EventType.NOTIFICATION_DISPATCHED: "Notification dispatched",
    EventType.NOTIFICATION_CONFIG_CREATED: "Notification config added",
    EventType.NOTIFICATION_CONFIG_DELETED: "Notification config removed",
    EventType.PROFILE_CREATED: "Temporal profile added",
    EventType.PROFILE_UPDATED: "Temporal profile updated",
    EventType.PROFILE_DELETED: "Temporal profile removed",
}

_TIMELINE_CATEGORY: dict[str, str] = {
    EventType.WATCH_CREATED: "config",
    EventType.WATCH_UPDATED: "config",
    EventType.WATCH_DEACTIVATED: "config",
    EventType.WATCH_ARCHIVED: "config",
    EventType.WATCH_RESTORED: "config",
    EventType.WATCH_DELETED: "config",
    EventType.CHECK_SNAPSHOT_CREATED: "run",
    EventType.CHECK_NO_CHANGE: "run",
    EventType.CHECK_FETCH_FAILED: "error",
    EventType.NOTIFICATION_DISPATCHED: "run",
    EventType.NOTIFICATION_CONFIG_CREATED: "config",
    EventType.NOTIFICATION_CONFIG_DELETED: "config",
    EventType.PROFILE_CREATED: "config",
    EventType.PROFILE_UPDATED: "config",
    EventType.PROFILE_DELETED: "config",
}


async def get_watch_timeline(
    session: AsyncSession,
    watch_id: str,
    offset: int = 0,
    limit: int = 50,
) -> list[dict]:
    """Return lifecycle event timeline for a watch.

    Sources AuditLog entries into a chronological list sorted newest-first.
    Supports offset-based pagination.

    Phase 5 (#156): Snapshot + Change tables dropped — timeline is AuditLog-only.

    Each entry is a dict with keys:
    - ``event_type`` — string identifier for the event
    - ``timestamp`` — timezone-aware datetime
    - ``summary`` — short human-readable description
    - ``detail_url`` — optional URL for a detail page (or ``None``)
    - ``category`` — one of ``"error"``, ``"config"``, ``"run"``
    """
    try:
        parsed = ULID.from_str(watch_id)
    except ValueError:
        return []

    # Phase 5 (#156): Snapshot table dropped. Timeline now sourced from AuditLog only.
    audit_stmt = select(
        AuditLog.event_type.label("event_type"),
        AuditLog.created_at.label("timestamp"),
        AuditLog.payload.label("payload"),
        AuditLog.id.label("source_id"),
    ).where(AuditLog.watch_id == parsed)

    audit_rows = list((await session.execute(audit_stmt)).all())

    entries: list[dict] = []

    for row in audit_rows:
        et = row.event_type
        category = _TIMELINE_CATEGORY.get(et, "config")
        summary = _TIMELINE_SUMMARY.get(et, et)
        # Enrich fetch-failed summary with error detail from payload
        if et == EventType.CHECK_FETCH_FAILED:
            payload = row.payload or {}
            error_msg = payload.get("error") or payload.get("message") or ""
            if error_msg:
                summary = f"Fetch failed — {error_msg[:80]}"
        entries.append(
            {
                "event_type": et,
                "timestamp": row.timestamp,
                "summary": summary,
                "detail_url": None,
                "category": category,
            }
        )

    # Sort all entries newest-first, then apply pagination
    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    return entries[offset : offset + limit]


async def get_watch_timeline_count(
    session: AsyncSession,
    watch_id: str,
) -> int:
    """Return total number of timeline entries for a watch (for pagination).

    Phase 5 (#156): Snapshot table dropped — count is AuditLog rows only.
    """
    try:
        parsed = ULID.from_str(watch_id)
    except ValueError:
        return 0

    return (
        await session.scalar(select(func.count(AuditLog.id)).where(AuditLog.watch_id == parsed))
        or 0
    )


async def get_watch_profiles(session: AsyncSession, watch_id: ULID) -> list[TemporalProfile]:
    """Fetch temporal profiles for a watch."""
    result = await session.execute(
        select(TemporalProfile).where(TemporalProfile.watch_id == watch_id)
    )
    return list(result.scalars().all())


async def get_watch_notifications(
    session: AsyncSession, watch_id: ULID
) -> list[WatchNotificationConfig]:
    """Fetch notification configs for a watch."""
    result = await session.execute(
        select(WatchNotificationConfig).where(WatchNotificationConfig.watch_id == watch_id)
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
        .outerjoin(Watch, Watch.watched_item_id == WatchedItem.id)
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


async def get_watched_item_templates(
    session: AsyncSession, watched_item_id: ULID
) -> list[WatchedItemNotificationTemplate]:
    """Load notification templates under a WatchedItem (created_at asc)."""
    result = await session.execute(
        select(WatchedItemNotificationTemplate)
        .where(WatchedItemNotificationTemplate.watched_item_id == watched_item_id)
        .order_by(WatchedItemNotificationTemplate.created_at)
    )
    return list(result.scalars().all())
