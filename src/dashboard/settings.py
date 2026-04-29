"""Dashboard settings routes — API key management."""

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.core.models.api_key import ApiKey
from src.core.models.app_user import AppUser
from src.dashboard import templates
from src.dashboard.deps import generate_api_key, get_dashboard_user

router = APIRouter(
    prefix="/settings",
    tags=["settings"],
    dependencies=[Depends(get_dashboard_user)],
)


def _is_htmx(request: Request) -> bool:
    return bool(request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"))


def _flash_trigger(level: str, body: str) -> dict[str, str]:
    return {"HX-Trigger": json.dumps({"showFlash": {"level": level, "body": body}})}


@router.get("")
async def settings_landing(
    request: Request,
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Render settings landing page with API key count."""
    result = await session.execute(
        select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user.id)
    )
    key_count = result.scalar_one()
    return templates.TemplateResponse(
        request,
        "pages/settings.html",
        {"active_page": "settings", "user": user, "api_key_count": key_count},
    )


@router.get("/api-keys")
async def api_keys_list(
    request: Request,
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    """List all API keys for the current user."""
    result = await session.execute(
        select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc())
    )
    keys = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "pages/settings_api_keys.html",
        {"active_page": "settings", "user": user, "keys": keys},
    )


@router.get("/api-keys/new-row")
async def api_key_new_row(request: Request):
    """Return inline add-row form for creating a new API key."""
    return templates.TemplateResponse(
        request,
        "partials/api_key_edit_row.html",
        {"key": None},
    )


@router.post("/api-keys")
async def api_key_create(
    request: Request,
    label: str = Form(...),
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Create a new API key; return modal with raw key (HTMX) or redirect."""
    label_val = label.strip()
    if not label_val:
        raise HTTPException(status_code=422, detail="label is required")
    raw_key, key_hash, key_prefix = generate_api_key()
    key = ApiKey(
        user_id=user.id,
        label=label_val,
        key_prefix=key_prefix,
        key_hash=key_hash,
    )
    session.add(key)
    await session.commit()
    if not _is_htmx(request):
        return RedirectResponse("/settings/api-keys", status_code=303)
    return templates.TemplateResponse(
        request,
        "partials/api_key_new_key_modal.html",
        {"raw_key": raw_key, "label": label_val},
    )


@router.get("/api-keys/{key_id}/edit-row")
async def api_key_edit_row_get(
    key_id: str,
    request: Request,
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Return inline edit-row form for an existing API key."""
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/api_key_edit_row.html",
        {"key": key},
    )


@router.post("/api-keys/{key_id}/edit-row")
async def api_key_edit_row_post(
    key_id: str,
    request: Request,
    label: str = Form(...),
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Save updated label; return read row (HTMX) or redirect."""
    label_val = label.strip()
    if not label_val:
        raise HTTPException(status_code=422, detail="label is required")
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404)
    key.label = label_val
    await session.commit()
    if not _is_htmx(request):
        return RedirectResponse("/settings/api-keys", status_code=303)
    return templates.TemplateResponse(
        request,
        "partials/api_key_row.html",
        {"key": key},
        headers=_flash_trigger("success", f"Key <strong>{escape(label_val)}</strong> renamed."),
    )


@router.get("/api-keys/{key_id}/read-row")
async def api_key_read_row(
    key_id: str,
    request: Request,
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Return read-only row partial for an API key."""
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "partials/api_key_row.html", {"key": key})


@router.delete("/api-keys/{key_id}")
async def api_key_delete(
    key_id: str,
    request: Request,
    user: AppUser = Depends(get_dashboard_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Delete an API key; return empty 200 with flash trigger."""
    result = await session.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id)
    )
    key = result.scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=404)
    label_val = key.label
    await session.delete(key)
    await session.commit()
    return HTMLResponse(
        content="",
        status_code=200,
        headers=_flash_trigger("info", f"Key <strong>{escape(label_val)}</strong> deleted."),
    )
