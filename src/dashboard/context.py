"""Dashboard context helpers — DB queries for stats, changes, queue health."""

from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.models.audit_log import AuditLog
from src.core.models.change import Change
from src.core.models.notification_config import NotificationConfig
from src.core.models.temporal_profile import TemporalProfile
from src.core.models.watch import Watch


async def get_watch_list(session: AsyncSession, is_active: bool | None = None) -> list[Watch]:
    """Fetch watches for list display, optionally filtered by active status."""
    stmt = select(Watch).order_by(Watch.created_at.desc())
    if is_active is not None:
        stmt = stmt.where(Watch.is_active == is_active)
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
                ["check.snapshot_created", "check.no_change", "check.fetch_failed"]
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
        meta = change.change_metadata or {}
        added = len(meta.get("added", []))
        modified = len(meta.get("modified", []))
        removed = len(meta.get("removed", []))
        parts = []
        if added:
            parts.append(f"{added} added")
        if modified:
            parts.append(f"{modified} modified")
        if removed:
            parts.append(f"{removed} removed")
        summary = ", ".join(parts) if parts else "change detected"

        changes.append(
            {
                "id": str(change.id),
                "watch_id": str(change.watch_id),
                "watch_name": watch_name,
                "detected_at": change.detected_at,
                "summary": summary,
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
        meta = change.change_metadata or {}
        added = len(meta.get("added", []))
        modified = len(meta.get("modified", []))
        removed = len(meta.get("removed", []))
        parts = []
        if added:
            parts.append(f"{added} added")
        if modified:
            parts.append(f"{modified} modified")
        if removed:
            parts.append(f"{removed} removed")
        changes.append(
            {
                "id": str(change.id),
                "detected_at": change.detected_at,
                "summary": ", ".join(parts) if parts else "change detected",
            }
        )
    return changes


async def get_watch_profiles(session: AsyncSession, watch_id: ULID) -> list[TemporalProfile]:
    """Fetch temporal profiles for a watch."""
    result = await session.execute(
        select(TemporalProfile).where(TemporalProfile.watch_id == watch_id)
    )
    return list(result.scalars().all())


async def get_watch_notifications(
    session: AsyncSession, watch_id: ULID
) -> list[NotificationConfig]:
    """Fetch notification configs for a watch."""
    result = await session.execute(
        select(NotificationConfig).where(NotificationConfig.watch_id == watch_id)
    )
    return list(result.scalars().all())
