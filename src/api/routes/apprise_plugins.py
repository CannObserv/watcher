"""Apprise plugin catalog API endpoints."""

from fastapi import APIRouter, HTTPException

from src.api.schemas.apprise_plugin import PluginDetail, PluginListItem, PluginVariant, TokenMeta
from src.core.notifications.apprise_builder import get_plugin_detail, list_plugins

router = APIRouter(prefix="/apprise", tags=["apprise"])


@router.get("/plugins", response_model=list[PluginListItem])
async def list_apprise_plugins():
    """List all available Apprise notification plugins."""
    return list_plugins()


@router.get("/plugins/{schema}", response_model=PluginDetail)
async def get_apprise_plugin(schema: str):
    """Return token definitions and variant info for an Apprise plugin."""
    detail = get_plugin_detail(schema)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Unknown Apprise plugin: {schema!r}")
    return PluginDetail(
        plugin_schema=detail["plugin_schema"],
        service_name=detail["service_name"],
        setup_url=detail.get("setup_url"),
        service_url=detail.get("service_url"),
        tokens={k: TokenMeta(**v) for k, v in detail["tokens"].items()},
        variants=[PluginVariant(**v) for v in detail["variants"]],
    )
