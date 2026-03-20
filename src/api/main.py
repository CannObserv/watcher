"""FastAPI application entry point."""

from fastapi import FastAPI

from src.api.routes.watches import router as watches_router
from src.core.logging import configure_logging

configure_logging()

app = FastAPI(title="watcher", version="0.1.0")
app.include_router(watches_router)
