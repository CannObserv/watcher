"""Information service — FastAPI application entry point."""

from fastapi import APIRouter, Depends, FastAPI

from src.information.api.deps import require_api_key
from src.information.api.routes.health import router as health_router
from src.information.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

app = FastAPI(title="information", version="0.1.0")

v1_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
# Routers attach in later tasks (info_items, info_specs).

app.include_router(v1_router)
app.include_router(health_router)
