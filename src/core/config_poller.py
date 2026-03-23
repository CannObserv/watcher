"""Background config poller — syncs domain configs from DB into rate limiter."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from src.core.logging import get_logger
from src.core.models.domain import Domain
from src.core.rate_limiter import DomainRateLimiter

logger = get_logger(__name__)

DEFAULT_POLL_INTERVAL = 60


async def poll_domain_configs(
    limiter: DomainRateLimiter,
    session_factory,
    last_poll: datetime,
) -> datetime:
    """Poll for domain configs updated since last_poll. Returns new poll timestamp.

    On DB error, logs warning and returns last_poll unchanged (retry next cycle).
    """
    now = datetime.now(UTC)
    try:
        async with session_factory() as session:
            stmt = select(Domain).where(Domain.updated_at > last_poll)
            result = await session.execute(stmt)
            domains = result.scalars().all()
        for d in domains:
            limiter.configure_domain(
                name=d.name,
                max_concurrency=d.max_concurrency,
                min_interval=d.min_interval,
                current_interval=d.current_interval,
            )
        if domains:
            logger.info("config poller synced domains", extra={"count": len(domains)})
    except Exception:
        logger.warning("config poller DB error, will retry next cycle", exc_info=True)
        return last_poll
    return now


async def start_config_poller(
    limiter: DomainRateLimiter,
    session_factory,
    interval: int = DEFAULT_POLL_INTERVAL,
) -> asyncio.Task:
    """Start background task that polls domain configs every `interval` seconds."""

    async def _poll_loop():
        last_poll = datetime.now(UTC)
        while True:
            await asyncio.sleep(interval)
            last_poll = await poll_domain_configs(limiter, session_factory, last_poll)

    return asyncio.create_task(_poll_loop())
