"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.routes.audit_log import router as audit_router
from src.api.routes.changes import router as changes_router
from src.api.routes.notification_configs import router as notification_configs_router
from src.api.routes.temporal_profiles import router as profiles_router
from src.api.routes.watches import router as watches_router
from src.core.logging import configure_logging
from src.dashboard import register_dashboard

configure_logging()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Start procrastinate worker alongside FastAPI."""
    # Local import: avoids importing procrastinate at module level, which would
    # add startup overhead for contexts that don't need the worker (e.g., pytest).
    from src.workers import get_app

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
register_dashboard(app)
