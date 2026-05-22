"""Dashboard — server-rendered UI for watcher."""

from pathlib import Path
from urllib.parse import quote as _url_quote

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from src.core.config import BUILD_ID
from src.core.notifications.default_templates import TEMPLATE_VARIABLES
from src.core.notifications.events import EVENT_TITLES

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["build_id"] = BUILD_ID
templates.env.globals["event_titles"] = EVENT_TITLES
templates.env.globals["template_variables"] = TEMPLATE_VARIABLES
templates.env.filters["url_quote"] = lambda s: _url_quote(str(s), safe="")


def register_dashboard(app: FastAPI) -> None:
    """Mount static files and include dashboard routes."""
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from src.dashboard.routes import router
    from src.dashboard.settings import router as settings_router

    app.include_router(router)
    app.include_router(settings_router)
