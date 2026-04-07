"""Dashboard page routes — server-rendered HTML via Jinja2 + HTMX."""

import html as html_lib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.dependencies import get_db_session, get_probe_fn
from src.api.routes.helpers import parse_ulid
from src.api.routes.watches import delete_watch as api_delete_watch
from src.api.schemas.notification_config import (
    extract_channel_hint,
    validate_apprise_url,
    validate_event_list,
)
from src.core.crypto import decrypt_apprise_url, encrypt_apprise_url
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import Domain
from src.core.models.notification_config import NotificationConfig
from src.core.models.snapshot import Snapshot
from src.core.models.watch import ContentType, Watch
from src.core.notifications.apprise_builder import (
    assemble_url,
    get_plugin_detail,
    get_service_name,
    list_plugins,
)
from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.probe import ProbeResult
from src.core.screenshot import capture_screenshot
from src.core.storage import STORAGE_BASE_DIR, LocalStorage
from src.dashboard import templates
from src.dashboard.context import (
    compute_watch_health,
    generate_diff,
    get_audit_entries,
    get_change_detail,
    get_dashboard_stats,
    get_domain_watches,
    get_domains_total_count,
    get_domains_with_watch_counts,
    get_latest_snapshot,
    get_queue_health,
    get_recent_changes,
    get_watch_changes,
    get_watch_detail,
    get_watch_health_map,
    get_watch_list,
    get_watch_notifications,
    get_watch_profiles,
    get_watch_timeline,
    get_watch_timeline_count,
)

router = APIRouter(tags=["dashboard"])
logger = get_logger(__name__)


@router.get("/")
async def dashboard_home(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Dashboard home page with stats, recent changes, and system health."""
    stats = await get_dashboard_stats(session)
    changes = await get_recent_changes(session, limit=20)
    queue = await get_queue_health(session)
    domains = await get_domains_with_watch_counts(session)

    context = {
        "request": request,
        "active_page": "dashboard",
        "stats": stats,
        "changes": changes,
        "queue": queue,
        "domains": domains,
    }
    return templates.TemplateResponse("pages/dashboard.html", context)


async def _build_watch_health(session: AsyncSession, watches: list[Watch]) -> dict:
    """Return a mapping of watch.id → health string for each watch in the list.

    Health strings: ``"healthy"``, ``"warning"``, ``"error"``, or ``"unknown"``.
    Passes directly to templates as ``health_map``.
    """
    now = datetime.now(UTC)
    watch_ids = [w.id for w in watches]
    event_map = await get_watch_health_map(session, watch_ids)
    return {w.id: compute_watch_health(w, event_map.get(w.id), now) for w in watches}


@router.get("/watches")
async def watches_page(
    request: Request,
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Watch list page."""
    watches = await get_watch_list(session, is_active=is_active)
    health_map = await _build_watch_health(session, watches)
    context = {
        "request": request,
        "active_page": "watches",
        "watches": watches,
        "is_active": is_active,
        "health_map": health_map,
    }
    return templates.TemplateResponse("pages/watches.html", context)


@router.get("/watches/new")
async def watch_create_form(request: Request):
    """Watch creation form."""
    return templates.TemplateResponse(
        "pages/watch_form.html",
        {
            "request": request,
            "active_page": "watches",
            "watch": None,
            "flash": None,
            "content_types": list(ContentType),
        },
    )


@router.post("/watches/new")
async def watch_create_submit(
    request: Request,
    name: str = Form(""),
    url: str = Form(""),
    content_type: str = Form("html"),
    interval: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Handle watch creation form submission."""
    errors = []
    if not name.strip():
        errors.append("Name is required")
    if not url.strip():
        errors.append("URL is required")

    if errors:
        flash = {"type": "error", "message": ". ".join(errors)}
        return templates.TemplateResponse(
            "pages/watch_form.html",
            {
                "request": request,
                "active_page": "watches",
                "watch": None,
                "flash": flash,
                "content_types": list(ContentType),
            },
        )

    schedule_config = {}
    if interval.strip():
        schedule_config["interval"] = interval.strip()

    watch = Watch(
        name=name.strip(),
        url=url.strip(),
        content_type=content_type,
        schedule_config=schedule_config,
    )
    session.add(watch)
    audit(
        session,
        EventType.WATCH_CREATED,
        watch_id=watch.id,
        name=name,
        url=url,
        source="dashboard",
    )
    await session.commit()
    return RedirectResponse(url=f"/watches/{watch.id}", status_code=303)


@router.get("/watches/{watch_id}")
async def watch_detail_page(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Watch detail page with profiles, notifications, and change history."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)
    profiles = await get_watch_profiles(session, watch.id)
    notifications = await get_watch_notifications(session, watch.id)
    latest_snapshot = await get_latest_snapshot(session, watch.id)

    # Build field contexts for content-type-aware rendering
    applicable_fields = _watch_fields_for_content_type(watch.content_type)
    field_contexts = {
        name: _watch_field_context(request, watch, name, mode="view") for name in applicable_fields
    }

    # Build snapshot metadata whenever a snapshot exists; screenshot fields are conditional.
    storage = LocalStorage(STORAGE_BASE_DIR)
    snapshot_meta = None
    if latest_snapshot is not None:
        raw_bytes = None
        if latest_snapshot.storage_path and storage.exists(latest_snapshot.storage_path):
            raw_bytes = storage.size(latest_snapshot.storage_path)
        has_screenshot = latest_snapshot.screenshot_path is not None and storage.exists(
            latest_snapshot.screenshot_path
        )
        snapshot_meta = {
            "snapshot_id": str(latest_snapshot.id),
            "fetched_at": latest_snapshot.fetched_at,
            "chunk_count": latest_snapshot.chunk_count,
            "text_bytes": latest_snapshot.text_bytes,
            "raw_bytes": raw_bytes,
            "screenshot_browser": latest_snapshot.screenshot_browser if has_screenshot else None,
            "has_screenshot": has_screenshot,
        }

    # Check if the watch's domain is inactive (disables the status toggle)
    domain_inactive = False
    if watch.effective_domain:
        domain_result = await session.execute(
            select(Domain).where(Domain.name == watch.effective_domain)
        )
        domain = domain_result.scalar_one_or_none()
        if domain and not domain.is_active:
            domain_inactive = True

    # Initial timeline page (page 1, no category filter)
    timeline_page_size = 25
    timeline = await get_watch_timeline(session, watch_id, offset=0, limit=timeline_page_size)
    timeline_total = await get_watch_timeline_count(session, watch_id)

    context = {
        "request": request,
        "active_page": "watches",
        "watch": watch,
        "profiles": profiles,
        "notifications": notifications,
        "notification_urls": _decrypt_notification_urls(notifications),
        "field_contexts": field_contexts,
        "snapshot_meta": snapshot_meta,
        "domain_inactive": domain_inactive,
        "timeline": timeline,
        "timeline_total": timeline_total,
        "timeline_page": 1,
        "timeline_page_size": timeline_page_size,
        "timeline_category": None,
        # Pagination vars for partials/pagination.html (used inside the timeline include)
        "page": 1,
        "page_size": timeline_page_size,
        "total_count": timeline_total,
        "base_url": f"/partials/watch-timeline/{watch_id}",
        "extra_params": {},
    }
    return templates.TemplateResponse("pages/watch_detail.html", context)


@router.get("/watches/{watch_id}/screenshot")
async def watch_screenshot(
    watch_id: str,
    snapshot_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Serve the PNG screenshot for the latest (or specified) snapshot of a watch."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    if snapshot_id:
        try:
            sid = ULID.from_str(snapshot_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        result = await session.execute(
            select(Snapshot).where(Snapshot.id == sid, Snapshot.watch_id == watch.id)
        )
        snapshot = result.scalar_one_or_none()
    else:
        snapshot = await get_latest_snapshot(session, watch.id)

    storage = LocalStorage(STORAGE_BASE_DIR)
    if (
        snapshot is None
        or snapshot.screenshot_path is None
        or not storage.exists(snapshot.screenshot_path)
    ):
        raise HTTPException(status_code=404, detail="Screenshot not available")

    png_bytes = storage.load(snapshot.screenshot_path)
    return Response(content=png_bytes, media_type="image/png")


@router.post("/watches/{watch_id}/screenshot")
async def watch_screenshot_recapture(
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Trigger an on-demand screenshot re-capture for the latest snapshot of a watch.

    Returns JSON:
    - ``{"status": "ok", "screenshot_path": "..."}`` on success.
    - ``{"status": "unavailable", "detail": "..."}`` if Playwright not installed.
    - 404 if the watch or its latest snapshot does not exist.
    """
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    snapshot = await get_latest_snapshot(session, watch.id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No snapshot available for this watch")

    result = await capture_screenshot(watch.url)
    if result is None:
        return JSONResponse(
            status_code=200,
            content={"status": "unavailable", "detail": "Playwright not installed"},
        )

    storage = LocalStorage(STORAGE_BASE_DIR)
    screenshot_path = storage.snapshot_path(str(watch.id), str(snapshot.id), "png")
    storage.save(screenshot_path, result.png_bytes)

    snapshot.screenshot_path = screenshot_path
    snapshot.screenshot_browser = result.browser
    await session.flush()

    return JSONResponse(
        status_code=200,
        content={"status": "ok", "screenshot_path": screenshot_path},
    )


@router.get("/watches/{watch_id}/snapshots/{snapshot_id}/content")
async def watch_snapshot_content(
    watch_id: str,
    snapshot_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Serve the stored snapshot text/content as an escaped HTML page.

    Prefers ``text_path`` (extracted text); falls back to ``storage_path`` (raw content).
    Returns 404 if the watch, snapshot, or storage file is not found.
    """
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    try:
        sid = ULID.from_str(snapshot_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    result = await session.execute(
        select(Snapshot).where(Snapshot.id == sid, Snapshot.watch_id == watch.id)
    )
    snapshot = result.scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    storage = LocalStorage(STORAGE_BASE_DIR)

    # Prefer extracted text; fall back to raw storage
    content_path: str | None = None
    if snapshot.text_path and storage.exists(snapshot.text_path):
        content_path = snapshot.text_path
    elif snapshot.storage_path and storage.exists(snapshot.storage_path):
        content_path = snapshot.storage_path

    if content_path is None:
        raise HTTPException(status_code=404, detail="Snapshot content not available")

    raw_bytes = storage.load(content_path)
    text = raw_bytes.decode("utf-8", errors="replace")
    escaped = html_lib.escape(text)

    html_page = (
        "<!doctype html><html><head>"
        "<meta charset='utf-8'>"
        f"<title>Snapshot content — {html_lib.escape(watch.name)}</title>"
        "<style>body{font-family:monospace;white-space:pre-wrap;padding:1rem;}"
        "pre{margin:0;}</style>"
        "</head><body>"
        f"<pre>{escaped}</pre>"
        "</body></html>"
    )
    return HTMLResponse(content=html_page)


@router.delete("/watches/{watch_id}")
async def watch_delete(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete an archived watch — requires is_archived=True.

    Delegates to the API layer for business logic and translates the outcome
    into an HTMX-compatible response.
    """
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)
    if not watch.is_archived:
        msg = '<p class="text-red-600 text-sm mt-2">Archive the watch before deleting it.</p>'
        return HTMLResponse(status_code=409, content=msg)
    try:
        await api_delete_watch(watch_id=watch_id, session=session)
    except HTTPException as exc:
        if exc.status_code == 409:
            msg = '<p class="text-red-600 text-sm mt-2">Archive the watch before deleting it.</p>'
            return HTMLResponse(status_code=409, content=msg)
        raise
    return HTMLResponse(status_code=200, content="", headers={"HX-Redirect": "/watches"})


# --- Watch inline field editing ---


def _split_lines(text: str) -> list[str]:
    """Split text into non-empty lines."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _parse_json_dict(text: str) -> dict:
    """Parse JSON text as a dict, or return empty dict for empty input."""
    text = text.strip()
    if not text:
        return {}
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object")
    return result


WATCH_FIELD_META: dict[str, dict] = {
    # -- Details section (column fields) --
    "name": {
        "label": "Name",
        "hint": None,
        "type": "text",
        "source": "column",
        "cast": lambda v: v.strip(),
        "format": lambda w: w.name,
        "content_types": None,
    },
    "url": {
        "label": "URL",
        "hint": None,
        "type": "url",
        "source": "column",
        "cast": lambda v: v.strip(),
        "format": lambda w: w.url,
        "content_types": None,
    },
    # -- Schedule section --
    "interval": {
        "label": "Check Interval",
        "hint": "Format: 30s, 15m, 6h, 1d",
        "type": "text",
        "source": "schedule_config",
        "cast": lambda v: v.strip(),
        "format": lambda w: (w.schedule_config or {}).get("interval", "1d"),
        "content_types": None,
    },
    # -- Fetch config: shared --
    "timeout": {
        "label": "Timeout",
        "hint": "Request timeout in seconds",
        "type": "number",
        "step": "1",
        "min": "1",
        "unit": "seconds",
        "source": "fetch_config",
        "cast": float,
        "format": lambda w: str((w.fetch_config or {}).get("timeout", 30)),
        "content_types": None,
    },
    "headers": {
        "label": "Headers",
        "hint": 'JSON object, e.g. {"Authorization": "Bearer ..."}',
        "type": "textarea",
        "source": "fetch_config",
        "cast": _parse_json_dict,
        "format": lambda w: (
            json.dumps((w.fetch_config or {}).get("headers", {}), indent=2)
            if (w.fetch_config or {}).get("headers")
            else ""
        ),
        "content_types": None,
    },
    "ignore_patterns": {
        "label": "Ignore Patterns",
        "hint": "One regex per line (fullmatch against chunk text)",
        "type": "textarea",
        "source": "fetch_config",
        "cast": _split_lines,
        "format": lambda w: "\n".join((w.fetch_config or {}).get("ignore_patterns", [])),
        "content_types": None,
    },
    # -- Fetch config: HTML-specific --
    "selectors": {
        "label": "CSS Selectors",
        "hint": "One CSS selector per line (empty = whole body)",
        "type": "textarea",
        "source": "fetch_config",
        "cast": _split_lines,
        "format": lambda w: "\n".join((w.fetch_config or {}).get("selectors", [])),
        "content_types": ["html"],
    },
    "exclude_selectors": {
        "label": "Exclude Selectors",
        "hint": "CSS selectors to remove from included content",
        "type": "textarea",
        "source": "fetch_config",
        "cast": _split_lines,
        "format": lambda w: "\n".join((w.fetch_config or {}).get("exclude_selectors", [])),
        "content_types": ["html"],
    },
    "dynamic_id_patterns": {
        "label": "Dynamic ID Patterns",
        "hint": "HTML attribute names to strip (e.g. data-block-id)",
        "type": "textarea",
        "source": "fetch_config",
        "cast": _split_lines,
        "format": lambda w: "\n".join((w.fetch_config or {}).get("dynamic_id_patterns", [])),
        "content_types": ["html"],
    },
    "strip_boilerplate": {
        "label": "Strip Boilerplate",
        "hint": "Remove nav, footer, header, script, style elements",
        "type": "toggle",
        "source": "fetch_config",
        "cast": lambda v: v == "true",
        "format": lambda w: (w.fetch_config or {}).get("strip_boilerplate", True),
        "content_types": ["html"],
    },
    # -- Fetch config: PDF-specific --
    "skip_empty_pages": {
        "label": "Skip Empty Pages",
        "hint": "Omit pages with no text content",
        "type": "toggle",
        "source": "fetch_config",
        "cast": lambda v: v == "true",
        "format": lambda w: (w.fetch_config or {}).get("skip_empty_pages", False),
        "content_types": ["pdf"],
    },
    # -- Fetch config: File-specific --
    "file_format": {
        "label": "File Format",
        "hint": None,
        "type": "select",
        "options": [("csv", "CSV"), ("xlsx", "Excel")],
        "source": "fetch_config",
        "cast": lambda v: v.strip(),
        "format": lambda w: (w.fetch_config or {}).get("file_format", "csv"),
        "content_types": ["file"],
    },
    "chunk_row_size": {
        "label": "Chunk Row Size",
        "hint": "Number of rows per chunk",
        "type": "number",
        "step": "1",
        "min": "1",
        "unit": "rows",
        "source": "fetch_config",
        "cast": int,
        "format": lambda w: str((w.fetch_config or {}).get("chunk_row_size", 100)),
        "content_types": ["file"],
    },
    "sort_columns": {
        "label": "Sort Columns",
        "hint": "Column names to sort by before chunking (one per line)",
        "type": "textarea",
        "source": "fetch_config",
        "cast": _split_lines,
        "format": lambda w: "\n".join((w.fetch_config or {}).get("sort_columns", [])),
        "content_types": ["file"],
    },
    "sheet_name": {
        "label": "Sheet Name",
        "hint": "Excel sheet name (empty = active sheet)",
        "type": "text",
        "source": "fetch_config",
        "cast": lambda v: v.strip(),
        "format": lambda w: (w.fetch_config or {}).get("sheet_name", ""),
        "content_types": ["file"],
    },
}
EDITABLE_WATCH_FIELDS = set(WATCH_FIELD_META.keys())


def _watch_fields_for_content_type(content_type: str) -> list[str]:
    """Return field names applicable to a given content type."""
    ct = str(content_type).lower()
    return [
        name
        for name, meta in WATCH_FIELD_META.items()
        if meta["content_types"] is None or ct in meta["content_types"]
    ]


def _watch_field_context(
    request: Request, watch: Watch, field_name: str, mode: str = "view"
) -> dict:
    """Build template context for a single watch field partial."""
    meta = WATCH_FIELD_META[field_name]
    ctx = {
        "request": request,
        "watch": watch,
        "field_name": field_name,
        "field_label": meta["label"],
        "field_hint": meta.get("hint"),
        "field_value": meta["format"](watch),
        "field_type": meta["type"],
        "field_step": meta.get("step"),
        "field_min": meta.get("min"),
        "field_unit": meta.get("unit"),
        "field_options": meta.get("options"),
        "field_mode": mode,
    }
    return ctx


def _apply_watch_field_update(watch: Watch, field_name: str, raw_value: str) -> None:
    """Apply a single field update to a Watch object."""
    meta = WATCH_FIELD_META[field_name]
    cast_fn = meta["cast"]
    typed_value = cast_fn(raw_value)

    source = meta["source"]
    if source == "column":
        setattr(watch, field_name, typed_value)
    elif source == "schedule_config":
        config = dict(watch.schedule_config or {})
        if typed_value:
            config[field_name] = typed_value
        else:
            config.pop(field_name, None)
        watch.schedule_config = config
    elif source == "fetch_config":
        config = dict(watch.fetch_config or {})
        # Only store non-default values; remove key if empty/default
        if typed_value in (None, "", [], {}):
            config.pop(field_name, None)
        else:
            config[field_name] = typed_value
        watch.fetch_config = config


@router.get("/watches/{watch_id}/field/{field_name}")
async def watch_field_partial(
    request: Request,
    watch_id: str,
    field_name: str,
    mode: Literal["view", "edit"] = "view",
    session: AsyncSession = Depends(get_db_session),
):
    """Serve a single watch field partial in view or edit mode."""
    if field_name not in EDITABLE_WATCH_FIELDS:
        raise HTTPException(status_code=400, detail=f"Field '{field_name}' is not editable")

    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)

    ctx = _watch_field_context(request, watch, field_name, mode=mode)
    return templates.TemplateResponse("partials/watch_field.html", ctx)


@router.post("/watches/{watch_id}/field/{field_name}")
async def watch_field_update(
    request: Request,
    watch_id: str,
    field_name: str,
    value: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Update a single watch field (inline edit from detail view)."""
    if field_name not in EDITABLE_WATCH_FIELDS:
        raise HTTPException(status_code=400, detail=f"Field '{field_name}' is not editable")

    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    if field_name in ("name", "url") and not value.strip():
        raise HTTPException(status_code=400, detail=f"{field_name.title()} cannot be empty")

    try:
        _apply_watch_field_update(watch, field_name, value)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail=f"Invalid value for {field_name}")

    audit(
        session,
        EventType.WATCH_UPDATED,
        watch_id=watch.id,
        updated_fields=[field_name],
        source="dashboard",
    )
    await session.commit()
    await session.refresh(watch)

    if request.headers.get("HX-Request") == "true":
        ctx = _watch_field_context(request, watch, field_name, mode="view")
        return templates.TemplateResponse("partials/watch_field.html", ctx)
    return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)


@router.post("/watches/{watch_id}/toggle-active")
async def watch_toggle_active(
    request: Request,
    watch_id: str,
    active: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Toggle watch active status via HTMX checkbox."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    if watch.is_archived:
        raise HTTPException(status_code=409, detail="Cannot toggle archived watch")

    new_active = active == "true"

    # Block activation while the watch's domain is inactive (kill-switch)
    domain_inactive = False
    if watch.effective_domain:
        domain_result = await session.execute(
            select(Domain).where(Domain.name == watch.effective_domain)
        )
        domain = domain_result.scalar_one_or_none()
        if domain and not domain.is_active:
            domain_inactive = True
            if new_active:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot activate watch while its domain is inactive",
                )

    watch.is_active = new_active

    event = EventType.WATCH_UPDATED if new_active else EventType.WATCH_DEACTIVATED
    audit(session, event, watch_id=watch.id, name=watch.name, source="dashboard")
    await session.commit()
    await session.refresh(watch)

    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            "partials/watch_status_toggle.html",
            {"request": request, "watch": watch, "domain_inactive": domain_inactive},
        )
    return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)


@router.post("/watches/{watch_id}/archive")
async def watch_archive(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Archive a watch — sets is_archived=True and is_active=False."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    watch.is_archived = True
    watch.is_active = False
    audit(session, EventType.WATCH_ARCHIVED, watch_id=watch.id, name=watch.name, source="dashboard")
    await session.commit()

    return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)


@router.post("/watches/{watch_id}/restore")
async def watch_restore(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Restore an archived watch — clears is_archived, stays inactive."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    watch.is_archived = False
    # Watch stays inactive after restore — user re-activates via toggle
    audit(session, EventType.WATCH_RESTORED, watch_id=watch.id, name=watch.name, source="dashboard")
    await session.commit()

    return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)


@router.get("/domains")
async def domains_page(
    request: Request,
    q: str | None = None,
    status: str | None = "active",
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_db_session),
):
    """Domains list page with search, filter, and pagination."""
    domains = await get_domains_with_watch_counts(
        session,
        search=q,
        status=status,
        page=page,
        page_size=page_size,
    )
    total_count = await get_domains_total_count(session, search=q, status=status)
    context = {
        "request": request,
        "active_page": "domains",
        "domains": domains,
        "search": q,
        "status": status,
        "page": page,
        "page_size": page_size,
        "total_count": total_count,
        "base_url": "/partials/domains-table",
        "extra_params": {k: v for k, v in {"q": q, "status": status}.items() if v},
    }
    return templates.TemplateResponse("pages/domains.html", context)


@router.get("/partials/domains-table")
async def partial_domains_table(
    request: Request,
    q: str | None = None,
    status: str | None = "active",
    page: int = 1,
    page_size: int = 25,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: domains table with search, filter, and pagination."""
    domains = await get_domains_with_watch_counts(
        session,
        search=q,
        status=status,
        page=page,
        page_size=page_size,
    )
    total_count = await get_domains_total_count(session, search=q, status=status)
    return templates.TemplateResponse(
        "partials/domains_table.html",
        {
            "request": request,
            "domains": domains,
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "base_url": "/partials/domains-table",
            "extra_params": {k: v for k, v in {"q": q, "status": status}.items() if v},
        },
    )


@router.get("/domains/new")
async def domain_create_form(request: Request):
    """Domain creation form."""
    return templates.TemplateResponse(
        "pages/domain_form.html",
        {"request": request, "active_page": "domains", "flash": None, "url": ""},
    )


@router.post("/domains")
async def domain_create_submit(
    request: Request,
    url: str = Form(""),
    probe_fn: Callable[[str], Awaitable[ProbeResult]] = Depends(get_probe_fn),
    session: AsyncSession = Depends(get_db_session),
):
    """Create domain by probing a URL to extract the effective domain."""
    if not url.strip():
        flash = {"type": "error", "message": "URL is required"}
        return templates.TemplateResponse(
            "pages/domain_form.html",
            {"request": request, "active_page": "domains", "flash": flash, "url": url},
        )

    try:
        result = await probe_fn(url.strip())
    except Exception:
        flash = {
            "type": "error",
            "message": "Could not reach URL. Check the address and try again.",
        }
        return templates.TemplateResponse(
            "pages/domain_form.html",
            {"request": request, "active_page": "domains", "flash": flash, "url": url},
        )

    domain_name = result.effective_domain
    if not domain_name:
        flash = {"type": "error", "message": "Could not extract domain from URL."}
        return templates.TemplateResponse(
            "pages/domain_form.html",
            {"request": request, "active_page": "domains", "flash": flash, "url": url},
        )

    # Check if domain already exists
    existing = await session.execute(select(Domain).where(Domain.name == domain_name))
    if existing.scalar_one_or_none():
        return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)

    domain = Domain(name=domain_name)
    session.add(domain)
    audit(session, EventType.DOMAIN_CREATED, domain_name=domain_name, source="dashboard")
    await session.commit()
    return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)


@router.post("/domains/{name}/archive")
async def domain_archive(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Archive a domain from the dashboard."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    if domain.archived_at is None:
        domain.archived_at = datetime.now(UTC)
        audit(session, EventType.DOMAIN_ARCHIVED, domain_name=name, source="dashboard")
        await session.commit()

    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.post("/domains/{name}/restore")
async def domain_restore(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Restore an archived domain from the dashboard."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    domain.archived_at = None
    audit(session, EventType.DOMAIN_RESTORED, domain_name=name, source="dashboard")
    await session.commit()

    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.post("/domains/{name}/toggle-active")
async def domain_toggle_active(
    request: Request,
    name: str,
    active: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Toggle domain active status.

    Deactivating suspends all active watches; reactivating restores them.
    """
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    if domain.archived_at is not None:
        raise HTTPException(status_code=409, detail="Cannot toggle archived domain")

    new_active = active == "true"
    domain.is_active = new_active

    if not new_active:
        # Suspend all currently-active, non-archived watches in this domain
        watches_result = await session.execute(
            select(Watch).where(
                Watch.effective_domain == name,
                Watch.is_active == True,  # noqa: E712
                Watch.is_archived == False,  # noqa: E712
            )
        )
        for watch in watches_result.scalars().all():
            watch.is_active = False
            watch.domain_suspended = True
        audit(session, EventType.DOMAIN_DEACTIVATED, domain_name=name, source="dashboard")
    else:
        # Restore only watches that were suspended by a previous domain deactivation
        watches_result = await session.execute(
            select(Watch).where(
                Watch.effective_domain == name,
                Watch.domain_suspended == True,  # noqa: E712
                Watch.is_archived == False,  # noqa: E712
            )
        )
        for watch in watches_result.scalars().all():
            watch.is_active = True
            watch.domain_suspended = False
        audit(session, EventType.DOMAIN_ACTIVATED, domain_name=name, source="dashboard")

    await session.commit()
    await session.refresh(domain)

    if request.headers.get("HX-Request") == "true":
        watches = await get_domain_watches(session, name)
        return templates.TemplateResponse(
            "partials/domain_toggle_oob.html",
            {"request": request, "domain": domain, "watches": watches},
        )
    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.post("/domains/{name}/delete")
async def domain_delete(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Hard-delete an archived domain with no watches."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    if domain.archived_at is None:
        raise HTTPException(status_code=409, detail="Archive the domain before deleting it")

    watch_result = await session.execute(
        select(Watch).where(Watch.effective_domain == name).limit(1)
    )
    if watch_result.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete: watches still reference domain '{name}'",
        )

    audit(session, EventType.DOMAIN_DELETED, domain_name=name, source="dashboard")
    await session.delete(domain)
    await session.commit()

    return RedirectResponse(url="/domains", status_code=303)


DOMAIN_FIELD_META: dict[str, dict] = {
    "min_interval": {
        "label": "Min Interval",
        "hint": "Minimum seconds between requests to this domain",
        "type": "number",
        "step": "0.1",
        "min": "0.1",
        "unit": "seconds",
        "format": lambda v: f"{v:.1f}",
        "cast": float,
    },
    "max_concurrency": {
        "label": "Max Concurrency",
        "hint": "Maximum simultaneous requests allowed",
        "type": "number",
        "step": None,
        "min": "1",
        "unit": None,
        "format": lambda v: str(v),
        "cast": int,
    },
    "decay_window": {
        "label": "Decay Window",
        "hint": "Seconds before backoff interval decays toward minimum",
        "type": "number",
        "step": "1",
        "min": "1",
        "unit": "seconds",
        "format": lambda v: f"{v:.0f}",
        "cast": float,
    },
    "notes": {
        "label": "Notes",
        "hint": None,
        "type": "textarea",
        "step": None,
        "min": None,
        "unit": None,
        "format": lambda v: v or "",
        "cast": str,
    },
}
EDITABLE_DOMAIN_FIELDS = set(DOMAIN_FIELD_META.keys())


def _field_context(request: Request, domain: Domain, field_name: str, mode: str = "view") -> dict:
    """Build template context for a single domain field partial."""
    meta = DOMAIN_FIELD_META[field_name]
    return {
        "request": request,
        "domain": domain,
        "field_name": field_name,
        "field_label": meta["label"],
        "field_hint": meta["hint"],
        "field_value": meta["format"](getattr(domain, field_name)),
        "field_type": meta["type"],
        "field_step": meta["step"],
        "field_min": meta["min"],
        "field_unit": meta["unit"],
        "field_options": meta.get("options"),
        "field_mode": mode,
    }


@router.get("/domains/{name}/field/{field_name}")
async def domain_field_partial(
    request: Request,
    name: str,
    field_name: str,
    mode: Literal["view", "edit"] = "view",
    session: AsyncSession = Depends(get_db_session),
):
    """Serve a single domain field partial in view or edit mode."""
    if field_name not in EDITABLE_DOMAIN_FIELDS:
        raise HTTPException(status_code=400, detail=f"Field '{field_name}' is not editable")

    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=f"/domains/{name}", status_code=303)

    ctx = _field_context(request, domain, field_name, mode=mode)
    return templates.TemplateResponse("partials/domain_field.html", ctx)


@router.post("/domains/{name}")
async def domain_inline_update(
    request: Request,
    name: str,
    field: str = Form(""),
    value: str = Form(""),
    session: AsyncSession = Depends(get_db_session),
):
    """Update a single domain field (inline edit from detail view)."""
    if field not in EDITABLE_DOMAIN_FIELDS:
        raise HTTPException(status_code=400, detail=f"Field '{field}' is not editable")

    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    cast_fn = DOMAIN_FIELD_META[field]["cast"]
    try:
        typed_value: str | int | float = cast_fn(value) if field != "notes" else value
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid value for {field}")

    setattr(domain, field, typed_value)
    audit(session, EventType.DOMAIN_UPDATED, domain_name=name, field=field, source="dashboard")
    await session.commit()
    await session.refresh(domain)

    if request.headers.get("HX-Request") == "true":
        ctx = _field_context(request, domain, field, mode="view")
        return templates.TemplateResponse("partials/domain_field.html", ctx)
    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.get("/domains/{name}")
async def domain_detail_page(
    request: Request,
    name: str,
    watch_q: str | None = None,
    watch_status: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Domain detail page with config, watches, and danger zone."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    is_active = None
    if watch_status == "active":
        is_active = True
    elif watch_status == "inactive":
        is_active = False

    watches = await get_domain_watches(session, name, search=watch_q, is_active=is_active)

    field_contexts = {
        fname: _field_context(request, domain, fname, mode="view") for fname in DOMAIN_FIELD_META
    }

    context = {
        "request": request,
        "active_page": "domains",
        "domain": domain,
        "watches": watches,
        "watch_q": watch_q,
        "watch_status": watch_status,
        "flash": None,
        "field_contexts": field_contexts,
    }
    return templates.TemplateResponse("pages/domain_detail.html", context)


@router.get("/partials/stats-cards")
async def partial_stats_cards(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: stats cards only."""
    stats = await get_dashboard_stats(session)
    return templates.TemplateResponse(
        "partials/stats_cards.html", {"request": request, "stats": stats}
    )


@router.get("/partials/recent-changes")
async def partial_recent_changes(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: recent changes table."""
    changes = await get_recent_changes(session, limit=20)
    return templates.TemplateResponse(
        "partials/recent_changes.html", {"request": request, "changes": changes}
    )


@router.get("/partials/system-health")
async def partial_system_health(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: queue health and rate limiter."""
    queue = await get_queue_health(session)
    domains = await get_domains_with_watch_counts(session)
    return templates.TemplateResponse(
        "partials/system_health.html",
        {"request": request, "queue": queue, "domains": domains},
    )


@router.get("/partials/watch-table")
async def partial_watch_table(
    request: Request,
    is_active: bool | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: watch table with optional filter."""
    watches = await get_watch_list(session, is_active=is_active)
    health_map = await _build_watch_health(session, watches)
    return templates.TemplateResponse(
        "partials/watch_table.html",
        {"request": request, "watches": watches, "health_map": health_map},
    )


@router.get("/partials/watch-changes/{watch_id}")
async def partial_watch_changes(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: change history for a watch (legacy endpoint)."""
    changes = await get_watch_changes(session, watch_id)
    return templates.TemplateResponse(
        "partials/watch_changes.html", {"request": request, "changes": changes}
    )


@router.get("/partials/watch-timeline/{watch_id}")
async def partial_watch_timeline(
    request: Request,
    watch_id: str,
    page: int = 1,
    page_size: int = 25,
    category: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: unified lifecycle event timeline for a watch."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    offset = (page - 1) * page_size
    timeline = await get_watch_timeline(session, watch_id, offset=offset, limit=page_size)
    timeline_total = await get_watch_timeline_count(session, watch_id)

    # Client-side category filter (applied after DB fetch)
    if category and category != "all":
        timeline = [e for e in timeline if e["category"] == category]

    return templates.TemplateResponse(
        "partials/watch_timeline.html",
        {
            "request": request,
            "watch": watch,
            "timeline": timeline,
            "timeline_total": timeline_total,
            "timeline_page": page,
            "timeline_page_size": page_size,
            "timeline_category": category,
            "page": page,
            "page_size": page_size,
            "total_count": timeline_total,
            "base_url": f"/partials/watch-timeline/{watch_id}",
            "extra_params": {k: v for k, v in {"category": category}.items() if v and v != "all"},
        },
    )


def _decrypt_notification_urls(notifications: list) -> dict[str, str]:
    """Decrypt Apprise URLs for a list of NotificationConfig objects.

    Returns a mapping from config ID (str) to plaintext URL.
    Silently stores an error placeholder if decryption fails.
    """
    result = {}
    for nc in notifications:
        try:
            result[str(nc.id)] = decrypt_apprise_url(nc.apprise_url)
        except (InvalidToken, ValueError):
            result[str(nc.id)] = "(decryption error)"
    return result


@router.get("/partials/watch-notifications/{watch_id}")
async def partial_watch_notifications(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: notification config list for a watch."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    notifications = await get_watch_notifications(session, watch.id)
    return templates.TemplateResponse(
        "partials/watch_notifications.html",
        {
            "request": request,
            "watch": watch,
            "notifications": notifications,
            "notification_urls": _decrypt_notification_urls(notifications),
            "apprise_plugins": list_plugins(),
        },
    )


@router.get("/partials/apprise-plugin-form")
async def partial_apprise_plugin_form(
    request: Request,
    schema: str | None = None,
    variant: int = 0,
    raw: bool = False,
):
    """HTMX partial: token form for a selected Apprise plugin, or raw URL input."""
    if raw or schema is None:
        return templates.TemplateResponse(
            "partials/apprise_raw_url_form.html", {"request": request}
        )
    detail = get_plugin_detail(schema)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Unknown Apprise plugin: {schema!r}")

    # Filter tokens to match selected variant: show only that variant's
    # required tokens + all globally optional tokens.  Hide required tokens
    # belonging to other variants.
    variants = detail["variants"]
    if variants and 0 <= variant < len(variants):
        selected_required = set(variants[variant]["required_token_names"])
        other_required: set[str] = set()
        for i, v in enumerate(variants):
            if i != variant:
                other_required |= set(v["required_token_names"])
        # Exclude tokens that are required only in other variants
        exclude = other_required - selected_required
        detail["tokens"] = {k: v for k, v in detail["tokens"].items() if k not in exclude}

    return templates.TemplateResponse(
        "partials/apprise_plugin_form.html",
        {"request": request, "plugin": detail, "variant": variant},
    )


@router.post("/watches/{watch_id}/notifications/new")
async def watch_notification_create(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a notification config from dashboard form. Returns refreshed partial."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    form = await request.form()
    events = form.getlist("events")
    schema_val = form.get("plugin_schema") or ""

    # Determine the Apprise URL: token path or raw URL path
    if schema_val:
        # Token-based submission: collect tok_{name} fields
        tokens = {
            key[4:]: str(value)
            for key, value in form.items()
            if key.startswith("tok_") and str(value).strip()
        }
        try:
            variant_raw = form.get("variant")
            variant_index = int(variant_raw) if variant_raw is not None else None
            apprise_url = assemble_url(schema_val, tokens, variant_index=variant_index)
        except ValueError as exc:
            notifications = await get_watch_notifications(session, watch.id)
            return templates.TemplateResponse(
                "partials/watch_notifications.html",
                {
                    "request": request,
                    "watch": watch,
                    "notifications": notifications,
                    "notification_urls": _decrypt_notification_urls(notifications),
                    "error": str(exc),
                    "apprise_plugins": list_plugins(),
                },
            )
    else:
        # Raw URL submission (legacy path)
        apprise_url = str(form.get("apprise_url") or "")
        try:
            validate_apprise_url(apprise_url)
        except ValueError as exc:
            notifications = await get_watch_notifications(session, watch.id)
            return templates.TemplateResponse(
                "partials/watch_notifications.html",
                {
                    "request": request,
                    "watch": watch,
                    "notifications": notifications,
                    "notification_urls": _decrypt_notification_urls(notifications),
                    "error": str(exc),
                    "apprise_plugins": list_plugins(),
                },
            )

    try:
        validate_event_list(events)
    except ValueError as exc:
        notifications = await get_watch_notifications(session, watch.id)
        return templates.TemplateResponse(
            "partials/watch_notifications.html",
            {
                "request": request,
                "watch": watch,
                "notifications": notifications,
                "notification_urls": _decrypt_notification_urls(notifications),
                "error": str(exc),
                "apprise_plugins": list_plugins(),
            },
        )

    hint = get_service_name(schema_val) if schema_val else extract_channel_hint(apprise_url)
    config = NotificationConfig(
        watch_id=watch.id,
        apprise_url=encrypt_apprise_url(apprise_url),
        channel_hint=hint,
        events=events,
    )
    session.add(config)
    audit(
        session,
        EventType.NOTIFICATION_CONFIG_CREATED,
        watch_id=watch.id,
        config_id=str(config.id),
        channel_hint=config.channel_hint,
    )
    await session.commit()
    notifications = await get_watch_notifications(session, watch.id)
    return templates.TemplateResponse(
        "partials/watch_notifications.html",
        {
            "request": request,
            "watch": watch,
            "notifications": notifications,
            "notification_urls": _decrypt_notification_urls(notifications),
            "apprise_plugins": list_plugins(),
        },
    )


@router.post("/watches/{watch_id}/notifications/{config_id}/toggle")
async def watch_notification_toggle(
    request: Request,
    watch_id: str,
    config_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Toggle is_active on a notification config. Returns refreshed partial."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    nc = await session.get(NotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    nc.is_active = not nc.is_active
    audit(session, EventType.NOTIFICATION_CONFIG_UPDATED, watch_id=watch.id, config_id=str(nc.id))
    await session.commit()
    notifications = await get_watch_notifications(session, watch.id)
    return templates.TemplateResponse(
        "partials/watch_notifications.html",
        {
            "request": request,
            "watch": watch,
            "notifications": notifications,
            "notification_urls": _decrypt_notification_urls(notifications),
            "apprise_plugins": list_plugins(),
        },
    )


@router.get("/watches/{watch_id}/notifications/{config_id}/edit-form")
async def watch_notification_edit_form(
    request: Request,
    watch_id: str,
    config_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: edit form for an existing notification config."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    nc = await session.get(NotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    decrypted_url = decrypt_apprise_url(nc.apprise_url)
    return templates.TemplateResponse(
        "partials/notification_edit_form.html",
        {
            "request": request,
            "watch": watch,
            "nc": nc,
            "decrypted_url": decrypted_url,
        },
    )


@router.post("/watches/{watch_id}/notifications/{config_id}/edit")
async def watch_notification_edit(
    request: Request,
    watch_id: str,
    config_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Update apprise_url and/or events for a notification config. Returns refreshed partial."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    nc = await session.get(NotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")

    form = await request.form()
    apprise_url = str(form.get("apprise_url") or "").strip()
    events = form.getlist("events")

    try:
        validate_apprise_url(apprise_url)
    except ValueError as exc:
        decrypted_url = decrypt_apprise_url(nc.apprise_url)
        return templates.TemplateResponse(
            "partials/notification_edit_form.html",
            {
                "request": request,
                "watch": watch,
                "nc": nc,
                "decrypted_url": decrypted_url,
                "error": str(exc),
            },
        )

    try:
        validate_event_list(events)
    except ValueError as exc:
        decrypted_url = decrypt_apprise_url(nc.apprise_url)
        return templates.TemplateResponse(
            "partials/notification_edit_form.html",
            {
                "request": request,
                "watch": watch,
                "nc": nc,
                "decrypted_url": decrypted_url,
                "error": str(exc),
            },
        )

    nc.apprise_url = encrypt_apprise_url(apprise_url)
    nc.channel_hint = extract_channel_hint(apprise_url)
    nc.events = events
    audit(
        session,
        EventType.NOTIFICATION_CONFIG_UPDATED,
        watch_id=watch.id,
        config_id=str(nc.id),
        channel_hint=nc.channel_hint,
    )
    await session.commit()
    notifications = await get_watch_notifications(session, watch.id)
    return templates.TemplateResponse(
        "partials/watch_notifications.html",
        {
            "request": request,
            "watch": watch,
            "notifications": notifications,
            "notification_urls": _decrypt_notification_urls(notifications),
            "apprise_plugins": list_plugins(),
        },
    )


@router.post("/watches/{watch_id}/notifications/{config_id}/test-result")
async def watch_notification_test_result(
    request: Request,
    watch_id: str,
    config_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Send a test notification and return an OOB flash with the result."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    nc = await session.get(NotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watch_id=str(watch.id),
        watch_name=watch.name,
        watch_url=watch.url,
        occurred_at=datetime.now(UTC),
        metadata={"test": True},
    )
    try:
        outcome = await dispatch_event(event, nc.apprise_url)
    except Exception:
        logger.exception("test notification error", extra={"config_id": config_id})
        reason = "Internal error during dispatch"
        success = False
    else:
        success = outcome.success
        reason = outcome.reason
    audit(
        session,
        EventType.NOTIFICATION_TEST,
        watch_id=watch.id,
        config_id=str(nc.id),
        channel_hint=nc.channel_hint,
        success=success,
        reason=reason,
    )
    await session.commit()
    level = "success" if success else "error"
    message = f"Test notification: {reason}"
    return templates.TemplateResponse(
        "partials/flash_oob.html",
        {
            "request": request,
            "flash_oob_level": level,
            "flash_oob_message": message,
        },
    )


def _load_snapshot_text(storage: LocalStorage, snapshot, path_attr: str) -> str:
    """Load text content from a snapshot's storage path. Returns empty string on failure."""
    if not snapshot:
        return ""
    path = getattr(snapshot, path_attr, None)
    if not path:
        return ""
    try:
        return storage.load(path).decode(errors="replace")
    except FileNotFoundError:
        return ""


@router.get("/changes/{change_id}")
async def change_detail_page(
    request: Request,
    change_id: str,
    mode: str = "extracted",
    session: AsyncSession = Depends(get_db_session),
):
    """Change detail page with metadata, chunks, and diff."""
    detail = await get_change_detail(session, change_id)
    if not detail:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    storage = LocalStorage(base_dir=STORAGE_BASE_DIR)
    path_attr = "storage_path" if mode == "raw" else "text_path"
    prev_text = _load_snapshot_text(storage, detail["previous_snapshot"], path_attr)
    curr_text = _load_snapshot_text(storage, detail["current_snapshot"], path_attr)
    diff = generate_diff(prev_text, curr_text)

    context = {
        "request": request,
        "active_page": "watches",
        **detail,
        "diff": diff,
    }
    return templates.TemplateResponse("pages/change_detail.html", context)


@router.get("/partials/diff/{change_id}")
async def partial_diff(
    request: Request,
    change_id: str,
    mode: str = "extracted",
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: diff view (extracted text or raw content)."""
    detail = await get_change_detail(session, change_id)
    if not detail:
        return templates.TemplateResponse("pages/404.html", {"request": request}, status_code=404)

    storage = LocalStorage(base_dir=STORAGE_BASE_DIR)
    path_attr = "storage_path" if mode == "raw" else "text_path"
    prev_text = _load_snapshot_text(storage, detail["previous_snapshot"], path_attr)
    curr_text = _load_snapshot_text(storage, detail["current_snapshot"], path_attr)
    diff = generate_diff(prev_text, curr_text)
    return templates.TemplateResponse("partials/diff_view.html", {"request": request, "diff": diff})


@router.get("/audit")
async def audit_log_page(
    request: Request,
    event_type: str | None = None,
    watch_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """Audit log page with filtering."""
    event_type = event_type or None
    entries = await get_audit_entries(session, event_type=event_type, watch_id=watch_id)
    context = {
        "request": request,
        "active_page": "audit",
        "entries": entries,
        "event_type": event_type,
    }
    return templates.TemplateResponse("pages/audit_log.html", context)


@router.get("/partials/audit-table")
async def partial_audit_table(
    request: Request,
    event_type: str | None = None,
    watch_id: str | None = None,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: filtered audit log table."""
    event_type = event_type or None
    entries = await get_audit_entries(session, event_type=event_type, watch_id=watch_id)
    return templates.TemplateResponse(
        "partials/audit_table.html", {"request": request, "entries": entries}
    )


@router.get("/system")
async def system_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """System monitoring page -- queue health and rate limiter state."""
    queue = await get_queue_health(session)
    domains = await get_domains_with_watch_counts(session)
    context = {
        "request": request,
        "active_page": "system",
        "queue": queue,
        "domains": domains,
    }
    return templates.TemplateResponse("pages/system.html", context)
