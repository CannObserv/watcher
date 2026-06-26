"""Dashboard shared dependencies and request helpers."""

import hashlib
import os
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db_session
from src.core.models.app_user import AppUser

# Largest page size the pagination control (partials/pagination.html) offers;
# the cap for a user-supplied page_size so a crafted query can't load unbounded rows.
MAX_PAGE_SIZE = 100


def clamp_pagination(page: int, page_size: int, *, default_size: int = 25) -> tuple[int, int]:
    """Clamp user-supplied pagination params to safe bounds (#215 CR-6).

    ``page`` floors at 1 — a negative page yields a negative SQL ``OFFSET``, which
    Postgres rejects. ``page_size`` is bounded to ``[1, MAX_PAGE_SIZE]``: a sub-1
    value (``0`` / negative — a negative ``LIMIT`` errors in Postgres) falls back to
    ``default_size``, and anything above the cap is capped, while legitimate
    in-range sizes (e.g. ``2`` for a small list) pass through. Returns the
    ``(page, page_size)`` pair.
    """
    page = max(1, page)
    if page_size < 1:
        page_size = default_size
    elif page_size > MAX_PAGE_SIZE:
        page_size = MAX_PAGE_SIZE
    return page, page_size


def is_htmx(request: Request) -> bool:
    """True for an HTMX-driven request, excluding boosted full-page navigations.

    Canonical HTMX detector for the dashboard (#211). ``hx-boost`` sends
    ``HX-Request: true`` for what is semantically a full-page navigation; the
    ``HX-Boosted`` guard keeps those on the non-HTMX (full page / redirect) path
    rather than treating them as inline fragment swaps. Use this everywhere
    instead of a bare ``HX-Request`` header check.
    """
    return bool(request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"))


async def get_dashboard_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> AppUser:
    """Validate exe.dev auth headers; upsert AppUser row; return user.

    Raises 307 → /__exe.dev/login when headers are absent.
    The exe.dev proxy injects X-ExeDev-UserID and X-ExeDev-Email for all
    authenticated visitors; absence means the user is not logged in.
    """
    user_id = request.headers.get("X-ExeDev-UserID")
    email = request.headers.get("X-ExeDev-Email")
    if not user_id or not email:
        path = request.url.path
        query = request.url.query
        next_url = f"{path}?{query}" if query else path
        raise HTTPException(
            status_code=307,
            headers={"Location": f"/__exe.dev/login?redirect={quote(next_url)}"},
        )
    stmt = (
        insert(AppUser)
        .values(id=user_id, email=email)
        .on_conflict_do_update(
            index_elements=["id"],
            set_={"email": email, "updated_at": func.now()},
        )
        .returning(AppUser)
    )
    result = await session.execute(stmt)
    user = result.scalar_one()
    await session.commit()
    return user


def generate_api_key() -> tuple[str, str, str]:
    """Return (raw_key, key_hash, key_prefix).

    raw_key:    "co_" + 32 hex chars (128-bit random)
    key_hash:   SHA-256 hex of raw_key — stored in DB, never returned again
    key_prefix: first 8 chars of raw_key — stored for display identification
    """
    raw_key = "co_" + os.urandom(16).hex()
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:8]
    return raw_key, key_hash, key_prefix
