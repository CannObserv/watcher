"""Dashboard context helpers — DB queries for stats, changes, queue health."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.change import Change
from src.core.models.domain import Domain
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watch import Watch


def summarize_change_metadata(metadata: dict) -> str:
    """Summarize change metadata as a human-readable string.

    Counts added, modified, and removed chunks from *metadata* and returns
    a comma-joined description (e.g. ``"2 added, 1 modified"``).  Returns
    ``"change detected"`` when all counts are zero or keys are absent.
    """
    added = len(metadata.get("added", []))
    modified = len(metadata.get("modified", []))
    removed = len(metadata.get("removed", []))
    parts = []
    if added:
        parts.append(f"{added} added")
    if modified:
        parts.append(f"{modified} modified")
    if removed:
        parts.append(f"{removed} removed")
    return ", ".join(parts) if parts else "change detected"


_WATCH_SORT_COLS: dict[str, Any] = {
    "name": Watch.name,
    "status": Watch.is_active,
    "health": Watch.health_status,
    "last_checked_at": Watch.last_checked_at,
    "last_changed_at": Watch.last_changed_at,
    "created_at": Watch.created_at,
}


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

    Default sort is ``last_checked_at desc`` (changed from ``created_at`` in #101).
    """
    col = _WATCH_SORT_COLS.get(sort, Watch.last_checked_at)
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
        stmt = stmt.where(Watch.effective_domain.ilike(f"%{escaped}%"))
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_dashboard_stats(session: AsyncSession) -> dict:
    """Aggregate counts for dashboard stat cards."""
    total = await session.scalar(select(func.count(Watch.id)))
    active = await session.scalar(select(func.count(Watch.id)).where(Watch.is_active.is_(True)))

    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    changes_today = await session.scalar(
        select(func.count(Change.id)).where(Change.detected_at >= today_start)
    )
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
        "changes_today": changes_today or 0,
        "checks_today": checks_today or 0,
    }


async def get_recent_changes(session: AsyncSession, limit: int = 20) -> list[dict]:
    """Fetch recent changes with watch names for display."""
    stmt = (
        select(Change, Watch.name)
        .join(Watch, Change.watch_id == Watch.id)
        .order_by(Change.detected_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.all()

    changes = []
    for change, watch_name in rows:
        summary = summarize_change_metadata(change.change_metadata or {})

        changes.append(
            {
                "id": str(change.id),
                "watch_id": str(change.watch_id),
                "watch_name": watch_name,
                "detected_at": change.detected_at,
                "summary": summary,
                "visual_change_score": change.visual_change_score,
            }
        )
    return changes


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


async def get_change_detail(session: AsyncSession, change_id: str) -> dict | None:
    """Fetch a change with its snapshots, chunks, and watch name."""
    try:
        parsed = ULID.from_str(change_id)
    except ValueError:
        return None

    change = await session.get(Change, parsed)
    if not change:
        return None

    # Watch name
    watch = await session.get(Watch, change.watch_id)
    watch_name = watch.name if watch else "Unknown"

    # Snapshots
    prev_snap = await session.get(Snapshot, change.previous_snapshot_id)
    curr_snap = await session.get(Snapshot, change.current_snapshot_id)

    # Chunks for current snapshot
    curr_chunks = []
    if curr_snap:
        stmt = (
            select(SnapshotChunk)
            .where(SnapshotChunk.snapshot_id == curr_snap.id)
            .order_by(SnapshotChunk.chunk_index)
        )
        result = await session.execute(stmt)
        curr_chunks = list(result.scalars().all())

    # Chunks for previous snapshot
    prev_chunks = []
    if prev_snap:
        stmt = (
            select(SnapshotChunk)
            .where(SnapshotChunk.snapshot_id == prev_snap.id)
            .order_by(SnapshotChunk.chunk_index)
        )
        result = await session.execute(stmt)
        prev_chunks = list(result.scalars().all())

    return {
        "change": change,
        "watch_name": watch_name,
        "watch_id": str(change.watch_id),
        "current_snapshot": curr_snap,
        "previous_snapshot": prev_snap,
        "current_chunks": curr_chunks,
        "previous_chunks": prev_chunks,
    }


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


async def get_watch_changes(session: AsyncSession, watch_id: str, limit: int = 50) -> list[dict]:
    """Fetch change history for a specific watch."""
    try:
        parsed = ULID.from_str(watch_id)
    except ValueError:
        return []
    stmt = (
        select(Change)
        .where(Change.watch_id == parsed)
        .order_by(Change.detected_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    changes = []
    for change in result.scalars().all():
        changes.append(
            {
                "id": str(change.id),
                "detected_at": change.detected_at,
                "summary": summarize_change_metadata(change.change_metadata or {}),
            }
        )
    return changes


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
    """Return a unified lifecycle event timeline for a watch.

    Merges AuditLog entries, Snapshot rows (as pipeline run events), and
    Change rows (as change-detected events) into a single chronological list
    sorted newest-first.  Supports offset-based pagination.

    Each entry is a dict with keys:
    - ``event_type`` — string identifier for the event
    - ``timestamp`` — timezone-aware datetime
    - ``summary`` — short human-readable description
    - ``detail_url`` — optional URL for a detail page (or ``None``)
    - ``category`` — one of ``"change"``, ``"error"``, ``"config"``, ``"run"``
    """
    try:
        parsed = ULID.from_str(watch_id)
    except ValueError:
        return []

    # --- AuditLog rows (config + error events; excludes CHECK_SNAPSHOT_CREATED to avoid
    #     double-counting with the Snapshot rows fetched below) ---
    audit_stmt = select(
        AuditLog.event_type.label("event_type"),
        AuditLog.created_at.label("timestamp"),
        AuditLog.payload.label("payload"),
        AuditLog.id.label("source_id"),
    ).where(
        AuditLog.watch_id == parsed,
        AuditLog.event_type != EventType.CHECK_SNAPSHOT_CREATED,
    )

    # --- Snapshot rows (authoritative source for pipeline run events) ---
    snapshot_stmt = select(
        Snapshot.id.label("source_id"),
        Snapshot.fetched_at.label("timestamp"),
    ).where(Snapshot.watch_id == parsed)

    # --- Change rows ---
    change_stmt = select(
        Change.id.label("source_id"),
        Change.detected_at.label("timestamp"),
        Change.change_metadata.label("change_metadata"),
    ).where(Change.watch_id == parsed)

    # Execute all three queries
    audit_rows = list((await session.execute(audit_stmt)).all())
    snapshot_rows = list((await session.execute(snapshot_stmt)).all())
    change_rows = list((await session.execute(change_stmt)).all())

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

    for row in snapshot_rows:
        entries.append(
            {
                "event_type": "check.snapshot_created",
                "timestamp": row.timestamp,
                "summary": "Snapshot fetched",
                "detail_url": None,
                "category": "run",
            }
        )

    for row in change_rows:
        meta = row.change_metadata or {}
        summary = summarize_change_metadata(meta)
        entries.append(
            {
                "event_type": "change.detected",
                "timestamp": row.timestamp,
                "summary": summary,
                "detail_url": f"/changes/{row.source_id}",
                "category": "change",
            }
        )

    # Sort all entries newest-first, then apply pagination
    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    return entries[offset : offset + limit]


async def get_watch_timeline_count(
    session: AsyncSession,
    watch_id: str,
) -> int:
    """Return total number of timeline entries for a watch (for pagination)."""
    try:
        parsed = ULID.from_str(watch_id)
    except ValueError:
        return 0

    audit_count = (
        await session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.watch_id == parsed,
                AuditLog.event_type != EventType.CHECK_SNAPSHOT_CREATED,
            )
        )
        or 0
    )
    snapshot_count = (
        await session.scalar(select(func.count(Snapshot.id)).where(Snapshot.watch_id == parsed))
        or 0
    )
    change_count = (
        await session.scalar(select(func.count(Change.id)).where(Change.watch_id == parsed)) or 0
    )
    return audit_count + snapshot_count + change_count


async def get_latest_snapshot(session: AsyncSession, watch_id: ULID) -> Snapshot | None:
    """Fetch the most recent snapshot for a watch, or None."""
    stmt = (
        select(Snapshot)
        .where(Snapshot.watch_id == watch_id)
        .order_by(Snapshot.fetched_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


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
    if status == "active":
        stmt = stmt.where(
            Domain.archived_at.is_(None),
            Domain.current_interval <= Domain.min_interval,
        )
    elif status == "archived":
        stmt = stmt.where(Domain.archived_at.isnot(None))
    elif status == "backoff":
        stmt = stmt.where(
            Domain.archived_at.is_(None),
            Domain.current_interval > Domain.min_interval,
        )
    return stmt


async def get_domains_with_watch_counts(
    session: AsyncSession,
    *,
    search: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int | None = None,
) -> list[dict]:
    """Fetch domains with watch count, last_checked, search, filter, and pagination.

    Args:
        search: Substring match on domain name.
        status: Filter — "active", "archived", "backoff", or None (all).
        page: 1-based page number (only used when page_size is set).
        page_size: Results per page. None means no pagination (return all).
    """
    stmt = (
        select(
            Domain,
            func.count(Watch.id).label("watch_count"),
            func.max(Watch.last_checked_at).label("last_checked"),
        )
        .outerjoin(Watch, Watch.effective_domain == Domain.name)
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
            "watch_count": watch_count,
            "last_checked": last_checked,
            "status": domain.status,
            "notes": domain.notes,
            "archived_at": domain.archived_at,
        }
        for domain, watch_count, last_checked in rows
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


async def get_domain_watches(
    session: AsyncSession,
    domain_name: str,
    *,
    search: str | None = None,
    is_active: bool | None = None,
    sort: str = "name",
    order: str = "asc",
) -> list[Watch]:
    """Fetch watches for a domain with optional name search, status filter, and sorting."""
    col = _WATCH_SORT_COLS.get(sort, Watch.name)
    order_expr = col.asc().nulls_first() if order == "asc" else col.desc().nulls_last()
    stmt = select(Watch).where(Watch.effective_domain == domain_name).order_by(order_expr)
    if search:
        escaped = search.replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Watch.name.ilike(f"%{escaped}%"))
    if is_active is not None:
        stmt = stmt.where(Watch.is_active == is_active)
    result = await session.execute(stmt)
    return list(result.scalars().all())
