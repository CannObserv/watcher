# Notification Content Options Design

**Date:** 2026-04-14  
**Status:** Approved

## Goal

Allow per-config/template control over what information appears in notification bodies. Currently title and body are hardcoded computed properties on `WatchEvent` — every notification looks identical regardless of which config triggered it or which channel it targets. This prevents users from distinguishing what changed, tailoring verbose channels (email) from brief ones (Slack), or including watch-level context.

## Problem Statement

Priority order of pain points:
1. **Notifications lack context** — users can't tell from a notification alone what watch triggered it or what specifically changed
2. **No metadata beyond counts** — watch tags, description, domain, and actual diff content never reach the notification
3. **Channel mismatch** — Slack and email warrant different verbosity; no mechanism exists to differentiate

## Approved Approach

### Phase 1: Structured field toggles (this design)

Add a `content_config` JSONB column to both `WatchNotificationConfig` and `NotificationTemplate`. Backed by Pydantic models for validation. `null` = no customization, fully backwards-compatible.

### Phase 2 (future): Template strings

`ContentOptions` gains `title_template` / `body_template` Jinja2 string fields. No schema migration needed — JSONB column already in place.

### Future: AI-powered adaptive summaries

Interpret diff content to produce concise, contextual summaries of what changed and why the user cares. Out of scope for Phase 1 and 2.

---

## Data Model

### Pydantic schemas (`src/api/schemas/content_config.py`)

```python
class ContentOptions(BaseModel):
    include_diff_snippet: bool = False    # first N lines of diff
    diff_snippet_lines: int = 10          # max lines for snippet mode
    include_diff_full: bool = False       # full diff — verbose channels (email)
    include_temporal_context: bool = False  # last_changed_at + check_interval
    include_domain: bool = False          # effective_domain
    include_tags: bool = False            # watch tags (verify field exists on Watch)
    include_description: bool = False     # watch description (same caveat)

class ContentConfig(BaseModel):
    default: ContentOptions = ContentOptions()
    overrides: dict[str, ContentOptions] = {}  # WatchEventType value → ContentOptions
```

`overrides` keys are validated against `WatchEventType` enum values. Unset keys inherit `default`. Both `default` and `overrides` entries are optional — only configure what differs from the baseline.

### DB migration

Single Alembic migration:
- `ALTER TABLE watch_notification_configs ADD COLUMN content_config JSONB NULL`
- `ALTER TABLE notification_templates ADD COLUMN content_config JSONB NULL`

---

## WatchEvent Metadata Enrichment

The worker enriches `WatchEvent.metadata` at event creation time with all potentially-relevant data. The content builder picks what to use — the worker does not need to know what any config wants.

Fields added to metadata unconditionally where available:

| Key | Event types | Source |
|---|---|---|
| `diff_lines` | `change_detected` | differ output — unified diff format (list of strings) |
| `last_changed_at` | all | `Watch.last_changed_at` |
| `check_interval` | all | `Watch.interval` |
| `effective_domain` | all | `Watch.effective_domain` |
| `tags` | all | `Watch.tags` *(verify field exists)* |
| `description` | all | `Watch.description` *(verify field exists)* |

`diff_lines` is unified diff format: `["+added line", "-removed line", " context line", ...]`. Snippet mode takes `diff_lines[:diff_snippet_lines]`; full mode uses all lines. One representation, two consumption modes.

---

## Content Builder (`src/core/notifications/content.py`)

```python
def resolve_options(config: ContentConfig | None, event_type: str) -> ContentOptions:
    if config is None:
        return ContentOptions()  # all defaults — nothing extra included
    return config.overrides.get(event_type) or config.default

def build_body(event: WatchEvent, options: ContentOptions) -> str:
    parts = [event.body]  # existing hardcoded body always first
    # append sections based on options + available metadata
    # include_diff_full supersedes include_diff_snippet if both set
    ...
    return "\n\n".join(parts)
```

Customization is **additive** — the existing body is always the first part. No existing content is suppressed.

---

## Dispatch Integration

In `notify.py`, for each resolved config/template:

```python
options = resolve_options(config.content_config, event.event_type)
body = build_body(event, options) if config.content_config else event.body
await dispatch_event(event, apprise_url, body=body)
```

`dispatch_event` gains an optional `body` parameter; if provided, uses it instead of `event.body`. Title is unchanged in Phase 1.

---

## API Surface

`ContentConfig` added to create/update/response schemas for both models:

```python
content_config: ContentConfig | None = None
```

Serializes to/from JSONB. `null` round-trips cleanly.

---

## Dashboard UI

Content options appear as a **collapsed "Content Options" section** in existing add/edit forms (local configs and templates):

```
▶ Content Options

  [✓] Diff snippet     Lines: [10]
  [ ] Full diff
  [ ] Temporal context (last changed, check interval)
  [ ] Domain
  [ ] Tags
  [ ] Description

  [ ] Customize per event type   ← expands per-event accordion
    change_detected ▶            ← only events checked in the Events field
    watch_error ▶
```

Each per-event accordion row exposes the same checkbox set. Unconfigured per-event rows inherit `default`. The per-event accordion UI may be deferred to a follow-up — the data model supports it from day one regardless.

---

## Key Decisions

| Decision | Rationale |
|---|---|
| JSONB over explicit boolean columns | Single migration; extensible to Phase 2 template strings without schema changes; mirrors existing `events: ARRAY` pattern |
| Enrich metadata always, render selectively | Worker stays simple; dispatcher doesn't need to query config at event-creation time |
| Additive body composition | Zero regression risk; existing notification content never suppressed |
| `null` content_config = no change | Full backwards compatibility; zero behavior change for existing configs |
| Title unchanged in Phase 1 | Title customization deferred to Phase 2 (template strings) |

## Out of Scope

- Title customization
- Jinja2 template strings (Phase 2)
- AI-powered diff summaries (future)
- Template preview rendering
- Per-event-type UI accordion (can ship data model first, UI as follow-up)
