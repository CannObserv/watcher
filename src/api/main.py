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
from src.core.bus import (
    BUS_REDIS_URL_ENV,
    BusNotEnabled,
    aclose_shared_bus_client,
    assert_environment_bus_allowed,
    get_shared_bus_client,
    probe_bus_reachable,
)
from src.core.database import get_session_factory
from src.core.db_safety import ProductionDatabaseRefused, assert_environment_db_allowed
from src.core.logging import configure_logging, get_logger
from src.core.notifier_client import (
    NotifierCredentialMissing,
    NotifierNotEnabled,
    assert_environment_notifier_allowed,
)
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
    """Start the fact and registry consumers plus the procrastinate worker.

    Refuses to serve a production database unless the caller opted in via
    WATCHER_ALLOW_PRODUCTION_DB=1 (only deploy/watcher.service does) —
    launch-path-independent backstop for the prod-pointing dev-server recipe
    (#233); see src.core.db_safety. Runs before any resource is built, so a
    refused process never starts the consumer or the worker.

    Refuses the same way for a bus URL held without WATCHER_BUS_ENABLED=1
    (#262), for a notifier URL held without WATCHER_NOTIFIER_ENABLED=1 (#277), and
    for that flag held without a URL (#278) — which means the unit's
    /etc/watcher/notifier.env did not load. The
    enforcement point is here rather than at import of src.core.bus or
    src.core.notifier_client: an import-time check would abort alembic,
    everything under scripts/, and anything else that transitively imports the
    module — including the tooling used to deploy the fix.

    Nothing to pre-warm since #254: the Archiver SDK went with Watcher's last
    outbound call to Archiver, so there is no client to build, no
    ARCHIVER_API_KEY to fail fast on, and no close to order at shutdown.
    """
    try:
        assert_environment_db_allowed(os.environ)
        assert_environment_bus_allowed(os.environ)
        assert_environment_notifier_allowed(os.environ)
    except (
        ProductionDatabaseRefused,
        BusNotEnabled,
        NotifierNotEnabled,
        NotifierCredentialMissing,
    ) as e:
        # Log before re-raising: under systemd the bare exception surfaces in
        # journalctl as a lifespan traceback, burying the actionable text.
        logger.critical("Refusing to start: %s", e)
        raise

    # Phase 4 (#241): the content.blobs fact consumer — the only inbound path
    # for check results now that Watcher itself does not fetch. Without a bus
    # URL no facts can arrive at all, so the process is issue-only and every
    # command will eventually be reaped.
    bus_client = get_shared_bus_client()
    consumer_stop = asyncio.Event()
    consumer_task = None
    registry_task = None
    reachability_task = None
    if bus_client is not None:
        # #287: ``from_url`` is lazy, so nothing above can tell a reachable
        # broker from a dead one — the consumers below start either way and a
        # partition then reads as an idle cluster, which is what a healthy idle
        # Watcher also looks like. One PING says which it is, at ERROR.
        #
        # Detached because the worst case is ``WORST_CASE_CONNECT_SECONDS`` of
        # waiting, and the dashboard has no business being unavailable because
        # the bus is. Held on ``app.state`` so a test can await it rather than
        # yield the loop and hope it ran.
        reachability_task = asyncio.create_task(
            probe_bus_reachable(bus_client, os.environ.get(BUS_REDIS_URL_ENV, ""))
        )
        application.state.bus_reachability_task = reachability_task
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
    if reachability_task is not None:
        # A single PING that is supposed to have finished long ago; cancelling a
        # straggler only matters when the broker is black-holed at shutdown.
        reachability_task.cancel()
    tasks = [
        t for t in (worker_task, consumer_task, registry_task, reachability_task) if t is not None
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
    await aclose_shared_bus_client()
    await proc_app.close_async()


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
