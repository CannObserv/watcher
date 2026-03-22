"""Dashboard context helpers — DB queries for stats, changes, queue health."""

from sqlalchemy.ext.asyncio import AsyncSession


async def get_dashboard_stats(session: AsyncSession) -> dict:
    """Aggregate counts for dashboard stat cards."""
    return {"total_watches": 0, "active_watches": 0, "changes_today": 0, "checks_today": 0}


async def get_recent_changes(session: AsyncSession, limit: int = 20) -> list[dict]:
    """Fetch recent changes with watch names for display."""
    return []


async def get_queue_health(session: AsyncSession) -> dict:
    """Query procrastinate_jobs table for queue status counts."""
    return {"todo": 0, "doing": 0, "failed": 0, "succeeded_today": 0}


def get_rate_limiter_state() -> list[dict]:
    """Get current rate limiter domain states."""
    return []
