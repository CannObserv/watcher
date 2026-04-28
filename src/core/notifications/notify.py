"""Notification dispatch for watch lifecycle events."""

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.schemas.content_config import ContentConfig
from src.core.diff.normalize import normalize_html
from src.core.diff.textual import compute_unified_diff
from src.core.logging import get_logger
from src.core.models.audit_log import EventType, audit
from src.core.models.change import Change
from src.core.models.notification_config import WatchNotificationConfig
from src.core.models.notification_template import DomainNcRef, NotificationTemplate, WatchNcRef
from src.core.models.snapshot import Snapshot
from src.core.models.watch import ContentType, Watch
from src.core.notifications.content import (
    build_body,
    build_title,
    resolve_options,
)
from src.core.notifications.dispatcher import dispatch_event
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.storage import STORAGE_BASE_DIR, LocalStorage, StorageBackend

logger = get_logger(__name__)

# Match `diff_snippet` or `diff_full` referenced inside a Jinja delimiter pair
# (`{{ ... }}` or `{% ... %}`). Used by `_candidate_needs_unified_diff` so a
# template that mentions "diff_snippet" only in a comment or literal string
# doesn't trigger an unnecessary storage round-trip.
_DIFF_VAR_RE = re.compile(r"\{[{%][^{}]*\b(?:diff_snippet|diff_full)\b[^{}]*[}%]\}")


@dataclass
class DispatchCandidate:
    """A single notification target, drawn from global, domain, watch, or local source."""

    apprise_url: str
    source: str  # "global" | "domain" | "watch_template" | "local"
    source_id: str
    content_config: dict | None = None


def _candidate_needs_unified_diff(candidate: DispatchCandidate, event_value: str) -> bool:
    """True if rendering this candidate's body would consume `unified_diff`.

    Two paths to needing the diff:
      - resolved options have `include_diff_snippet` or `include_diff_full` on
      - a custom `body_template` references `diff_snippet` or `diff_full`
    """
    cfg_dict = candidate.content_config
    if not cfg_dict:
        return False
    try:
        cc = ContentConfig.model_validate(cfg_dict)
    except Exception:
        return False
    opts = resolve_options(cc, event_value)
    if opts.include_diff_snippet or opts.include_diff_full:
        return True
    tmpl = opts.body_template or ""
    return bool(_DIFF_VAR_RE.search(tmpl))


async def _load_event_unified_diff(
    session: AsyncSession,
    event: WatchEvent,
    *,
    storage: StorageBackend | None = None,
) -> str:
    """Lazily compute the unified diff for a change_detected event.

    For HTML watches, loads each side's `storage_path` (raw HTML) and runs
    `normalize_html` (html5lib pretty-print) so notification output mirrors
    the dashboard's Raw-mode diff (#118) — no long unwrapped lines.
    For non-HTML watches, loads `text_path` (the chunk-joined extracted text).

    Returns "" on any missing piece (no change_id, no Change row, missing
    snapshot, missing required path, unreadable artifact). Storage failures
    are non-fatal — we degrade to empty diff rather than blocking dispatch.
    """
    change_id = event.metadata.get("change_id")
    if not change_id:
        return ""
    try:
        change_ulid = ULID.from_str(str(change_id))
    except (TypeError, ValueError):
        return ""

    change = await session.get(Change, change_ulid)
    if not change:
        return ""

    prev = await session.get(Snapshot, change.previous_snapshot_id)
    curr = await session.get(Snapshot, change.current_snapshot_id)
    if not prev or not curr:
        return ""

    try:
        watch_ulid = ULID.from_str(event.watch_id)
    except (TypeError, ValueError):
        return ""
    watch = await session.get(Watch, watch_ulid)

    # HTML branch: diff the prettified raw HTML, mirroring the dashboard.
    # If watch is missing or non-HTML, fall through to the text_path path.
    if watch is not None and watch.content_type == ContentType.HTML:
        if not prev.storage_path or not curr.storage_path:
            return ""
        storage = storage or LocalStorage(base_dir=STORAGE_BASE_DIR)
        try:
            prev_raw = storage.load(prev.storage_path).decode(errors="replace")
            curr_raw = storage.load(curr.storage_path).decode(errors="replace")
        except Exception:
            # Broad catch — StorageBackend doesn't constrain exception types.
            logger.warning(
                "snapshot raw load failed; skipping unified diff",
                extra={"watch_id": event.watch_id, "change_id": str(change_id)},
                exc_info=True,
            )
            return ""
        try:
            prev_pretty = normalize_html(prev_raw)
            curr_pretty = normalize_html(curr_raw)
        except Exception:
            logger.warning(
                "normalize_html failed; skipping unified diff",
                extra={"watch_id": event.watch_id, "change_id": str(change_id)},
                exc_info=True,
            )
            return ""
        return compute_unified_diff(prev_pretty, curr_pretty).unified_diff

    # Non-HTML branch: diff the stored extracted text.
    if not prev.text_path or not curr.text_path:
        return ""
    storage = storage or LocalStorage(base_dir=STORAGE_BASE_DIR)
    try:
        prev_text = storage.load(prev.text_path).decode(errors="replace")
        curr_text = storage.load(curr.text_path).decode(errors="replace")
    except Exception:
        # Broad catch — StorageBackend doesn't constrain exception types.
        logger.warning(
            "snapshot text load failed; skipping unified diff",
            extra={"watch_id": event.watch_id, "change_id": str(change_id)},
            exc_info=True,
        )
        return ""

    return compute_unified_diff(prev_text, curr_text).unified_diff


async def dispatch_event_notifications(
    session: AsyncSession,
    event: WatchEvent,
) -> None:
    """Dispatch a WatchEvent to all active, opted-in notification targets.

    Queries four live sources in priority order:
      1. Global templates (NotificationTemplate.is_global_default=True) — all watches
      2. Domain templates (DomainNcRef) — watches whose effective_domain matches
      3. Watch-assigned templates (WatchNcRef) — this watch only, deduped vs. 1+2
      4. Local configs (WatchNotificationConfig) — this watch only

    Template sources are deduplicated by template_id so a template that appears in
    multiple sources (e.g. global AND manually assigned via WatchNcRef) fires once.
    Failures are logged but never raise. Writes a single audit log entry. Does not
    commit; caller is responsible.

    For change_detected events, the prev/curr extracted text is lazily loaded
    via `_load_event_unified_diff` once per event when at least one candidate
    needs it (toggle on or `body_template` references `diff_snippet`/`diff_full`),
    then reused across every candidate's body render.
    """
    watch_ulid = ULID.from_str(event.watch_id)
    event_value = event.event_type.value

    # Resolve effective_domain for this watch
    domain_row = await session.execute(select(Watch.effective_domain).where(Watch.id == watch_ulid))
    effective_domain: str | None = domain_row.scalar_one_or_none()

    # 1. Global templates
    global_result = await session.execute(
        select(NotificationTemplate).where(
            NotificationTemplate.is_global_default.is_(True),
            NotificationTemplate.is_active.is_(True),
            NotificationTemplate.events.contains([event_value]),
        )
    )
    global_templates = global_result.scalars().all()

    # 2. Domain templates
    domain_templates = []
    if effective_domain:
        domain_result = await session.execute(
            select(NotificationTemplate)
            .join(DomainNcRef, DomainNcRef.template_id == NotificationTemplate.id)
            .where(
                DomainNcRef.domain_name == effective_domain,
                NotificationTemplate.is_active.is_(True),
                NotificationTemplate.events.contains([event_value]),
            )
        )
        domain_templates = domain_result.scalars().all()

    # 3. Watch-assigned templates (WatchNcRef)
    watch_tpl_result = await session.execute(
        select(NotificationTemplate)
        .join(WatchNcRef, WatchNcRef.template_id == NotificationTemplate.id)
        .where(
            WatchNcRef.watch_id == watch_ulid,
            NotificationTemplate.is_active.is_(True),
            NotificationTemplate.events.contains([event_value]),
        )
    )
    watch_templates = watch_tpl_result.scalars().all()

    # 4. Local configs
    local_result = await session.execute(
        select(WatchNotificationConfig).where(
            WatchNotificationConfig.watch_id == watch_ulid,
            WatchNotificationConfig.is_active.is_(True),
            WatchNotificationConfig.events.contains([event_value]),
        )
    )
    local_configs = local_result.scalars().all()

    # Build deduped candidate list: templates first (global → domain → watch), then local
    seen_template_ids: set[str] = set()
    candidates: list[DispatchCandidate] = []

    for source, tpl_list in [
        ("global", global_templates),
        ("domain", domain_templates),
        ("watch_template", watch_templates),
    ]:
        for tpl in tpl_list:
            tpl_id = str(tpl.id)
            if tpl_id not in seen_template_ids:
                seen_template_ids.add(tpl_id)
                candidates.append(
                    DispatchCandidate(
                        apprise_url=tpl.apprise_url,
                        source=source,
                        source_id=tpl_id,
                        content_config=tpl.content_config,
                    )
                )

    for c in local_configs:
        candidates.append(
            DispatchCandidate(
                apprise_url=c.apprise_url,
                source="local",
                source_id=str(c.id),
                content_config=c.content_config,
            )
        )

    if not candidates:
        return

    # Lazy-load the unified diff once per event when at least one candidate
    # needs it. Reused across every candidate's body render — never recomputed.
    unified_diff: str = ""
    if event.event_type == WatchEventType.CHANGE_DETECTED and any(
        _candidate_needs_unified_diff(c, event_value) for c in candidates
    ):
        unified_diff = await _load_event_unified_diff(session, event)

    results = []
    for candidate in candidates:
        try:
            cfg = (
                ContentConfig.model_validate(candidate.content_config)
                if candidate.content_config
                else None
            )
            options = resolve_options(cfg, event_value)
            rendered_title = build_title(event, options)
            rendered_body = build_body(event, options, unified_diff=unified_diff)
            result = await dispatch_event(
                event, candidate.apprise_url, body=rendered_body, title=rendered_title
            )
            results.append(
                {
                    "source": candidate.source,
                    "source_id": candidate.source_id,
                    "success": result.success,
                    "reason": result.reason,
                }
            )
            extra = {
                "source": candidate.source,
                "source_id": candidate.source_id,
                "watch_id": event.watch_id,
                "event_type": event.event_type,
            }
            if result.success:
                logger.info("notification sent", extra=extra)
            else:
                logger.warning("notification failed", extra=extra)
        except Exception:
            logger.exception(
                "notification dispatch error",
                extra={"source": candidate.source, "source_id": candidate.source_id},
            )
            results.append(
                {
                    "source": candidate.source,
                    "source_id": candidate.source_id,
                    "success": False,
                    "reason": "exception",
                }
            )

    audit(
        session,
        EventType.NOTIFICATION_DISPATCHED,
        watch_id=event.watch_id,
        watch_event_type=event.event_type,
        results=results,
    )
