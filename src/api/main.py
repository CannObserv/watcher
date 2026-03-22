"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select

from src.api.routes.audit_log import router as audit_router
from src.api.routes.changes import router as changes_router
from src.api.routes.domains import router as domains_router
from src.api.routes.notification_configs import router as notification_configs_router
from src.api.routes.probe import router as probe_router
from src.api.routes.temporal_profiles import router as profiles_router
from src.api.routes.watches import router as watches_router
from src.core.database import get_session_factory
from src.core.logging import configure_logging, get_logger
from src.core.models.domain import Domain
from src.core.rate_limiter import DomainRateLimiter, get_rate_limiter
from src.dashboard import register_dashboard

configure_logging()
logger = get_logger(__name__)


async def hydrate_rate_limiter(limiter: DomainRateLimiter) -> None:
    """Load persisted domain configs into the rate limiter at startup."""
    async with get_session_factory()() as session:
        result = await session.execute(select(Domain))
        domains = result.scalars().all()
    for d in domains:
        limiter.configure_domain(
            name=d.name,
            max_concurrency=d.max_concurrency,
            current_interval=d.current_interval,
        )
    logger.info("rate limiter hydrated", extra={"domain_count": len(domains)})


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Hydrate rate limiter and start procrastinate worker at startup."""
    from src.workers import get_app

    await hydrate_rate_limiter(get_rate_limiter())

    proc_app = get_app()
    await proc_app.open_async()
    worker_task = asyncio.create_task(proc_app.run_worker_async(install_signal_handlers=False))
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await proc_app.close_async()


app = FastAPI(title="watcher", version="0.1.0", lifespan=lifespan)
app.include_router(watches_router)
app.include_router(changes_router)
app.include_router(profiles_router)
app.include_router(notification_configs_router)
app.include_router(audit_router)
app.include_router(domains_router)
app.include_router(probe_router)
register_dashboard(app)
