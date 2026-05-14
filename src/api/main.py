"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy import select

from src.api.deps import require_api_key
from src.api.routes.audit_log import router as audit_router
from src.api.routes.domains import router as domains_router
from src.api.routes.health import router as health_router
from src.api.routes.notification_configs import router as notification_configs_router
from src.api.routes.notification_templates import router as notification_templates_router
from src.api.routes.probe import router as probe_router
from src.api.routes.temporal_profiles import router as profiles_router
from src.api.routes.watches import router as watches_router
from src.core.config_poller import start_config_poller
from src.core.database import get_session_factory
from src.core.logging import configure_logging, get_logger
from src.core.models.domain import Domain
from src.core.rate_limiter import DomainRateLimiter, get_rate_limiter
from src.core.registry import get_registry
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
            min_interval=d.min_interval,
            current_interval=d.current_interval,
        )
    logger.info("rate limiter hydrated", extra={"domain_count": len(domains)})


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Hydrate rate limiter, pre-warm SDK, start config poller and procrastinate worker.

    Pre-warming the ArchiverClient on startup means a missing ARCHIVER_API_KEY
    crashes the API on boot, not on first request. The SDK is closed last on shutdown,
    after the worker is fully gathered and the procrastinate app has closed.
    """
    from src.workers import get_app

    limiter = get_rate_limiter()
    await hydrate_rate_limiter(limiter)

    # Pre-warm the ArchiverClient — raises if ARCHIVER_API_KEY is unset.
    registry = get_registry()
    registry.get_archiver_client()
    logger.info("archiver client pre-warmed")

    poller_task = await start_config_poller(limiter, get_session_factory())

    proc_app = get_app()
    await proc_app.open_async()
    worker_task = asyncio.create_task(proc_app.run_worker_async(install_signal_handlers=False))
    yield
    poller_task.cancel()
    worker_task.cancel()
    await asyncio.gather(poller_task, worker_task, return_exceptions=True)
    await proc_app.close_async()
    # SDK close must be the last shutdown step (no consumer can still be in flight).
    await registry.aclose_archiver_client()


app = FastAPI(title="watcher", version="0.1.0", lifespan=lifespan)

v1_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
v1_router.include_router(watches_router)
v1_router.include_router(profiles_router)
v1_router.include_router(notification_configs_router)
v1_router.include_router(notification_templates_router)
v1_router.include_router(audit_router)
v1_router.include_router(domains_router)
v1_router.include_router(probe_router)
app.include_router(v1_router)
app.include_router(health_router)
register_dashboard(app)
