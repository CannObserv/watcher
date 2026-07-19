"""Dashboard route package — one module per resource area.

Submodules define bare ``APIRouter()`` instances; the shared dashboard
auth dependency and tag are applied here on the parent router.
"""

from fastapi import APIRouter, Depends

from src.dashboard.deps import get_dashboard_user
from src.dashboard.routes import (
    audit,
    domains,
    home,
    notifications,
    watched_item_templates,
    watched_items,
)

router = APIRouter(tags=["dashboard"], dependencies=[Depends(get_dashboard_user)])
router.include_router(home.router)
router.include_router(watched_items.router)
router.include_router(watched_item_templates.router)
router.include_router(domains.router)
router.include_router(audit.router)
router.include_router(notifications.router)
