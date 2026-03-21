"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.routes.watches import router as watches_router
from src.core.logging import configure_logging

configure_logging()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Start procrastinate worker alongside FastAPI."""
    # Local import: avoids importing procrastinate at module level, which would
    # add startup overhead for contexts that don't need the worker (e.g., pytest).
    from src.workers import get_app

    proc_app = get_app()
    await proc_app.open_async()
    worker_task = asyncio.create_task(
        proc_app.run_worker_async(install_signal_handlers=False)
    )
    yield
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await proc_app.close_async()


app = FastAPI(title="watcher", version="0.1.0", lifespan=lifespan)
app.include_router(watches_router)
