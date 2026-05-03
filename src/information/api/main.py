"""Information service — FastAPI application entry point."""

from fastapi import APIRouter, Depends, FastAPI

from src.information.api.deps import require_api_key
from src.information.api.routes.health import router as health_router
from src.information.api.routes.info_items import router as info_items_router
from src.information.api.routes.info_specs import router as info_specs_router
from src.information.core.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)

app = FastAPI(title="information", version="0.1.0")

v1_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
v1_router.include_router(info_items_router)
v1_router.include_router(info_specs_router)

app.include_router(v1_router)
app.include_router(health_router)
