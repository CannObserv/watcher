"""Dashboard page routes — server-rendered HTML via Jinja2 + HTMX."""

import html as html_lib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import TemplateError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ulid import ULID

from src.api.dependencies import get_db_session, get_probe_fn
from src.api.routes.helpers import parse_ulid
from src.api.routes.watches import delete_watch as api_delete_watch
from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.api.schemas.notification_config import (
    extract_channel_hint,
    validate_apprise_url,
    validate_event_list,
)
from src.core.crypto import decrypt_apprise_url, encrypt_apprise_url
from src.core.database import get_session_factory
from src.core.diff import compute_unified_diff
from src.core.diff.normalize import normalize_html
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.domain import Domain
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.notification_template import DomainNcRef, NotificationTemplate, WatchNcRef
from src.core.models.snapshot import Snapshot
from src.core.models.watch import ContentType, Watch
from src.core.notifications.apprise_builder import (
    assemble_url,
    get_plugin_detail,
    get_service_name,
    list_plugins,
)
from src.core.notifications.content import build_body, build_title, resolve_options
from src.core.notifications.default_templates import (
    compose_body_prefill,
    compose_title_prefill,
)
from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import EVENT_TITLES, WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.notifications.preview_fixtures import (
    build_preview_event,
    compute_preview_unified_diff,
)
from src.core.probe import ProbeResult
from src.core.screenshot import capture_screenshot
from src.core.storage import LocalStorage, default_storage
from src.core.watches import create_watch as _create_watch
from src.dashboard import templates
from src.dashboard.context import (
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
    get_watch_list,
    get_watch_notifications,
    get_watch_profiles,
    get_watch_timeline,
    get_watch_timeline_count,
)
from src.dashboard.deps import get_dashboard_user

router = APIRouter(tags=["dashboard"], dependencies=[Depends(get_dashboard_user)])
logger = get_logger(__name__)

_ALL_EVENT_TYPE_VALUES: list[str] = [e.value for e in WatchEventType]


def _status_to_is_active(status: str | None) -> bool | None:
    """Convert status string param to is_active bool for DB queries."""
    if status == "active":
        return True
    if status == "inactive":
        return False
    return None


def _parse_content_config_from_form(form) -> dict | None:
    """Extract content_config fields from a flat form POST dict."""
    _lines_raw = form.get("content_config__diff_snippet_lines", "25")
    try:
        _lines = max(1, min(200, int(_lines_raw)))
    except (ValueError, TypeError):
        _lines = 25
    title_template = form.get("content_config__title_template", "").strip() or None
    body_template = form.get("content_config__body_template", "").strip() or None
    opts = ContentOptions(
        include_diff_snippet="content_config__include_diff_snippet" in form,
        include_diff_full="content_config__include_diff_full" in form,
        include_temporal_context="content_config__include_temporal_context" in form,
        include_domain="content_config__include_domain" in form,
        diff_snippet_lines=_lines,
        include_last_changed_at="content_config__include_last_changed_at" in form,
        include_significance="content_config__include_significance" in form,
        include_change_dashboard_url="content_config__include_change_dashboard_url" in form,
        include_tags="content_config__include_tags" in form,
        include_description="content_config__include_description" in form,
        title_template=title_template,
        body_template=body_template,
    )
    # Only store if at least one toggle is enabled or a template string is provided.
    any_enabled = (
        opts.include_diff_snippet
        or opts.include_diff_full
        or opts.include_temporal_context
        or opts.include_domain
        or opts.include_last_changed_at
        or opts.include_significance
        or opts.include_change_dashboard_url
        or opts.include_tags
        or opts.include_description
        or opts.title_template
        or opts.body_template
    )
    # Parse per-event overrides
    overrides: dict[str, ContentOptions] = {}
    for et_value in _ALL_EVENT_TYPE_VALUES:
        prefix = f"content_config__override__{et_value}__"
        et_opts = ContentOptions(
            include_diff_snippet=f"{prefix}include_diff_snippet" in form,
            include_diff_full=f"{prefix}include_diff_full" in form,
            include_temporal_context=f"{prefix}include_temporal_context" in form,
            include_domain=f"{prefix}include_domain" in form,
            include_last_changed_at=f"{prefix}include_last_changed_at" in form,
            include_significance=f"{prefix}include_significance" in form,
            include_change_dashboard_url=f"{prefix}include_change_dashboard_url" in form,
            include_tags=f"{prefix}include_tags" in form,
            include_description=f"{prefix}include_description" in form,
        )
        if any(
            x
            for x in (
                et_opts.include_diff_snippet,
                et_opts.include_diff_full,
                et_opts.include_temporal_context,
                et_opts.include_domain,
                et_opts.include_last_changed_at,
                et_opts.include_significance,
                et_opts.include_change_dashboard_url,
                et_opts.include_tags,
                et_opts.include_description,
            )
        ):
            overrides[et_value] = et_opts

    if not any_enabled and not overrides:
        return None
    return ContentConfig(default=opts, overrides=overrides).model_dump()


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
        "active_page": "dashboard",
        "stats": stats,
        "changes": changes,
        "queue": queue,
        "domains": domains,
    }
    return templates.TemplateResponse(request, "pages/dashboard.html", context)


@router.get("/watches")
async def watches_page(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    domain: str | None = None,
    sort: str = "last_checked_at",
    order: str = "desc",
    session: AsyncSession = Depends(get_db_session),
):
    """Watch list page."""
    is_active = _status_to_is_active(status)
    watches = await get_watch_list(
        session, is_active=is_active, search=q, domain=domain, sort=sort, order=order
    )
    health_map = {w.id: w.health_status for w in watches}
    context = {
        "active_page": "watches",
        "watches": watches,
        "q": q or "",
        "status": status or "",
        "domain": domain or "",
        "sort": sort,
        "order": order,
        "health_map": health_map,
    }
    return templates.TemplateResponse(request, "pages/watches.html", context)


@router.get("/watches/new")
async def watch_create_form(request: Request):
    """Watch creation form."""
    return templates.TemplateResponse(
        request,
        "pages/watch_form.html",
        {
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
    probe_fn: Callable[[str], Awaitable[ProbeResult]] = Depends(get_probe_fn),
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
            request,
            "pages/watch_form.html",
            {
                "active_page": "watches",
                "watch": None,
                "flash": flash,
                "content_types": list(ContentType),
            },
        )

    schedule_config = {}
    if interval.strip():
        schedule_config["interval"] = interval.strip()

    try:
        watch = await _create_watch(
            session=session,
            probe_fn=probe_fn,
            name=name.strip(),
            url=url.strip(),
            content_type=content_type,
            schedule_config=schedule_config,
            fetch_config={},
        )
    except httpx.HTTPError as exc:
        flash = {"type": "error", "message": f"URL unreachable: {exc}"}
        return templates.TemplateResponse(
            request,
            "pages/watch_form.html",
            {
                "active_page": "watches",
                "watch": None,
                "flash": flash,
                "content_types": list(ContentType),
            },
        )

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
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)
    profiles = await get_watch_profiles(session, watch.id)
    notifications = await get_watch_notifications(session, watch.id)
    latest_snapshot = await get_latest_snapshot(session, watch.id)

    # Build field contexts for content-type-aware rendering
    applicable_fields = _watch_fields_for_content_type(watch.content_type)
    field_contexts = {
        name: _watch_field_context(request, watch, name, mode="view") for name in applicable_fields
    }

    # Build snapshot metadata whenever a snapshot exists; screenshot fields are conditional.
    storage = default_storage
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
        "active_page": "watches",
        "watch": watch,
        "profiles": profiles,
        "notifications": notifications,
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
    return templates.TemplateResponse(request, "pages/watch_detail.html", context)


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

    storage = default_storage
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

    storage = default_storage
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

    storage = default_storage

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
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)
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
    return templates.TemplateResponse(request, "partials/watch_field.html", ctx)


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
        return templates.TemplateResponse(request, "partials/watch_field.html", ctx)
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
            request,
            "partials/watch_status_toggle.html",
            {"watch": watch, "domain_inactive": domain_inactive},
        )
    return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)


async def _dispatch_archive_notification(
    watch_id: str,
    watch_name: str,
    watch_url: str,
    occurred_at: datetime,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Dispatch WATCH_ARCHIVED notification in a background task with its own session.

    Best-effort: exceptions are logged and swallowed so notification failure never
    blocks or rolls back the archive action.

    ``session_factory`` is injected explicitly so tests can supply a factory
    scoped to the test database rather than the global production factory.
    """
    factory = session_factory if session_factory is not None else get_session_factory()
    try:
        async with factory() as session:
            await dispatch_event_notifications(
                session=session,
                event=WatchEvent(
                    event_type=WatchEventType.WATCH_ARCHIVED,
                    watch_id=watch_id,
                    watch_name=watch_name,
                    watch_url=watch_url,
                    occurred_at=occurred_at,
                ),
            )
    except Exception:
        logger.exception("failed to dispatch archive notification for watch %s", watch_id)


@router.post("/watches/{watch_id}/archive")
async def watch_archive(
    request: Request,
    watch_id: str,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db_session),
):
    """Archive a watch — sets is_archived=True and is_active=False."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

    if watch.is_archived:
        return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)

    watch.is_archived = True
    watch.is_active = False
    audit(session, EventType.WATCH_ARCHIVED, watch_id=watch.id, name=watch.name, source="dashboard")
    await session.commit()

    background_tasks.add_task(
        _dispatch_archive_notification,
        watch_id=str(watch.id),
        watch_name=watch.name,
        watch_url=watch.url,
        occurred_at=datetime.now(UTC),
        session_factory=get_session_factory(),
    )

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
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

    watch.is_archived = False
    # Watch stays inactive after restore — user re-activates via toggle
    audit(session, EventType.WATCH_RESTORED, watch_id=watch.id, name=watch.name, source="dashboard")
    await session.commit()

    return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)


@router.post("/watches/{watch_id}/deactivate")
async def watch_deactivate(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Deactivate a watch from the watch-list table row.

    Dedicated endpoint for the inline Deactivate button in watch_row.html.
    Returns the updated table row partial for HTMX outerHTML swap; falls back
    to a 303 redirect for non-HTMX (native form) requests.
    """
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    if watch.is_archived:
        raise HTTPException(status_code=409, detail="Cannot deactivate archived watch")

    if watch.is_active:
        watch.is_active = False
        audit(
            session,
            EventType.WATCH_DEACTIVATED,
            watch_id=watch.id,
            name=watch.name,
            source="dashboard",
        )
        await session.commit()
        await session.refresh(watch)

    if request.headers.get("HX-Request") == "true":
        health_map = {watch.id: watch.health_status}
        return templates.TemplateResponse(
            request,
            "partials/watch_row.html",
            {"watch": watch, "health_map": health_map},
        )
    return RedirectResponse(url="/watches", status_code=303)


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
    return templates.TemplateResponse(request, "pages/domains.html", context)


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
        request,
        "partials/domains_table.html",
        {
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
        request,
        "pages/domain_form.html",
        {"active_page": "domains", "flash": None, "url": ""},
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
            request,
            "pages/domain_form.html",
            {"active_page": "domains", "flash": flash, "url": url},
        )

    try:
        result = await probe_fn(url.strip())
    except Exception:
        flash = {
            "type": "error",
            "message": "Could not reach URL. Check the address and try again.",
        }
        return templates.TemplateResponse(
            request,
            "pages/domain_form.html",
            {"active_page": "domains", "flash": flash, "url": url},
        )

    domain_name = result.effective_domain
    if not domain_name:
        flash = {"type": "error", "message": "Could not extract domain from URL."}
        return templates.TemplateResponse(
            request,
            "pages/domain_form.html",
            {"active_page": "domains", "flash": flash, "url": url},
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
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

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
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

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
        health_map = {w.id: w.health_status for w in watches}
        return templates.TemplateResponse(
            request,
            "partials/domain_toggle_oob.html",
            {"domain": domain, "watches": watches, "health_map": health_map},
        )
    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.post("/domains/{name}/delete")
async def domain_delete(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Hard-delete an archived domain with no watches.

    Returns 200 + HX-Redirect on success so HTMX navigates the full page rather
    than swapping the redirect response into the #danger-zone-error element.
    Error cases return HTML fragments suitable for innerHTML swap into that target.
    """
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

    if domain.archived_at is None:
        msg = '<p class="text-red-600 text-sm mt-2">Archive the domain before deleting it.</p>'
        return HTMLResponse(status_code=409, content=msg)

    watch_result = await session.execute(
        select(Watch).where(Watch.effective_domain == name).limit(1)
    )
    if watch_result.scalar_one_or_none():
        msg = (
            f'<p class="text-red-600 text-sm mt-2">'
            f"Cannot delete: watches still reference domain '{html_lib.escape(name)}'.</p>"
        )
        return HTMLResponse(status_code=409, content=msg)

    audit(session, EventType.DOMAIN_DELETED, domain_name=name, source="dashboard")
    await session.delete(domain)
    await session.commit()

    return HTMLResponse(status_code=200, content="", headers={"HX-Redirect": "/domains"})


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
    return templates.TemplateResponse(request, "partials/domain_field.html", ctx)


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
        return templates.TemplateResponse(request, "partials/domain_field.html", ctx)
    return RedirectResponse(url=f"/domains/{name}", status_code=303)


@router.get("/domains/{name}")
async def domain_detail_page(
    request: Request,
    name: str,
    q: str | None = None,
    status: str | None = None,
    sort: str = "name",
    order: str = "asc",
    session: AsyncSession = Depends(get_db_session),
):
    """Domain detail page with config, watches, and danger zone."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

    is_active = _status_to_is_active(status)
    watches = await get_domain_watches(
        session, name, search=q, is_active=is_active, sort=sort, order=order
    )
    health_map = {w.id: w.health_status for w in watches}

    field_contexts = {
        fname: _field_context(request, domain, fname, mode="view") for fname in DOMAIN_FIELD_META
    }

    context = {
        "active_page": "domains",
        "domain": domain,
        "watches": watches,
        "health_map": health_map,
        "q": q or "",
        "status": status or "",
        "sort": sort,
        "order": order,
        "flash": None,
        "field_contexts": field_contexts,
    }
    return templates.TemplateResponse(request, "pages/domain_detail.html", context)


async def _render_domain_nc_defaults(request: Request, domain_name: str, session: AsyncSession):
    """Render the domain_nc_defaults partial for *domain_name*."""
    assigned_result = await session.execute(
        select(NotificationTemplate)
        .join(DomainNcRef, DomainNcRef.template_id == NotificationTemplate.id)
        .where(DomainNcRef.domain_name == domain_name)
        .order_by(NotificationTemplate.title)
    )
    assigned = assigned_result.scalars().all()
    assigned_ids = {str(t.id) for t in assigned}

    global_result = await session.execute(
        select(NotificationTemplate)
        .where(NotificationTemplate.is_global_default.is_(True))
        .order_by(NotificationTemplate.title)
    )
    global_templates = global_result.scalars().all()

    # Count assignable templates: active, non-global, not already assigned to this domain.
    # Used to disable the + Assign Existing button when empty.
    assignable_result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.is_active.is_(True),
            NotificationTemplate.is_global_default.is_(False),
        )
    )
    has_assignable = any(str(t.id) not in assigned_ids for t in assignable_result.scalars().all())

    return templates.TemplateResponse(
        request,
        "partials/domain_nc_defaults.html",
        {
            "domain_name": domain_name,
            "assigned": assigned,
            "global_templates": global_templates,
            "has_assignable": has_assignable,
        },
    )


@router.get("/domains/{domain_name}/nc-defaults/assign-row")
async def domain_nc_defaults_assign_row(
    request: Request,
    domain_name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX: inline assign-row form for adding a domain NC default."""
    assigned_result = await session.execute(
        select(NotificationTemplate)
        .join(DomainNcRef, DomainNcRef.template_id == NotificationTemplate.id)
        .where(DomainNcRef.domain_name == domain_name)
    )
    assigned = assigned_result.scalars().all()
    assigned_ids = {str(t.id) for t in assigned}

    all_result = await session.execute(
        select(NotificationTemplate)
        .where(
            NotificationTemplate.is_active.is_(True),
            NotificationTemplate.is_global_default.is_(False),
        )
        .order_by(NotificationTemplate.title)
    )
    all_templates = all_result.scalars().all()
    unassigned = [t for t in all_templates if str(t.id) not in assigned_ids]
    return templates.TemplateResponse(
        request,
        "partials/domain_nc_assign_row.html",
        {"domain_name": domain_name, "unassigned": unassigned},
    )


@router.get("/domains/{domain_name}/notifications/new")
async def domain_notification_new_page(
    request: Request,
    domain_name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Full page: create a new notification template for a domain."""
    domain = await session.scalar(select(Domain).where(Domain.name == domain_name))
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    return templates.TemplateResponse(
        request,
        "pages/domain_notification_new.html",
        {
            "domain_name": domain_name,
            "apprise_plugins": list_plugins(),
            "title": None,
            "events": None,
            "content_config": None,
            "error": None,
        },
    )


@router.post("/domains/{domain_name}/notifications/new")
async def domain_notification_create(
    request: Request,
    domain_name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Create a NotificationTemplate and link to domain. Redirects on success."""
    domain = await session.scalar(select(Domain).where(Domain.name == domain_name))
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    form = await request.form()
    title = str(form.get("title") or "").strip()
    events = form.getlist("events")
    schema_val = form.get("plugin_schema") or ""

    _cc = _parse_content_config_from_form(form)
    _parsed_config = ContentConfig.model_validate(_cc) if _cc else None

    def _page_error(msg: str):
        return templates.TemplateResponse(
            request,
            "pages/domain_notification_new.html",
            {
                "domain_name": domain_name,
                "apprise_plugins": list_plugins(),
                "title": title,
                "events": events,
                "content_config": _parsed_config,
                "error": msg,
            },
        )

    if not title:
        return _page_error("Title is required.")

    if schema_val:
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
            return _page_error(str(exc))
    else:
        apprise_url = str(form.get("apprise_url") or "")
        try:
            validate_apprise_url(apprise_url)
        except ValueError as exc:
            return _page_error(str(exc))

    try:
        validate_event_list(events)
    except ValueError as exc:
        return _page_error(str(exc))

    hint = get_service_name(schema_val) if schema_val else extract_channel_hint(apprise_url)
    tpl = NotificationTemplate(
        title=title,
        apprise_url=encrypt_apprise_url(apprise_url),
        channel_hint=hint,
        events=events,
        is_global_default=False,
        is_active=True,
        content_config=_cc,
    )
    session.add(tpl)
    await session.flush()
    session.add(DomainNcRef(domain_name=domain_name, template_id=tpl.id))
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_CREATED,
        template_id=str(tpl.id),
        title=title,
        channel_hint=hint,
        source="domain_dashboard",
        domain_name=domain_name,
    )
    audit(
        session,
        EventType.DOMAIN_NC_DEFAULT_ADDED,
        domain_name=domain_name,
        template_id=str(tpl.id),
    )
    await session.commit()
    return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)


@router.get("/domains/{domain_name}/nc-defaults")
async def domain_nc_defaults_partial(
    request: Request,
    domain_name: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: notification defaults assigned to a domain."""
    return await _render_domain_nc_defaults(request, domain_name, session)


@router.post("/domains/{domain_name}/nc-defaults/add/{template_id}")
async def domain_nc_default_add(
    request: Request,
    domain_name: str,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Add a notification template as a default for a domain."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)
    domain = await session.scalar(select(Domain).where(Domain.name == domain_name))
    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")
    existing = await session.scalar(
        select(DomainNcRef).where(
            DomainNcRef.domain_name == domain_name,
            DomainNcRef.template_id == template_id,  # type: ignore[arg-type]
        )
    )
    if not existing:
        session.add(
            DomainNcRef(domain_name=domain_name, template_id=template_id)  # type: ignore[arg-type]
        )
        audit(
            session,
            EventType.DOMAIN_NC_DEFAULT_ADDED,
            domain_name=domain_name,
            template_id=template_id,
        )
        await session.commit()
    return await _render_domain_nc_defaults(request, domain_name, session)


@router.post("/domains/{domain_name}/nc-defaults/remove/{template_id}")
async def domain_nc_default_remove(
    request: Request,
    domain_name: str,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Remove a notification template default from a domain."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=f"/domains/{domain_name}", status_code=303)
    result = await session.execute(
        select(DomainNcRef).where(
            DomainNcRef.domain_name == domain_name,
            DomainNcRef.template_id == template_id,  # type: ignore[arg-type]
        )
    )
    ref = result.scalar_one_or_none()
    if ref:
        await session.delete(ref)
        audit(
            session,
            EventType.DOMAIN_NC_DEFAULT_REMOVED,
            domain_name=domain_name,
            template_id=template_id,
        )
        await session.commit()
    return await _render_domain_nc_defaults(request, domain_name, session)


@router.get("/partials/stats-cards")
async def partial_stats_cards(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: stats cards only."""
    stats = await get_dashboard_stats(session)
    return templates.TemplateResponse(request, "partials/stats_cards.html", {"stats": stats})


@router.get("/partials/recent-changes")
async def partial_recent_changes(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: recent changes table."""
    changes = await get_recent_changes(session, limit=20)
    return templates.TemplateResponse(request, "partials/recent_changes.html", {"changes": changes})


@router.get("/partials/system-health")
async def partial_system_health(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: queue health and rate limiter."""
    queue = await get_queue_health(session)
    domains = await get_domains_with_watch_counts(session)
    return templates.TemplateResponse(
        request,
        "partials/system_health.html",
        {"queue": queue, "domains": domains},
    )


@router.get("/partials/watch-table")
async def partial_watch_table(
    request: Request,
    q: str | None = None,
    status: str | None = None,
    domain: str | None = None,
    sort: str = "last_checked_at",
    order: str = "desc",
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: watch table with filter, search, and sort."""
    is_active = _status_to_is_active(status)
    watches = await get_watch_list(
        session, is_active=is_active, search=q, domain=domain, sort=sort, order=order
    )
    health_map = {w.id: w.health_status for w in watches}
    return templates.TemplateResponse(
        request,
        "partials/watch_table.html",
        {
            "watches": watches,
            "health_map": health_map,
            "q": q or "",
            "status": status or "",
            "domain": domain or "",
            "sort": sort,
            "order": order,
        },
    )


@router.get("/partials/domain-watches/{name}")
async def partial_domain_watches(
    request: Request,
    name: str,
    q: str | None = None,
    status: str | None = None,
    sort: str = "name",
    order: str = "asc",
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: domain watch table with filter, search, and sort."""
    result = await session.execute(select(Domain).where(Domain.name == name))
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404)
    is_active = _status_to_is_active(status)
    watches = await get_domain_watches(
        session, name, search=q, is_active=is_active, sort=sort, order=order
    )
    health_map = {w.id: w.health_status for w in watches}
    return templates.TemplateResponse(
        request,
        "partials/domain_watches_table.html",
        {
            "domain": domain,
            "watches": watches,
            "health_map": health_map,
            "q": q or "",
            "status": status or "",
            "sort": sort,
            "order": order,
        },
    )


@router.get("/partials/watch-changes/{watch_id}")
async def partial_watch_changes(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: change history for a watch (legacy endpoint)."""
    changes = await get_watch_changes(session, watch_id)
    return templates.TemplateResponse(request, "partials/watch_changes.html", {"changes": changes})


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
        request,
        "partials/watch_timeline.html",
        {
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


@router.get("/partials/watch-notifications/{watch_id}")
async def partial_watch_notifications(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: notification config table for a watch."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    return await _render_watch_notifications(request, watch, session)


@router.get("/watches/{watch_id}/notifications/new")
async def watch_notification_new_page(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Full page: add a new local notification config for a watch."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    return templates.TemplateResponse(
        request,
        "pages/watch_notification_new.html",
        {
            "watch": watch,
            "apprise_plugins": list_plugins(),
            "title": None,
            "events": None,
            "content_config": None,
            "error": None,
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
        return templates.TemplateResponse(request, "partials/apprise_raw_url_form.html")
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
        request,
        "partials/apprise_plugin_form.html",
        {"plugin": detail, "variant": variant},
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
    title = str(form.get("title") or "").strip() or None

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
            _cc = _parse_content_config_from_form(form)
            return templates.TemplateResponse(
                request,
                "pages/watch_notification_new.html",
                {
                    "watch": watch,
                    "apprise_plugins": list_plugins(),
                    "title": str(form.get("title") or ""),
                    "events": form.getlist("events"),
                    "content_config": ContentConfig.model_validate(_cc) if _cc else None,
                    "error": str(exc),
                },
            )
    else:
        # Raw URL submission (legacy path)
        apprise_url = str(form.get("apprise_url") or "")
        try:
            validate_apprise_url(apprise_url)
        except ValueError as exc:
            _cc = _parse_content_config_from_form(form)
            return templates.TemplateResponse(
                request,
                "pages/watch_notification_new.html",
                {
                    "watch": watch,
                    "apprise_plugins": list_plugins(),
                    "title": str(form.get("title") or ""),
                    "events": form.getlist("events"),
                    "content_config": ContentConfig.model_validate(_cc) if _cc else None,
                    "error": str(exc),
                },
            )

    try:
        validate_event_list(events)
    except ValueError as exc:
        _cc = _parse_content_config_from_form(form)
        return templates.TemplateResponse(
            request,
            "pages/watch_notification_new.html",
            {
                "watch": watch,
                "apprise_plugins": list_plugins(),
                "title": str(form.get("title") or ""),
                "events": form.getlist("events"),
                "content_config": ContentConfig.model_validate(_cc) if _cc else None,
                "error": str(exc),
            },
        )

    hint = get_service_name(schema_val) if schema_val else extract_channel_hint(apprise_url)
    config = WatchNotificationConfig(
        watch_id=watch.id,
        title=title,
        apprise_url=encrypt_apprise_url(apprise_url),
        channel_hint=hint,
        events=events,
        content_config=_parse_content_config_from_form(form),
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
    return RedirectResponse(url=f"/watches/{watch_id}#watch-notifications", status_code=303)


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
    nc = await session.get(WatchNotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    nc.is_active = not nc.is_active
    audit(session, EventType.NOTIFICATION_CONFIG_UPDATED, watch_id=watch.id, config_id=str(nc.id))
    await session.commit()
    return await _render_watch_notifications(request, watch, session)


@router.get("/watches/{watch_id}/notifications/{config_id}/edit")
async def watch_notification_edit_page(
    request: Request,
    watch_id: str,
    config_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Full page: edit an existing watch notification config."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    nc = await session.get(WatchNotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")
    decryption_failed = False
    try:
        decrypted_url = decrypt_apprise_url(nc.apprise_url)
    except (InvalidToken, ValueError):
        decrypted_url = ""
        decryption_failed = True
    content_config = ContentConfig.model_validate(nc.content_config) if nc.content_config else None
    return templates.TemplateResponse(
        request,
        "pages/watch_notification_edit.html",
        {
            "watch": watch,
            "nc": nc,
            "decrypted_url": decrypted_url,
            "decryption_failed": decryption_failed,
            "content_config": content_config,
            "error": None,
        },
    )


@router.post("/watches/{watch_id}/notifications/{config_id}/edit")
async def watch_notification_edit(
    request: Request,
    watch_id: str,
    config_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Update apprise_url and/or events for a notification config."""
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    nc = await session.get(WatchNotificationConfig, parse_ulid(config_id, "Config"))
    if not nc or nc.watch_id != watch.id:
        raise HTTPException(status_code=404, detail="Config not found")

    form = await request.form()
    apprise_url = str(form.get("apprise_url") or "").strip()
    events = form.getlist("events")
    title = str(form.get("title") or "").strip() or None

    try:
        validate_apprise_url(apprise_url)
    except ValueError as exc:
        _cc = _parse_content_config_from_form(form)
        return templates.TemplateResponse(
            request,
            "pages/watch_notification_edit.html",
            {
                "watch": watch,
                "nc": nc,
                "submitted_title": title,
                "submitted_events": events,
                "decrypted_url": apprise_url,
                "decryption_failed": False,
                "content_config": ContentConfig.model_validate(_cc) if _cc else None,
                "error": str(exc),
            },
        )

    try:
        validate_event_list(events)
    except ValueError as exc:
        _cc = _parse_content_config_from_form(form)
        return templates.TemplateResponse(
            request,
            "pages/watch_notification_edit.html",
            {
                "watch": watch,
                "nc": nc,
                "submitted_title": title,
                "submitted_events": events,
                "decrypted_url": apprise_url,
                "decryption_failed": False,
                "content_config": ContentConfig.model_validate(_cc) if _cc else None,
                "error": str(exc),
            },
        )

    nc.apprise_url = encrypt_apprise_url(apprise_url)
    nc.channel_hint = extract_channel_hint(apprise_url)
    nc.events = events
    nc.title = title
    nc.content_config = _parse_content_config_from_form(form)
    audit(
        session,
        EventType.NOTIFICATION_CONFIG_UPDATED,
        watch_id=watch.id,
        config_id=str(nc.id),
        channel_hint=nc.channel_hint,
    )
    await session.commit()
    return RedirectResponse(url=f"/watches/{watch_id}#watch-notifications", status_code=303)


async def _render_watch_notifications(
    request: Request,
    watch,
    session: AsyncSession,
):
    """Fetch notifications for all four sources and render watch_notifications partial.

    Sources (in display order):
      global_templates  — NotificationTemplate.is_global_default=True
      domain_templates  — DomainNcRef for watch.effective_domain
      watch_templates   — WatchNcRef for this watch, minus global/domain ids
      notifications     — WatchNotificationConfig for this watch (local)
    """
    notifications = await get_watch_notifications(session, watch.id)

    # 1. Global templates
    global_result = await session.execute(
        select(NotificationTemplate)
        .where(NotificationTemplate.is_global_default.is_(True))
        .order_by(NotificationTemplate.title)
    )
    global_templates = global_result.scalars().all()
    global_ids = {str(t.id) for t in global_templates}

    # 2. Domain templates
    domain_templates = []
    domain_ids: set[str] = set()
    if watch.effective_domain:
        domain_result = await session.execute(
            select(NotificationTemplate)
            .join(DomainNcRef, DomainNcRef.template_id == NotificationTemplate.id)
            .where(DomainNcRef.domain_name == watch.effective_domain)
            .order_by(NotificationTemplate.title)
        )
        domain_templates = domain_result.scalars().all()
        domain_ids = {str(t.id) for t in domain_templates}

    # 3. Watch-assigned templates — exclude any already shown as global/domain
    auto_ids = global_ids | domain_ids
    watch_tpl_result = await session.execute(
        select(NotificationTemplate)
        .join(WatchNcRef, WatchNcRef.template_id == NotificationTemplate.id)
        .where(WatchNcRef.watch_id == watch.id)
        .order_by(NotificationTemplate.title)
    )
    watch_templates = [t for t in watch_tpl_result.scalars().all() if str(t.id) not in auto_ids]

    # Unassigned picker: active templates not global, not domain, not already watch-assigned
    all_watch_ids = auto_ids | {str(t.id) for t in watch_templates}
    all_result = await session.execute(
        select(NotificationTemplate)
        .where(
            NotificationTemplate.is_active.is_(True),
            NotificationTemplate.is_global_default.is_(False),
        )
        .order_by(NotificationTemplate.title)
    )
    unassigned_templates = [t for t in all_result.scalars().all() if str(t.id) not in all_watch_ids]

    return templates.TemplateResponse(
        request,
        "partials/watch_notifications.html",
        {
            "watch": watch,
            "notifications": notifications,
            "global_templates": global_templates,
            "domain_templates": domain_templates,
            "watch_templates": watch_templates,
            "unassigned_templates": unassigned_templates,
        },
    )


@router.get("/watches/{watch_id}/notifications/assign-row")
async def watch_nc_assign_row(
    request: Request,
    watch_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: inline assign-row form with picker of assignable templates.

    Excludes: global defaults (auto-dispatched), domain defaults for this watch's
    domain (auto-dispatched), and templates already assigned via WatchNcRef.
    """
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")

    # Already-assigned watch templates
    assigned_result = await session.execute(
        select(NotificationTemplate.id)
        .join(WatchNcRef, WatchNcRef.template_id == NotificationTemplate.id)
        .where(WatchNcRef.watch_id == watch.id)
    )
    excluded_ids = {str(row[0]) for row in assigned_result}

    # Domain templates for this watch's domain (auto-dispatched, don't show in picker)
    if watch.effective_domain:
        domain_result = await session.execute(
            select(DomainNcRef.template_id).where(DomainNcRef.domain_name == watch.effective_domain)
        )
        excluded_ids.update(str(row[0]) for row in domain_result)

    # Active, non-global templates not already excluded
    all_result = await session.execute(
        select(NotificationTemplate)
        .where(
            NotificationTemplate.is_active.is_(True),
            NotificationTemplate.is_global_default.is_(False),
        )
        .order_by(NotificationTemplate.title)
    )
    unassigned = [t for t in all_result.scalars().all() if str(t.id) not in excluded_ids]
    return templates.TemplateResponse(
        request,
        "partials/watch_nc_assign_row.html",
        {
            "watch": watch,
            "unassigned_templates": unassigned,
        },
    )


@router.post("/watches/{watch_id}/notifications/assign/{template_id}")
async def watch_nc_assign(
    request: Request,
    watch_id: str,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Assign a library template to a watch. Returns refreshed notifications partial."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    existing = await session.scalar(
        select(WatchNcRef).where(
            WatchNcRef.watch_id == watch.id,
            WatchNcRef.template_id == template_id,  # type: ignore[arg-type]
        )
    )
    if not existing:
        session.add(WatchNcRef(watch_id=watch.id, template_id=template_id))  # type: ignore[arg-type]
        audit(session, EventType.WATCH_NC_ASSIGNED, watch_id=str(watch.id), template_id=template_id)
        await session.commit()
    return await _render_watch_notifications(request, watch, session)


@router.post("/watches/{watch_id}/notifications/unassign/{template_id}")
async def watch_nc_unassign(
    request: Request,
    watch_id: str,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Remove a library template assignment from a watch. Returns refreshed partial."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    ref = await session.scalar(
        select(WatchNcRef).where(
            WatchNcRef.watch_id == watch.id,
            WatchNcRef.template_id == template_id,  # type: ignore[arg-type]
        )
    )
    if ref:
        await session.delete(ref)
        audit(
            session,
            EventType.WATCH_NC_UNASSIGNED,
            watch_id=str(watch.id),
            template_id=template_id,
        )
        await session.commit()
    return await _render_watch_notifications(request, watch, session)


@router.post("/watches/{watch_id}/notifications/copy-template/{template_id}")
async def watch_nc_copy_template(
    request: Request,
    watch_id: str,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Copy a library template ref to a local WatchNotificationConfig, removing the ref."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    tpl = await session.scalar(
        select(NotificationTemplate).where(NotificationTemplate.id == template_id)  # type: ignore[arg-type]
    )
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    local = WatchNotificationConfig(
        watch_id=watch.id,
        title=tpl.title,
        apprise_url=tpl.apprise_url,
        channel_hint=tpl.channel_hint,
        events=tpl.events,
        content_config=tpl.content_config,
    )
    session.add(local)
    ref = await session.scalar(
        select(WatchNcRef).where(
            WatchNcRef.watch_id == watch.id,
            WatchNcRef.template_id == template_id,  # type: ignore[arg-type]
        )
    )
    if ref:
        await session.delete(ref)
    audit(session, EventType.NOTIFICATION_CONFIG_CREATED, watch_id=str(watch.id))
    await session.commit()
    return await _render_watch_notifications(request, watch, session)


@router.post("/watches/{watch_id}/notifications/{config_id}/copy")
async def watch_nc_copy_local(
    request: Request,
    watch_id: str,
    config_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Duplicate a local WatchNotificationConfig on the same watch."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=f"/watches/{watch_id}", status_code=303)
    watch = await get_watch_detail(session, watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="Watch not found")
    orig = await session.scalar(
        select(WatchNotificationConfig).where(
            WatchNotificationConfig.id == config_id,  # type: ignore[arg-type]
            WatchNotificationConfig.watch_id == watch.id,
        )
    )
    if not orig:
        raise HTTPException(status_code=404)
    copy = WatchNotificationConfig(
        watch_id=watch.id,
        title=f"{orig.title} (copy)" if orig.title else None,
        apprise_url=orig.apprise_url,
        channel_hint=orig.channel_hint,
        events=orig.events,
        content_config=orig.content_config,
    )
    session.add(copy)
    audit(session, EventType.NOTIFICATION_CONFIG_CREATED, watch_id=str(watch.id))
    await session.commit()
    return await _render_watch_notifications(request, watch, session)


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
    nc = await session.get(WatchNotificationConfig, parse_ulid(config_id, "Config"))
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
        request,
        "partials/flash_oob.html",
        {
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


def _maybe_prettify_html(text: str, *, mode: str, content_type: ContentType | str | None) -> str:
    """For Raw Content mode on HTML watches, pretty-print before diffing so
    long single-line markup wraps readably (issue #118). Other modes / types
    pass through untouched — Extracted Text is already line-oriented; PDF/file
    content isn't HTML.

    html5lib is lenient but not invincible — exotic encodings, deeply nested
    DOMs, or lxml memory failures could throw. On any error, log and fall back
    to the unprettified text so the change-detail page degrades gracefully
    instead of 500ing.
    """
    if mode == "raw" and content_type == "html":
        try:
            return normalize_html(text)
        except Exception:
            logger.exception(
                "normalize_html failed; falling back to raw text",
                extra={"input_len": len(text), "content_type": str(content_type)},
            )
            return text
    return text


@router.get("/changes/{change_id}")
async def change_detail_page(
    request: Request,
    change_id: str,
    mode: Literal["extracted", "raw"] = "extracted",
    session: AsyncSession = Depends(get_db_session),
):
    """Change detail page with metadata, chunks, and diff."""
    detail = await get_change_detail(session, change_id)
    if not detail:
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

    storage = default_storage
    path_attr = "storage_path" if mode == "raw" else "text_path"
    prev_text = _load_snapshot_text(storage, detail["previous_snapshot"], path_attr)
    curr_text = _load_snapshot_text(storage, detail["current_snapshot"], path_attr)
    content_type = detail.get("watch_content_type")
    prev_text = _maybe_prettify_html(prev_text, mode=mode, content_type=content_type)
    curr_text = _maybe_prettify_html(curr_text, mode=mode, content_type=content_type)
    diff = compute_unified_diff(prev_text, curr_text)

    context = {
        "active_page": "watches",
        **detail,
        "diff": diff,
    }
    return templates.TemplateResponse(request, "pages/change_detail.html", context)


@router.get("/partials/diff/{change_id}")
async def partial_diff(
    request: Request,
    change_id: str,
    mode: Literal["extracted", "raw"] = "extracted",
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: diff view (extracted text or raw content)."""
    detail = await get_change_detail(session, change_id)
    if not detail:
        return templates.TemplateResponse(request, "pages/404.html", status_code=404)

    storage = default_storage
    path_attr = "storage_path" if mode == "raw" else "text_path"
    prev_text = _load_snapshot_text(storage, detail["previous_snapshot"], path_attr)
    curr_text = _load_snapshot_text(storage, detail["current_snapshot"], path_attr)
    content_type = detail.get("watch_content_type")
    prev_text = _maybe_prettify_html(prev_text, mode=mode, content_type=content_type)
    curr_text = _maybe_prettify_html(curr_text, mode=mode, content_type=content_type)
    diff = compute_unified_diff(prev_text, curr_text)
    return templates.TemplateResponse(request, "partials/diff_view.html", {"diff": diff})


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
        "active_page": "audit",
        "entries": entries,
        "event_type": event_type,
    }
    return templates.TemplateResponse(request, "pages/audit_log.html", context)


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
    return templates.TemplateResponse(request, "partials/audit_table.html", {"entries": entries})


# ---------------------------------------------------------------------------
# Notification Template Library — /notifications/*
# ---------------------------------------------------------------------------


@router.get("/partials/notification-templates-list")
async def partial_notification_templates_list(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """HTMX partial: notification template table rows (tbody content)."""
    result = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "partials/notification_template_list.html",
        {"notification_templates": notification_templates},
    )


@router.get("/notifications/overrides/add-picker")
async def notifications_override_add_picker(request: Request):
    """Return the override picker (a <select> of subscribed-but-not-overridden events).

    Called via HTMX from the [+ Add override] button; reads current form state
    to figure out which events are subscribed and which already have overrides.
    """
    params = request.query_params
    form_id = params.get("form_id") or "new"
    subscribed = set(params.getlist("events"))
    # Events already overridden — infer from presence of any
    # content_config__override__<et>__* key in the form state.
    overridden = set()
    for key in params.keys():
        if key.startswith("content_config__override__"):
            # content_config__override__<et>__<field>
            rest = key[len("content_config__override__") :]
            if "__" in rest:
                et_value = rest.split("__", 1)[0]
                overridden.add(et_value)
    pickable = [
        (v, EVENT_TITLES[v])
        for v in _ALL_EVENT_TYPE_VALUES
        if v in subscribed and v not in overridden
    ]
    return templates.TemplateResponse(
        request,
        "partials/notification_form_override_picker.html",
        {"form_id": form_id, "pickable": pickable},
    )


@router.get("/notifications/overrides/card")
async def notifications_override_card(request: Request):
    """Return a new override card, pre-populated by copying current default state.

    Called via HTMX after the user picks an event in the add-picker. Reads the
    current form's default `content_config__*` fields to seed the override's
    options; user then tweaks.
    """
    params = request.query_params
    form_id = params.get("form_id") or "new"
    event_type = params.get("event_type") or ""
    try:
        WatchEventType(event_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid event_type: {event_type}")
    # Parse current defaults; treat result as the starting options for the new override.
    cc_dict = _parse_content_config_from_form(params)
    config = ContentConfig.model_validate(cc_dict) if cc_dict else None
    seed_opts = config.default if config else ContentOptions()
    return templates.TemplateResponse(
        request,
        "partials/notification_form_override_card.html",
        {
            "form_id": form_id,
            "event_type": event_type,
            "prefix": f"content_config__override__{event_type}__",
            "opts": seed_opts,
            "scope": f"override-{event_type}",
        },
    )


@router.get("/notifications/compose-title-prefill")
async def notifications_compose_title_prefill(request: Request):
    """Return the default title Jinja template for the current preview_event.

    Used by the [Edit template] control on the Default title block to pre-fill
    the textarea with runnable Jinja the user can tweak.
    """
    preview_event_raw = request.query_params.get("preview_event") or "change_detected"
    try:
        et = WatchEventType(preview_event_raw)
    except ValueError:
        et = WatchEventType.CHANGE_DETECTED
    prefill = compose_title_prefill(et.value)
    return HTMLResponse(prefill)


@router.get("/notifications/compose-body-prefill")
async def notifications_compose_body_prefill(request: Request):
    """Return the default body Jinja template for the selected preview_event.

    Used by the "Show default template" control on the Default body block so the
    user can copy the skeleton into a custom body_template and edit from there.
    Toggles are not applied to the seed — they drive Python-side interleaving
    in `build_body`, not the template the user customises.
    """
    preview_event_raw = request.query_params.get("preview_event") or "change_detected"
    try:
        et = WatchEventType(preview_event_raw)
    except ValueError:
        et = WatchEventType.CHANGE_DETECTED
    return HTMLResponse(compose_body_prefill(et.value))


@router.post("/notifications/preview")
async def notifications_preview(request: Request):
    """Stateless live preview of a notification body + title.

    Consumes the full notification form via `hx-include="closest form"` plus a
    `preview_event` field selecting which WatchEventType to simulate. Renders
    title and body through the same pipeline as the dispatcher, but under
    strict-Jinja mode so template errors surface.

    Returns the `partials/notification_preview.html` fragment with either a
    rendered preview or an error card on template failure.
    """
    form = await request.form()
    preview_event_raw = form.get("preview_event") or "change_detected"
    try:
        et = WatchEventType(preview_event_raw)
    except ValueError:
        et = WatchEventType.CHANGE_DETECTED

    cc_dict = _parse_content_config_from_form(form)
    config = ContentConfig.model_validate(cc_dict) if cc_dict else None
    options = resolve_options(config, et.value)

    event = build_preview_event(et.value)
    preview_diff = compute_preview_unified_diff(et.value)

    try:
        title = build_title(event, options, strict=True)
    except TemplateError as exc:
        return templates.TemplateResponse(
            request,
            "partials/notification_preview.html",
            {"error": {"where": "title template", "message": str(exc)}},
        )

    try:
        body = build_body(event, options, strict=True, unified_diff=preview_diff)
    except TemplateError as exc:
        return templates.TemplateResponse(
            request,
            "partials/notification_preview.html",
            {"error": {"where": "body template", "message": str(exc)}},
        )

    return templates.TemplateResponse(
        request,
        "partials/notification_preview.html",
        {
            "preview": {
                "title": title,
                "body": body,
                "event_label": EVENT_TITLES[et.value],
            }
        },
    )


@router.get("/notifications")
async def notifications_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Notification template library page."""
    result = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result.scalars().all()
    return templates.TemplateResponse(
        request,
        "pages/notifications.html",
        {
            "active_page": "notifications",
            "notification_templates": notification_templates,
        },
    )


@router.get("/notifications/new")
async def notification_template_new_page(
    request: Request,
):
    """Full page: create a new notification template."""
    apprise_plugins = list_plugins()
    return templates.TemplateResponse(
        request,
        "pages/notification_new.html",
        {
            "active_page": "notifications",
            "apprise_plugins": apprise_plugins,
            "title": None,
            "events": None,
            "is_global_default": False,
            "content_config": None,
            "error": None,
        },
    )


@router.post("/notifications/new")
async def notification_template_create(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
):
    """Create notification template from dashboard form.

    Redirects on success, rerenders page on error.
    """
    form = await request.form()
    events = form.getlist("events")
    schema_val = form.get("plugin_schema") or ""
    title = str(form.get("title") or "").strip()
    is_global_default = bool(form.get("is_global_default"))

    def _page_error(error_msg: str):
        _cc = _parse_content_config_from_form(form)
        return templates.TemplateResponse(
            request,
            "pages/notification_new.html",
            {
                "active_page": "notifications",
                "apprise_plugins": list_plugins(),
                "title": str(form.get("title") or ""),
                "events": form.getlist("events"),
                "is_global_default": bool(form.get("is_global_default")),
                "content_config": ContentConfig.model_validate(_cc) if _cc else None,
                "error": error_msg,
            },
        )

    if not title:
        return _page_error("Title is required.")

    # Determine Apprise URL: token builder or raw input
    if schema_val:
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
            return _page_error(str(exc))
    else:
        apprise_url = str(form.get("apprise_url") or "")
        try:
            validate_apprise_url(apprise_url)
        except ValueError as exc:
            return _page_error(str(exc))

    try:
        validate_event_list(events)
    except ValueError as exc:
        return _page_error(str(exc))

    hint = get_service_name(schema_val) if schema_val else extract_channel_hint(apprise_url)
    tpl = NotificationTemplate(
        title=title,
        apprise_url=encrypt_apprise_url(apprise_url),
        channel_hint=hint,
        events=events,
        is_global_default=is_global_default,
        content_config=_parse_content_config_from_form(form),
    )
    session.add(tpl)
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_CREATED,
        template_id=str(tpl.id),
        title=title,
        channel_hint=hint,
        source="dashboard",
    )
    await session.commit()
    return RedirectResponse(url="/notifications", status_code=303)


@router.get("/notifications/{template_id}/edit")
async def notification_template_edit_page(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Full page: edit an existing notification template."""
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    decrypted_url = ""
    decryption_failed = False
    try:
        decrypted_url = decrypt_apprise_url(tpl.apprise_url)
    except (InvalidToken, ValueError):
        decryption_failed = True
    watch_count = (
        await session.scalar(select(func.count()).where(WatchNcRef.template_id == tpl.id)) or 0
    )
    domain_count = (
        await session.scalar(select(func.count()).where(DomainNcRef.template_id == tpl.id)) or 0
    )
    content_config = (
        ContentConfig.model_validate(tpl.content_config) if tpl.content_config else None
    )
    return templates.TemplateResponse(
        request,
        "pages/notification_edit.html",
        {
            "active_page": "notifications",
            "tpl": tpl,
            "submitted_title": tpl.title,
            "decrypted_url": decrypted_url,
            "decryption_failed": decryption_failed,
            "watch_count": watch_count,
            "domain_count": domain_count,
            "content_config": content_config,
            "error": None,
        },
    )


@router.post("/notifications/{template_id}/edit")
async def notification_template_edit(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Save changes to a notification template. Redirects on success, rerenders page on error."""
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    form = await request.form()
    apprise_url = str(form.get("apprise_url") or "").strip()
    events = form.getlist("events")
    title = str(form.get("title") or "").strip() or tpl.title
    is_global_default = bool(form.get("is_global_default"))

    async def _edit_error(error_msg: str) -> Response:
        watch_count = (
            await session.scalar(select(func.count()).where(WatchNcRef.template_id == tpl.id)) or 0
        )
        domain_count = (
            await session.scalar(select(func.count()).where(DomainNcRef.template_id == tpl.id)) or 0
        )
        # Re-derive content_config from submitted form so checkboxes stay checked on error
        _content_config_err = _parse_content_config_from_form(form)
        content_config_err = (
            ContentConfig.model_validate(_content_config_err) if _content_config_err else None
        )
        return templates.TemplateResponse(
            request,
            "pages/notification_edit.html",
            {
                "active_page": "notifications",
                "tpl": tpl,
                "submitted_title": title,
                "submitted_events": events,
                "decrypted_url": apprise_url,  # show what was submitted
                "decryption_failed": False,
                "watch_count": watch_count,
                "domain_count": domain_count,
                "content_config": content_config_err,
                "error": error_msg,
            },
        )

    try:
        validate_apprise_url(apprise_url)
    except ValueError as exc:
        return await _edit_error(str(exc))

    try:
        validate_event_list(events)
    except ValueError as exc:
        return await _edit_error(str(exc))

    tpl.title = title
    tpl.apprise_url = encrypt_apprise_url(apprise_url)
    tpl.channel_hint = extract_channel_hint(apprise_url)
    tpl.events = events
    tpl.is_global_default = is_global_default
    tpl.content_config = _parse_content_config_from_form(form)
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_UPDATED,
        template_id=str(tpl.id),
        title=tpl.title,
        channel_hint=tpl.channel_hint,
        source="dashboard",
    )
    await session.commit()
    return RedirectResponse(url="/notifications", status_code=303)


@router.post("/notifications/{template_id}/toggle")
async def notification_template_toggle(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Toggle is_active on a notification template. Returns refreshed list."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/notifications", status_code=303)
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    tpl.is_active = not tpl.is_active
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_UPDATED,
        template_id=str(tpl.id),
        title=tpl.title,
        is_active=tpl.is_active,
        source="dashboard",
    )
    await session.commit()
    result2 = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result2.scalars().all()
    response = templates.TemplateResponse(
        request,
        "partials/notification_template_list.html",
        {"notification_templates": notification_templates},
    )
    response.headers["HX-Trigger"] = "refreshTemplates"
    return response


@router.delete("/notifications/{template_id}/delete")
async def notification_template_delete(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a notification template (reject if refs exist). Returns refreshed list."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/notifications", status_code=303)
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    watch_count = (
        await session.scalar(select(func.count()).where(WatchNcRef.template_id == tpl.id)) or 0
    )
    domain_count = (
        await session.scalar(select(func.count()).where(DomainNcRef.template_id == tpl.id)) or 0
    )
    if watch_count or domain_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Template is still referenced by {watch_count} watch(es)"
                f" and {domain_count} domain(s)."
            ),
        )

    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_DELETED,
        template_id=str(tpl.id),
        title=tpl.title,
        source="dashboard",
    )
    await session.delete(tpl)
    await session.commit()
    result2 = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result2.scalars().all()
    response = templates.TemplateResponse(
        request,
        "partials/notification_template_list.html",
        {"notification_templates": notification_templates},
    )
    response.headers["HX-Trigger"] = "refreshTemplates"
    return response


@router.post("/notifications/{template_id}/duplicate")
async def notification_template_duplicate(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Duplicate a notification template. Returns refreshed list."""
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url="/notifications", status_code=303)
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")
    copy = NotificationTemplate(
        title=f"{tpl.title} (copy)",
        apprise_url=tpl.apprise_url,
        channel_hint=tpl.channel_hint,
        events=list(tpl.events),
        is_global_default=False,
        content_config=tpl.content_config,
    )
    session.add(copy)
    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_CREATED,
        template_id="(duplicate)",
        title=copy.title,
        source="dashboard",
    )
    await session.commit()
    result2 = await session.execute(
        select(NotificationTemplate).order_by(NotificationTemplate.title)
    )
    notification_templates = result2.scalars().all()
    response = templates.TemplateResponse(
        request,
        "partials/notification_template_list.html",
        {"notification_templates": notification_templates},
    )
    response.headers["HX-Trigger"] = "refreshTemplates"
    return response


@router.post("/notifications/{template_id}/test-result")
async def notification_template_test_result(
    request: Request,
    template_id: str,
    session: AsyncSession = Depends(get_db_session),
):
    """Send a test notification for a template and return an OOB flash."""
    result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.id == parse_ulid(template_id, "Template")
        )
    )
    tpl = result.scalar_one_or_none()
    if not tpl:
        raise HTTPException(status_code=404, detail="Template not found")

    event = WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watch_id="00000000000000000000000000",
        watch_name="Test Notification",
        watch_url="https://example.com",
        occurred_at=datetime.now(UTC),
        metadata={"test": True},
    )
    try:
        outcome = await dispatch_event(event, tpl.apprise_url)
    except Exception:
        logger.exception("test notification error", extra={"template_id": template_id})
        reason = "Internal error during dispatch"
        success = False
    else:
        success = outcome.success
        reason = outcome.reason

    audit(
        session,
        EventType.NOTIFICATION_TEMPLATE_TESTED,
        template_id=str(tpl.id),
        title=tpl.title,
        channel_hint=tpl.channel_hint,
        success=success,
        reason=reason,
        source="dashboard",
    )
    await session.commit()
    level = "success" if success else "error"
    message = f"Test notification: {reason}"
    return templates.TemplateResponse(
        request,
        "partials/flash_oob.html",
        {
            "flash_oob_level": level,
            "flash_oob_message": message,
        },
    )
