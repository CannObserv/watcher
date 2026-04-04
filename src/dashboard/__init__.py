"""Dashboard — server-rendered UI for watcher."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from src.core.config import BUILD_ID
from src.core.notifications.events import EVENT_TITLES

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["build_id"] = BUILD_ID
templates.env.globals["event_titles"] = EVENT_TITLES


def register_dashboard(app: FastAPI) -> None:
    """Mount static files and include dashboard routes."""
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from src.dashboard.routes import router

    app.include_router(router)
