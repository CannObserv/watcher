"""FastAPI application entry point."""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI

from src.api.deps import require_api_key
from src.api.routes.audit_log import router as audit_router
from src.api.routes.domains import router as domains_router
from src.api.routes.health import router as health_router
from src.api.routes.notification_templates import router as notification_templates_router
from src.api.routes.probe import router as probe_router
from src.api.routes.temporal_profiles import router as profiles_router
from src.api.routes.watched_item_notifications import (
    router as watched_item_notifications_router,
)
from src.api.routes.watched_items import router as watched_items_router
from src.core.bus import BUS_REDIS_URL_ENV, aclose_shared_bus_client, get_shared_bus_client
from src.core.database import get_session_factory
from src.core.db_safety import ProductionDatabaseRefused, assert_environment_db_allowed
from src.core.logging import configure_logging, get_logger
from src.core.registry import get_registry
from src.dashboard import register_dashboard

# Worker imports are safe at module top: src.workers.__init__ defers task-module
# registration into get_app(), and fetch_facts only touches bp-registered tasks
# (#241 CR-7; previously inline in the lifespan).
from src.workers import get_app
from src.workers.fetch_facts import start_blobs_consumer
from src.workers.registry_reconcile import start_registry_consumer

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Pre-warm the SDK, start the fact consumer and the procrastinate worker.

    Refuses to serve a production database unless the caller opted in via
    WATCHER_ALLOW_PRODUCTION_DB=1 (only deploy/watcher.service does) —
    launch-path-independent backstop for the prod-pointing dev-server recipe
    (#233); see src.core.db_safety. Runs before any resource is built, so a
    refused process never starts the consumer or the worker.

    Pre-warming the ArchiverClient on startup means a missing ARCHIVER_API_KEY
    crashes the API on boot, not on first request. The SDK is closed last on shutdown,
    after the worker is fully gathered and the procrastinate app has closed.
    """
    try:
        assert_environment_db_allowed(os.environ)
    except ProductionDatabaseRefused as e:
        # Log before re-raising: under systemd the bare exception surfaces in
        # journalctl as a lifespan traceback, burying the actionable text.
        logger.critical("Refusing to start: %s", e)
        raise

    # Pre-warm the ArchiverClient — raises if ARCHIVER_API_KEY is unset.
    registry = get_registry()
    registry.get_archiver_client()
    logger.info("archiver client pre-warmed")

    # Phase 4 (#241): the content.blobs fact consumer — the only inbound path
    # for check results now that Watcher itself does not fetch. Without a bus
    # URL no facts can arrive at all, so the process is issue-only and every
    # command will eventually be reaped.
    bus_client = get_shared_bus_client()
    consumer_stop = asyncio.Event()
    consumer_task = None
    registry_task = None
    if bus_client is not None:
        consumer_task = start_blobs_consumer(bus_client, get_session_factory(), stop=consumer_stop)
        logger.info("content.blobs consumer started")
        # #254: the info.registry reconcile — Watcher's registry inbox. Groupless
        # tail, replayed from 0-0 at boot, so a fresh process converges from the
        # snapshot alone. Shares the stop event: both are registry/fact inboxes
        # with the same shutdown story.
        registry_task = start_registry_consumer(
            bus_client, get_session_factory(), stop=consumer_stop
        )
        logger.info("info.registry consumer started")
    else:
        # Not a degraded mode any more: with no fact inbox, issued commands
        # can never be applied and every one of them will be reaped.
        logger.error(
            "content.blobs and info.registry consumers NOT started: %s is not set — "
            "no check can complete and the registry cannot reconcile",
            BUS_REDIS_URL_ENV,
        )

    proc_app = get_app()
    await proc_app.open_async()
    worker_task = asyncio.create_task(proc_app.run_worker_async(install_signal_handlers=False))
    yield
    consumer_stop.set()
    worker_task.cancel()
    if consumer_task is not None:
        # The stop event alone leaves up to BLOCK_MS of read latency; a cancel
        # is safe (commit-then-ack means a cancelled ack just redelivers, and
        # the upsert + apply guard make redelivery a no-op).
        consumer_task.cancel()
    if registry_task is not None:
        # Same reasoning, and cheaper still: the reconcile commits before it
        # returns and the generation guard makes a re-read a no-op, so a cancel
        # mid-read costs at most one replayed announcement at next boot.
        registry_task.cancel()
    tasks = [t for t in (worker_task, consumer_task, registry_task) if t is not None]
    await asyncio.gather(*tasks, return_exceptions=True)
    await aclose_shared_bus_client()
    await proc_app.close_async()
    # SDK close must be the last shutdown step (no consumer can still be in flight).
    await registry.aclose_archiver_client()


app = FastAPI(title="watcher", version="0.1.0", lifespan=lifespan)

v1_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
v1_router.include_router(profiles_router)
v1_router.include_router(watched_item_notifications_router)
v1_router.include_router(notification_templates_router)
v1_router.include_router(audit_router)
v1_router.include_router(domains_router)
v1_router.include_router(probe_router)
v1_router.include_router(watched_items_router)
app.include_router(v1_router)
app.include_router(health_router)
register_dashboard(app)
