"""Information service — FastAPI application entry point."""

from fastapi import FastAPI

from src.information.api.routes.health import router as health_router
from src.information.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

app = FastAPI(title="information", version="0.1.0")
app.include_router(health_router)
