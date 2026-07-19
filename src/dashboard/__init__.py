"""Dashboard — server-rendered UI for watcher."""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.dashboard.routes import router
from src.dashboard.settings import router as settings_router
from src.dashboard.templating import STATIC_DIR


def register_dashboard(app: FastAPI) -> None:
    """Mount static files and include dashboard routes."""
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(router)
    app.include_router(settings_router)
