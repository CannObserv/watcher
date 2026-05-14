"""Effective root cadence = min(root.schedule, min(fragment_schedules))."""

from archiver_client import ArchiverClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.models.watch import Watch


async def effective_root_cadence_seconds(
    session: AsyncSession,
    client: ArchiverClient,
    root_watch: Watch,
) -> int:
    """Return min(root.interval, min(fragment_watch_intervals)).

    Queries Archiver for fragment InfoSources of the root_watch's source,
    then finds active Watches on those fragments. Returns the smallest
    schedule_config["interval_seconds"] (default 3600) across the root +
    any fragment Watches.
    """
    page = await client.list_info_sources(parent_info_source_id=str(root_watch.info_source_id))
    frag_ids = [str(f.info_source_id) for f in page.items]
    intervals = [int(root_watch.schedule_config.get("interval_seconds", 3600))]
    if frag_ids:
        result = await session.execute(
            select(Watch.schedule_config)
            .where(Watch.info_source_id.in_(frag_ids))
            .where(Watch.is_active.is_(True))
            .where(Watch.is_archived.is_(False))
        )
        for (cfg,) in result.all():
            intervals.append(int(cfg.get("interval_seconds", 3600)))
    return min(intervals)
