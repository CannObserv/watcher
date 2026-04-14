# Notification Content Options Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-config/template field toggles controlling what extra information appears in notification bodies (diff summary, temporal context, domain).

**Architecture:** A new `ContentConfig` JSONB column on both notification models stores optional field toggles. At dispatch time, `notify.py` resolves the effective `ContentOptions` for each candidate, passes it to a new `content.py` builder, and the resulting body is forwarded to the dispatcher. Metadata enrichment happens at event-creation time in the worker so the dispatcher stays DB-free.

**Tech Stack:** SQLAlchemy (JSONB column, mapped_column), Pydantic v2 (nested model validation), Alembic (migration), Apprise, pytest-asyncio.

---

## Reality checks (from codebase audit)

- **Watch model does NOT have** `tags`, `description`, or `last_changed_at` — those are out of scope
- **Diff content** = chunk labels already in metadata (`added`, `removed`, `modified` lists) — no line-level diffs
- **`check_interval`** lives in `watch.schedule_config.get("interval", "")`, not a direct column
- **`DispatchCandidate`** is a private dataclass in `notify.py` — needs a `content_config` field
- **`dispatch_event`** signature today: `(event, apprise_url_encrypted)` — needs optional `body` param

---

## File map

| Action | Path | Responsibility |
|---|---|---|
| Create | `src/api/schemas/content_config.py` | `ContentOptions` + `ContentConfig` Pydantic models |
| Create | `src/core/notifications/content.py` | `resolve_options()` + `build_body()` |
| Create | `tests/api/schemas/test_content_config.py` | Schema validation unit tests |
| Create | `tests/core/notifications/test_content.py` | Content builder unit tests |
| Create | `alembic/versions/<rev>_add_content_config.py` | Migration: JSONB column on both tables |
| Modify | `src/core/models/notification_config.py` | Add `content_config: dict \| None` mapped column |
| Modify | `src/core/models/notification_template.py` | Same |
| Modify | `src/core/notifications/dispatcher.py` | Optional `body` param on `dispatch_event` |
| Modify | `src/core/notifications/notify.py` | `DispatchCandidate.content_config`; resolve + build body |
| Modify | `src/workers/tasks.py` | Enrich WatchEvent metadata with `effective_domain`, `check_interval` |
| Modify | `src/api/schemas/notification_config.py` | `content_config` on Create/Update/Response schemas |
| Modify | `src/api/schemas/notification_template.py` | Same |
| Modify | `src/api/routes/notification_configs.py` | Pass `content_config` through create/update |
| Modify | `src/api/routes/notification_templates.py` | Same |
| Modify | `src/dashboard/templates/partials/notification_add_row.html` | "Content Options" collapsed section |
| Modify | `src/dashboard/templates/partials/notification_edit_form.html` | Same |
| Modify | `src/dashboard/templates/partials/notification_template_add_row.html` | Same |
| Modify | `src/dashboard/templates/partials/notification_template_edit_form.html` | Same |
| Modify | `src/dashboard/routes.py` | Parse + pass content_config from form POST |

---

## Task 1: ContentOptions + ContentConfig Pydantic schemas

**Files:**
- Create: `src/api/schemas/content_config.py`
- Create: `tests/api/schemas/test_content_config.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/schemas/test_content_config.py
"""Tests for ContentOptions and ContentConfig schemas."""

import pytest
from pydantic import ValidationError

from src.api.schemas.content_config import ContentConfig, ContentOptions


class TestContentOptions:
    def test_defaults_all_false(self):
        opts = ContentOptions()
        assert opts.include_diff_snippet is False
        assert opts.include_diff_full is False
        assert opts.include_temporal_context is False
        assert opts.include_domain is False
        assert opts.diff_snippet_lines == 10

    def test_explicit_values(self):
        opts = ContentOptions(include_diff_snippet=True, diff_snippet_lines=5)
        assert opts.include_diff_snippet is True
        assert opts.diff_snippet_lines == 5

    def test_diff_snippet_lines_must_be_positive(self):
        with pytest.raises(ValidationError):
            ContentOptions(diff_snippet_lines=0)

    def test_diff_snippet_lines_max(self):
        with pytest.raises(ValidationError):
            ContentOptions(diff_snippet_lines=101)


class TestContentConfig:
    def test_defaults_empty(self):
        cfg = ContentConfig()
        assert cfg.default == ContentOptions()
        assert cfg.overrides == {}

    def test_override_valid_event_type(self):
        cfg = ContentConfig(
            default=ContentOptions(),
            overrides={"change_detected": ContentOptions(include_diff_snippet=True)},
        )
        assert cfg.overrides["change_detected"].include_diff_snippet is True

    def test_override_invalid_event_type_rejected(self):
        with pytest.raises(ValidationError):
            ContentConfig(overrides={"invalid_type": ContentOptions()})

    def test_roundtrip_json(self):
        cfg = ContentConfig(
            default=ContentOptions(include_domain=True),
            overrides={"watch_error": ContentOptions(include_temporal_context=True)},
        )
        data = cfg.model_dump()
        restored = ContentConfig.model_validate(data)
        assert restored.default.include_domain is True
        assert restored.overrides["watch_error"].include_temporal_context is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/api/schemas/test_content_config.py -v
```
Expected: ImportError (module doesn't exist yet)

- [ ] **Step 3: Implement `src/api/schemas/content_config.py`**

```python
"""Pydantic schemas for notification content configuration."""

from pydantic import BaseModel, Field, field_validator

from src.core.notifications.events import WatchEventType

_VALID_EVENT_TYPES = {e.value for e in WatchEventType}


class ContentOptions(BaseModel):
    """Field toggles controlling what extra information appears in a notification body."""

    include_diff_snippet: bool = False
    """Include the first `diff_snippet_lines` changed sections in the body."""

    diff_snippet_lines: int = Field(default=10, ge=1, le=100)
    """Max number of changed-section entries to include in snippet mode."""

    include_diff_full: bool = False
    """Include all changed sections. Supersedes include_diff_snippet if both set."""

    include_temporal_context: bool = False
    """Include check interval in the body."""

    include_domain: bool = False
    """Include the effective domain in the body."""


class ContentConfig(BaseModel):
    """Per-config content customisation: default options with optional per-event overrides."""

    default: ContentOptions = Field(default_factory=ContentOptions)
    """Applied to all events unless an override exists for the specific event type."""

    overrides: dict[str, ContentOptions] = Field(default_factory=dict)
    """event_type value → ContentOptions. Keys must be valid WatchEventType values."""

    @field_validator("overrides")
    @classmethod
    def validate_override_keys(cls, v: dict) -> dict:
        invalid = [k for k in v if k not in _VALID_EVENT_TYPES]
        if invalid:
            raise ValueError(
                f"Unknown event type(s) in overrides: {invalid}. "
                f"Valid types: {sorted(_VALID_EVENT_TYPES)}"
            )
        return v
```

- [ ] **Step 4: Run tests — expect pass**

```bash
uv run pytest tests/api/schemas/test_content_config.py -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/api/schemas/content_config.py tests/api/schemas/test_content_config.py
git commit -m "#88 feat: add ContentOptions + ContentConfig Pydantic schemas"
```

---

## Task 2: DB migration + ORM model changes

**Files:**
- Modify: `src/core/models/notification_config.py`
- Modify: `src/core/models/notification_template.py`
- Create: migration via `alembic revision --autogenerate`

- [ ] **Step 1: Write a failing integration test for the new column**

Add to `tests/api/test_notification_configs.py`:

```python
@pytest.mark.integration
async def test_notification_config_has_content_config_column(db_session, watch):
    """ORM model exposes content_config field (fails until migration + model are updated)."""
    from src.core.models.notification_config import WatchNotificationConfig
    config = WatchNotificationConfig(
        watch_id=watch.id,
        apprise_url="encrypted",
        channel_hint="slack",
        events=["change_detected"],
    )
    db_session.add(config)
    await db_session.flush()
    assert config.content_config is None  # default null
```

Add a parallel test to `tests/api/test_notification_templates.py` for `NotificationTemplate`.

- [ ] **Step 2: Run tests — verify failure**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/api/test_notification_configs.py -v -m integration -k "content_config_column"
```
Expected: `AttributeError` or `ProgrammingError` (column doesn't exist)

- [ ] **Step 3: Add `content_config` to both ORM models**

In `src/core/models/notification_config.py`, add after the `is_active` column:

```python
from sqlalchemy.dialects.postgresql import JSONB
# (add JSONB to imports)

content_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True, default=None)
```

In `src/core/models/notification_template.py`, add the same column in the same position.

- [ ] **Step 4: Generate migration**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run alembic revision --autogenerate -m "add content_config to notification tables"
```

Open the generated file in `alembic/versions/` and verify it contains two `add_column` calls — one for `watch_notification_configs`, one for `notification_templates`. Both should be `JSONB` nullable.

- [ ] **Step 5: Apply migration**

```bash
uv run alembic upgrade head
```
Expected: no errors

- [ ] **Step 6: Verify columns exist**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
psql "$DATABASE_URL" -c "\d watch_notification_configs" | grep content_config
psql "$DATABASE_URL" -c "\d notification_templates" | grep content_config
```
Expected: one line each showing `content_config | jsonb | nullable`

- [ ] **Step 7: Run the failing test from Step 1 — expect pass now**

```bash
uv run pytest tests/api/test_notification_configs.py tests/api/test_notification_templates.py -v -m integration -k "content_config_column"
```
Expected: pass

- [ ] **Step 8: Commit**

```bash
git add src/core/models/notification_config.py src/core/models/notification_template.py \
  alembic/versions/ tests/api/test_notification_configs.py tests/api/test_notification_templates.py
git commit -m "#88 feat: add content_config JSONB column to notification tables"
```

---

## Task 3: Content builder

**Files:**
- Create: `src/core/notifications/content.py`
- Create: `tests/core/notifications/test_content.py`

The builder formats a notification body from a `WatchEvent` + `ContentOptions`. Diff content is derived from the existing `added`, `removed`, `modified` metadata keys (already chunk labels). Enrichment metadata (`effective_domain`, `check_interval`) is added to the event in Task 6.

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/notifications/test_content.py
"""Tests for the notification content builder."""

from datetime import UTC, datetime

import pytest

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.content import build_body, resolve_options
from src.core.notifications.events import WatchEvent, WatchEventType

OCCURRED_AT = datetime(2026, 4, 14, 12, 0, 0, tzinfo=UTC)


def make_event(event_type=WatchEventType.CHANGE_DETECTED, metadata=None):
    return WatchEvent(
        event_type=event_type,
        watch_id="01HV0000000000000000000001",
        watch_name="Test Watch",
        watch_url="https://example.com",
        occurred_at=OCCURRED_AT,
        metadata=metadata or {},
    )


CHANGE_META = {
    "added": ["Licenses"],
    "removed": ["Hours"],
    "modified": [{"label": "Contact Info", "similarity": 0.85}],
}


class TestResolveOptions:
    def test_none_config_returns_defaults(self):
        opts = resolve_options(None, "change_detected")
        assert opts == ContentOptions()

    def test_default_used_when_no_override(self):
        cfg = ContentConfig(default=ContentOptions(include_domain=True))
        opts = resolve_options(cfg, "change_detected")
        assert opts.include_domain is True

    def test_override_takes_precedence(self):
        cfg = ContentConfig(
            default=ContentOptions(include_domain=True),
            overrides={"change_detected": ContentOptions(include_domain=False)},
        )
        opts = resolve_options(cfg, "change_detected")
        assert opts.include_domain is False

    def test_non_overridden_event_falls_back_to_default(self):
        cfg = ContentConfig(
            default=ContentOptions(include_domain=True),
            overrides={"watch_error": ContentOptions(include_domain=False)},
        )
        opts = resolve_options(cfg, "change_detected")
        assert opts.include_domain is True


class TestBuildBodyBase:
    def test_base_body_always_present(self):
        event = make_event()
        body = build_body(event, ContentOptions())
        assert event.body in body

    def test_no_extra_sections_by_default(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(event, ContentOptions())
        assert "Changed sections" not in body
        assert "Domain" not in body
        assert "Check interval" not in body


class TestBuildBodyDiffSnippet:
    def test_snippet_appended(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(event, ContentOptions(include_diff_snippet=True))
        assert "Changed sections" in body
        assert "+ Licenses" in body
        assert "- Hours" in body
        assert "~ Contact Info" in body

    def test_snippet_respects_limit(self):
        meta = {
            "added": ["A", "B", "C"],
            "removed": [],
            "modified": [],
        }
        event = make_event(metadata=meta)
        body = build_body(event, ContentOptions(include_diff_snippet=True, diff_snippet_lines=2))
        assert "+ A" in body
        assert "+ B" in body
        assert "+ C" not in body

    def test_full_supersedes_snippet(self):
        meta = {"added": ["A", "B", "C"], "removed": [], "modified": []}
        event = make_event(metadata=meta)
        # With full=True, snippet limit is ignored
        body = build_body(
            event,
            ContentOptions(include_diff_full=True, include_diff_snippet=True, diff_snippet_lines=1),
        )
        assert "+ A" in body
        assert "+ B" in body
        assert "+ C" in body

    def test_no_diff_section_when_metadata_empty(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_diff_snippet=True))
        assert "Changed sections" not in body

    def test_similarity_shown_for_modified(self):
        event = make_event(metadata=CHANGE_META)
        body = build_body(event, ContentOptions(include_diff_full=True))
        assert "85%" in body


class TestBuildBodyTemporalContext:
    def test_check_interval_shown(self):
        event = make_event(metadata={"check_interval": "1h"})
        body = build_body(event, ContentOptions(include_temporal_context=True))
        assert "Check interval" in body
        assert "1h" in body

    def test_no_section_when_metadata_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_temporal_context=True))
        assert "Check interval" not in body


class TestBuildBodyDomain:
    def test_domain_shown(self):
        event = make_event(metadata={"effective_domain": "example.com"})
        body = build_body(event, ContentOptions(include_domain=True))
        assert "Domain: example.com" in body

    def test_no_section_when_missing(self):
        event = make_event(metadata={})
        body = build_body(event, ContentOptions(include_domain=True))
        assert "Domain" not in body


class TestBuildBodyOrdering:
    def test_sections_joined_with_double_newline(self):
        event = make_event(metadata={"effective_domain": "ex.com", **CHANGE_META})
        body = build_body(
            event,
            ContentOptions(include_diff_snippet=True, include_domain=True),
        )
        # Base body comes first, then extra sections
        assert body.startswith(event.body)
        assert "\n\n" in body
```

- [ ] **Step 2: Run tests — verify failure**

```bash
uv run pytest tests/core/notifications/test_content.py -v
```
Expected: ImportError

- [ ] **Step 3: Implement `src/core/notifications/content.py`**

```python
"""Notification body builder — resolves ContentOptions and composes custom bodies."""

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.events import WatchEvent


def resolve_options(config: ContentConfig | None, event_type: str) -> ContentOptions:
    """Return the effective ContentOptions for this event type.

    Falls back to ContentOptions() (all defaults) when config is None.
    Uses per-event override if present, otherwise config.default.
    """
    if config is None:
        return ContentOptions()
    return config.overrides.get(event_type) or config.default


def build_body(event: WatchEvent, options: ContentOptions) -> str:
    """Compose a notification body from the event and resolved options.

    The existing event.body is always the first section. Extra sections are
    appended based on options and available metadata keys. Sections are
    joined with a blank line.
    """
    parts = [event.body]

    diff_section = _build_diff_section(event.metadata, options)
    if diff_section:
        parts.append(diff_section)

    if options.include_temporal_context:
        temporal = _build_temporal_section(event.metadata)
        if temporal:
            parts.append(temporal)

    if options.include_domain:
        domain = _build_domain_section(event.metadata)
        if domain:
            parts.append(domain)

    return "\n\n".join(parts)


def _build_diff_section(metadata: dict, options: ContentOptions) -> str:
    """Format chunk-level change summary. Returns empty string if no diff data."""
    if not (options.include_diff_snippet or options.include_diff_full):
        return ""

    added = metadata.get("added", [])
    removed = metadata.get("removed", [])
    modified = metadata.get("modified", [])

    if not added and not removed and not modified:
        return ""

    entries: list[str] = []
    for label in added:
        entries.append(f"  + {label}")
    for label in removed:
        entries.append(f"  - {label}")
    for item in modified:
        pct = int(item["similarity"] * 100)
        entries.append(f"  ~ {item['label']} ({pct}% similar)")

    # Snippet mode: limit total entries; full mode: no limit (include_diff_full supersedes)
    if not options.include_diff_full:
        entries = entries[: options.diff_snippet_lines]

    return "Changed sections:\n" + "\n".join(entries)


def _build_temporal_section(metadata: dict) -> str:
    """Format check interval. Returns empty string if not in metadata."""
    interval = metadata.get("check_interval")
    if not interval:
        return ""
    return f"Check interval: {interval}"


def _build_domain_section(metadata: dict) -> str:
    """Format effective domain. Returns empty string if not in metadata."""
    domain = metadata.get("effective_domain")
    if not domain:
        return ""
    return f"Domain: {domain}"
```

- [ ] **Step 4: Run tests — expect pass**

```bash
uv run pytest tests/core/notifications/test_content.py -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/core/notifications/content.py tests/core/notifications/test_content.py
git commit -m "#88 feat: add notification content builder (resolve_options + build_body)"
```

---

## Task 4: Dispatcher — optional body parameter

**Files:**
- Modify: `src/core/notifications/dispatcher.py`
- Modify: `tests/core/notifications/test_dispatcher.py`

- [ ] **Step 1: Write a new test covering body override**

Add to `tests/core/notifications/test_dispatcher.py`:

```python
@pytest.mark.asyncio
async def test_dispatch_event_uses_custom_body_when_provided(monkeypatch, mock_key):
    """body param overrides event.body when provided."""
    monkeypatch.setenv("APPRISE_SECRET_KEY", mock_key)
    event = make_event()  # use existing make_event fixture/helper in that file
    encrypted = encrypt_apprise_url("slack://T/A/B")

    captured_body = {}

    async def fake_notify(**kwargs):
        captured_body["body"] = kwargs.get("body")
        return True

    with patch("apprise.Apprise.async_notify", new=fake_notify):
        with patch("apprise.Apprise.add", return_value=True):
            result = await dispatch_event(event, encrypted, body="Custom body text")

    assert result.success is True
    assert captured_body["body"] == "Custom body text"


@pytest.mark.asyncio
async def test_dispatch_event_uses_event_body_when_no_override(monkeypatch, mock_key):
    """Without body param, falls back to event.body."""
    monkeypatch.setenv("APPRISE_SECRET_KEY", mock_key)
    event = make_event()
    encrypted = encrypt_apprise_url("slack://T/A/B")

    captured_body = {}

    async def fake_notify(**kwargs):
        captured_body["body"] = kwargs.get("body")
        return True

    with patch("apprise.Apprise.async_notify", new=fake_notify):
        with patch("apprise.Apprise.add", return_value=True):
            result = await dispatch_event(event, encrypted)

    assert captured_body["body"] == event.body
```

Read the existing `test_dispatcher.py` first to understand the fixtures (`make_event`, `mock_key`, `encrypt_apprise_url`) before adding these tests.

- [ ] **Step 2: Run new tests — verify failure**

```bash
uv run pytest tests/core/notifications/test_dispatcher.py -v -k "custom_body or no_override"
```
Expected: FAIL (body param not accepted)

- [ ] **Step 3: Update `dispatch_event` signature**

In `src/core/notifications/dispatcher.py`, change the function signature and `async_notify` call:

```python
async def dispatch_event(
    event: WatchEvent,
    apprise_url_encrypted: str,
    *,
    body: str | None = None,
) -> DispatchResult:
    """Dispatch a WatchEvent to a single Apprise target.

    body — if provided, overrides event.body for this dispatch. Use this to
    send per-config customised content while preserving the event title and
    notify_type.
    """
    url = decrypt_apprise_url(apprise_url_encrypted)
    ap = apprise.Apprise(asset=_ASSET)
    if not ap.add(url):
        logger.warning(
            "invalid apprise url in notification config",
            extra={"watch_id": event.watch_id, "event_type": event.event_type},
        )
        return DispatchResult(success=False, reason="Invalid Apprise URL: check your configuration")

    messages: list[str] = []
    token = _capture_ctx.set(messages)
    try:
        result = await ap.async_notify(
            body=body if body is not None else event.body,
            title=event.title,
            notify_type=event.apprise_notify_type,
        )
    finally:
        _capture_ctx.reset(token)

    if result is True:
        return DispatchResult(success=True, reason="Notification sent successfully")
    detail = "; ".join(messages) or "no detail captured"
    return DispatchResult(success=False, reason=f"Delivery failed: {detail}")
```

- [ ] **Step 4: Run all dispatcher tests**

```bash
uv run pytest tests/core/notifications/test_dispatcher.py -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/core/notifications/dispatcher.py tests/core/notifications/test_dispatcher.py
git commit -m "#88 feat: add optional body override to dispatch_event"
```

---

## Task 5: notify.py — DispatchCandidate + body resolution

**Files:**
- Modify: `src/core/notifications/notify.py`
- Modify: `tests/workers/test_notify.py`

`DispatchCandidate` gets a `content_config` field. Before calling `dispatch_event`, `notify.py` resolves options and builds a custom body.

- [ ] **Step 1: Write failing tests covering content_config dispatch**

Add to `tests/workers/test_notify.py`:

```python
@pytest.mark.asyncio
async def test_content_config_body_used_in_dispatch(set_test_key):
    """When a config has content_config, build_body is called and the result forwarded."""
    from src.api.schemas.content_config import ContentConfig, ContentOptions
    from src.core.notifications.notify import dispatch_event_notifications

    event = make_event(WatchEventType.CHANGE_DETECTED)
    # event metadata has no extra keys — build_body will still produce event.body + nothing extra

    content_cfg = ContentConfig(default=ContentOptions(include_domain=True))
    content_cfg_dict = content_cfg.model_dump()

    mock_config = MagicMock()
    mock_config.apprise_url = "encrypted_url"
    mock_config.content_config = content_cfg_dict

    dispatched_bodies = []

    async def fake_dispatch(ev, url, *, body=None):
        dispatched_bodies.append(body)
        return MagicMock(success=True, reason="ok")

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[
        _scalar_result(None),   # domain lookup
        _empty_result(),         # global templates
        _empty_result(),         # domain templates (skipped since no domain)
        _empty_result(),         # watch templates
        _scalars_result([mock_config]),  # local configs
    ])

    with patch("src.core.notifications.notify.dispatch_event", fake_dispatch):
        await dispatch_event_notifications(session, event)

    assert len(dispatched_bodies) == 1
    # body should equal event.body (no extra metadata in event)
    assert dispatched_bodies[0] == event.body


@pytest.mark.asyncio
async def test_null_content_config_passes_none_body(set_test_key):
    """content_config=None — dispatch_event called with body=None (dispatcher uses event.body)."""
    event = make_event(WatchEventType.CHANGE_DETECTED)

    mock_config = MagicMock()
    mock_config.apprise_url = "encrypted_url"
    mock_config.content_config = None

    dispatched_bodies = []

    async def fake_dispatch(ev, url, *, body=None):
        dispatched_bodies.append(body)
        return MagicMock(success=True, reason="ok")

    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=[
        _scalar_result(None),        # domain lookup
        _empty_result(),              # global templates
        _empty_result(),              # domain templates (skipped)
        _empty_result(),              # watch templates
        _scalars_result([mock_config]),  # local configs
    ])

    with patch("src.core.notifications.notify.dispatch_event", fake_dispatch):
        await dispatch_event_notifications(session, event)

    assert len(dispatched_bodies) == 1
    assert dispatched_bodies[0] is None  # no override — dispatcher falls back to event.body
```

Read the existing `test_notify.py` to understand the `_empty_result`, `_scalar_result`, `_scalars_result`, and session-mock helpers — use those exact helpers, don't reinvent them.

- [ ] **Step 2: Run tests — verify failure**

```bash
uv run pytest tests/workers/test_notify.py -v -k "content_config"
```
Expected: FAIL

- [ ] **Step 3: Update `notify.py`**

Add `content_config` to `DispatchCandidate` and resolve body before dispatching:

```python
from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.content import build_body, resolve_options

@dataclass
class DispatchCandidate:
    """A single notification target, drawn from global, domain, watch, or local source."""

    apprise_url: str
    source: str  # "global" | "domain" | "watch_template" | "local"
    source_id: str
    content_config: dict | None = None
```

When building candidates, pass the ORM model's `content_config` field:

```python
# For templates:
candidates.append(
    DispatchCandidate(
        apprise_url=tpl.apprise_url,
        source=source,
        source_id=tpl_id,
        content_config=tpl.content_config,
    )
)

# For local configs:
candidates.append(
    DispatchCandidate(
        apprise_url=c.apprise_url,
        source="local",
        source_id=str(c.id),
        content_config=c.content_config,
    )
)
```

In the dispatch loop, resolve and build body. Use `event.event_type.value` (a plain string) consistent with the rest of `notify.py` which already uses `event_value = event.event_type.value`:

```python
for candidate in candidates:
    try:
        cfg = ContentConfig.model_validate(candidate.content_config) if candidate.content_config else None
        options = resolve_options(cfg, event.event_type.value)
        custom_body = build_body(event, options) if cfg is not None else None
        result = await dispatch_event(event, candidate.apprise_url, body=custom_body)
        # ... rest of result handling unchanged
```

- [ ] **Step 4: Run all notify tests**

```bash
uv run pytest tests/workers/test_notify.py -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/core/notifications/notify.py tests/workers/test_notify.py
git commit -m "#88 feat: resolve content_config per dispatch candidate in notify.py"
```

---

## Task 6: Worker metadata enrichment

**Files:**
- Modify: `src/workers/tasks.py`
- Modify: `tests/workers/test_tasks.py`

Enrich WatchEvent metadata with `effective_domain` and `check_interval` for all event types. These are available on the `watch` object already loaded in `check_watch`.

- [ ] **Step 1: Write failing tests**

Add to `tests/workers/test_tasks.py` inside `TestCheckWatchTask`:

```python
async def test_change_detected_metadata_includes_domain_and_interval(
    self, db_session, tmp_path, monkeypatch
):
    """check_watch enriches change_detected metadata with effective_domain + check_interval."""
    import src.workers.tasks as tasks_mod

    watch = Watch(
        name="Enrichment Test",
        url="https://example.com/enrich",
        content_type=ContentType.HTML,
        effective_domain="example.com",
        schedule_config={"interval": "1h"},
    )
    db_session.add(watch)
    await db_session.flush()

    # First check to establish a baseline snapshot
    # (_run_check_pipeline is imported at module level in test_tasks.py)
    storage = LocalStorage(base_dir=tmp_path)
    await _run_check_pipeline(
        watch=watch,
        raw_content=b"<html><body><p>V1</p></body></html>",
        fetcher_used="http",
        fetch_duration_ms=50,
        storage=storage,
        session=db_session,
    )
    await db_session.commit()

    mock_response = httpx.Response(
        200,
        content=b"<html><body><p>V2 changed</p></body></html>",
        request=httpx.Request("GET", "https://example.com/enrich"),
    )
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: mock_response)
    )
    fast_limiter = DomainRateLimiter(min_interval=0.0)
    mock_registry = ServiceRegistry(fetcher=HttpFetcher(client=mock_client))
    monkeypatch.setattr(tasks_mod, "get_registry", lambda: mock_registry)
    monkeypatch.setattr(tasks_mod, "get_rate_limiter", lambda: fast_limiter)
    monkeypatch.setattr(tasks_mod, "STORAGE_BASE_DIR", tmp_path)
    monkeypatch.setattr(
        tasks_mod, "get_session_factory", lambda: _mock_session_factory(db_session)
    )

    captured_events = []

    async def fake_dispatch(session, event):
        captured_events.append(event)

    monkeypatch.setattr(tasks_mod, "dispatch_event_notifications", fake_dispatch)

    await check_watch(str(watch.id))

    change_events = [
        e for e in captured_events if e.event_type.value == "change_detected"
    ]
    assert len(change_events) == 1
    assert change_events[0].metadata["effective_domain"] == "example.com"
    assert change_events[0].metadata["check_interval"] == "1h"
```

- [ ] **Step 2: Run tests — verify failure**

```bash
uv run pytest tests/workers/test_tasks.py -v -k "metadata_includes"
```

- [ ] **Step 3: Update `tasks.py` — enrich metadata**

Add a helper to build the common enrichment fields:

```python
def _watch_base_metadata(watch: Watch) -> dict:
    """Common metadata fields added to all WatchEvents for content-builder use."""
    meta: dict = {}
    if watch.effective_domain:
        meta["effective_domain"] = watch.effective_domain
    interval = (watch.schedule_config or {}).get("interval")
    if interval:
        meta["check_interval"] = interval
    return meta
```

Update each WatchEvent construction to merge base metadata:

```python
# WATCH_ERROR (line ~99):
metadata={"status_code": fetch_result.status_code, **_watch_base_metadata(watch)},

# WATCH_RECOVERED (line ~140):
metadata=_watch_base_metadata(watch),

# CHANGE_DETECTED (line ~152):
metadata={**result.get("change_metadata", {}), **_watch_base_metadata(watch)},
```

Lifecycle events (`WATCH_CREATED`, `WATCH_PAUSED`, etc.) are created in API routes — those are updated in Task 8.

- [ ] **Step 4: Run all worker tests**

```bash
uv run pytest tests/workers/ -v
```
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/workers/tasks.py tests/workers/test_tasks.py
git commit -m "#88 feat: enrich WatchEvent metadata with effective_domain + check_interval"
```

---

## Task 7: API schemas — add content_config field

**Files:**
- Modify: `src/api/schemas/notification_config.py`
- Modify: `src/api/schemas/notification_template.py`

- [ ] **Step 1: Write failing tests for schema round-trip**

Add to `tests/api/test_schemas.py` (or a new `tests/api/schemas/test_notification_schemas.py` — check what file tests these schemas today):

```python
def test_watch_notification_config_create_accepts_content_config():
    """WatchNotificationConfigCreate accepts content_config and it's accessible."""
    from src.api.schemas.notification_config import WatchNotificationConfigCreate
    from src.api.schemas.content_config import ContentConfig, ContentOptions

    schema = WatchNotificationConfigCreate(
        apprise_url="slack://T/A/B/#chan",
        content_config=ContentConfig(default=ContentOptions(include_domain=True)),
    )
    assert schema.content_config.default.include_domain is True


def test_watch_notification_config_response_exposes_content_config():
    """WatchNotificationConfigResponse deserialises content_config from dict (ORM output)."""
    from src.api.schemas.notification_config import WatchNotificationConfigResponse
    import datetime

    resp = WatchNotificationConfigResponse.model_validate({
        "id": "01HV0000000000000000000001",
        "watch_id": "01HV0000000000000000000002",
        "title": None,
        "channel_hint": "slack",
        "events": ["change_detected"],
        "is_active": True,
        "created_at": datetime.datetime.now(datetime.UTC),
        "updated_at": datetime.datetime.now(datetime.UTC),
        "content_config": {"default": {"include_domain": True}, "overrides": {}},
    })
    assert resp.content_config.default.include_domain is True


def test_notification_template_create_accepts_content_config():
    """NotificationTemplateCreate accepts content_config."""
    from src.api.schemas.notification_template import NotificationTemplateCreate
    from src.api.schemas.content_config import ContentConfig, ContentOptions

    schema = NotificationTemplateCreate(
        title="My Template",
        apprise_url="slack://T/A/B/#chan",
        content_config=ContentConfig(default=ContentOptions(include_diff_snippet=True)),
    )
    assert schema.content_config.default.include_diff_snippet is True
```

- [ ] **Step 2: Run tests — verify failure**

```bash
uv run pytest tests/api/test_schemas.py -v -k "content_config"
```
Expected: FAIL (`content_config` not on schemas)

- [ ] **Step 3: Update `notification_config.py` schemas**

Add import at top:
```python
from src.api.schemas.content_config import ContentConfig
```

In `WatchNotificationConfigCreate`, add:
```python
content_config: ContentConfig | None = None
```

In `WatchNotificationConfigUpdate`, add:
```python
content_config: ContentConfig | None = None
```

In `WatchNotificationConfigResponse`, add:
```python
content_config: ContentConfig | None = None
```

For the response, add a `@field_validator` or `model_validator` to deserialise the raw dict from the ORM into `ContentConfig`:

```python
@field_validator("content_config", mode="before")
@classmethod
def parse_content_config(cls, v: dict | None) -> ContentConfig | None:
    if v is None:
        return None
    return ContentConfig.model_validate(v)
```

- [ ] **Step 2: Update `notification_template.py` schemas**

Same changes: add `content_config: ContentConfig | None = None` to Create/Update/Response, plus the same `parse_content_config` validator on Response.

- [ ] **Step 3: Run schema tests**

```bash
uv run pytest tests/api/test_schemas.py tests/api/test_notification_configs.py tests/api/test_notification_templates.py -v
```
Expected: all pass (new field is nullable/optional, so existing tests should not break)

- [ ] **Step 4: Commit**

```bash
git add src/api/schemas/notification_config.py src/api/schemas/notification_template.py
git commit -m "#88 feat: add content_config to notification config + template API schemas"
```

---

## Task 8: API routes — persist + return content_config

**Files:**
- Modify: `src/api/routes/notification_configs.py`
- Modify: `src/api/routes/notification_templates.py`

- [ ] **Step 1: Write failing integration tests first**

Add to `tests/api/test_notification_configs.py`:

```python
@pytest.mark.integration
async def test_create_config_with_content_config(client, watch, valid_apprise_url):
    """content_config round-trips through create → response."""
    resp = await client.post(
        f"/api/v1/watches/{watch.id}/notifications",
        json={
            "apprise_url": valid_apprise_url,
            "content_config": {
                "default": {"include_diff_snippet": True, "diff_snippet_lines": 5},
            },
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["content_config"]["default"]["include_diff_snippet"] is True
    assert data["content_config"]["default"]["diff_snippet_lines"] == 5


@pytest.mark.integration
async def test_patch_config_updates_content_config(client, watch, existing_config):
    """PATCH with content_config updates the stored value."""
    resp = await client.patch(
        f"/api/v1/watches/{watch.id}/notifications/{existing_config.id}",
        json={"content_config": {"default": {"include_domain": True}}},
    )
    assert resp.status_code == 200
    assert resp.json()["content_config"]["default"]["include_domain"] is True
```

Add parallel tests to `tests/api/test_notification_templates.py`. Read both test files first to understand fixture names (`client`, `watch`, `valid_apprise_url`, etc.).

- [ ] **Step 2: Run tests — verify failure**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/api/test_notification_configs.py tests/api/test_notification_templates.py \
  -v -m integration -k "content_config"
```
Expected: FAIL (route doesn't persist `content_config` yet)

- [ ] **Step 3: Update `notification_configs.py`**

In the **create** route, after building the `WatchNotificationConfig` ORM object, set:
```python
config.content_config = (
    body.content_config.model_dump() if body.content_config else None
)
```

In the **update** route (PATCH), if `"content_config"` is in `body.model_fields_set`:
```python
if "content_config" in body.model_fields_set:
    config.content_config = (
        body.content_config.model_dump() if body.content_config else None
    )
```

- [ ] **Step 4: Update `notification_templates.py`**

Same pattern for create and update routes.

- [ ] **Step 5: Run failing tests from Step 1 — expect pass**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/api/test_notification_configs.py tests/api/test_notification_templates.py \
  -v -m integration -k "content_config"
```
Expected: pass

- [ ] **Step 6: Run full integration suite**

```bash
uv run pytest tests/api/ -v -m integration
```
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/api/routes/notification_configs.py src/api/routes/notification_templates.py \
  tests/api/test_notification_configs.py tests/api/test_notification_templates.py
git commit -m "#88 feat: persist + return content_config in notification config + template routes"
```

---

## Task 9: Dashboard UI — content options section

**Files:**
- Modify: `src/dashboard/templates/partials/notification_add_row.html`
- Modify: `src/dashboard/templates/partials/notification_edit_form.html`
- Modify: `src/dashboard/templates/partials/notification_template_add_row.html`
- Modify: `src/dashboard/templates/partials/notification_template_edit_form.html`
- Modify: `src/dashboard/routes.py`

Read all four template files in full before editing. Follow existing form field patterns (`.form-input`, label spacing, dark mode).

- [ ] **Step 1: Read the four templates**

```bash
cat src/dashboard/templates/partials/notification_add_row.html
cat src/dashboard/templates/partials/notification_edit_form.html
cat src/dashboard/templates/partials/notification_template_add_row.html
cat src/dashboard/templates/partials/notification_template_edit_form.html
```

- [ ] **Step 2: Add a reusable content options partial**

Create `src/dashboard/templates/partials/notification_content_options.html`:

```html
{# Collapsed "Content Options" section for notification add/edit forms.
   Expects: content_config (ContentConfig | None) for pre-filling on edit.
   All inputs use name="content_config__*" flattened for form POST parsing. #}
<details class="mt-4">
  <summary class="cursor-pointer text-sm font-medium text-gray-700 dark:text-gray-300 select-none">
    Content Options
  </summary>
  <div class="mt-3 space-y-3 ps-1">

    {# Diff snippet #}
    <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
      <input type="checkbox" name="content_config__include_diff_snippet" value="1"
        class="rounded border-gray-300 dark:border-gray-600"
        {% if content_config and content_config.default.include_diff_snippet %}checked{% endif %}>
      Include diff snippet
    </label>
    <div class="flex items-center gap-2 ps-6 text-sm text-gray-600 dark:text-gray-400">
      <label for="snippet_lines">Lines:</label>
      <input type="number" id="snippet_lines" name="content_config__diff_snippet_lines"
        min="1" max="100" value="{{ content_config.default.diff_snippet_lines if content_config else 10 }}"
        class="form-input w-20 text-sm">
    </div>

    {# Full diff #}
    <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
      <input type="checkbox" name="content_config__include_diff_full" value="1"
        class="rounded border-gray-300 dark:border-gray-600"
        {% if content_config and content_config.default.include_diff_full %}checked{% endif %}>
      Include full diff <span class="text-xs text-gray-500 dark:text-gray-500">(verbose — email)</span>
    </label>

    {# Temporal context #}
    <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
      <input type="checkbox" name="content_config__include_temporal_context" value="1"
        class="rounded border-gray-300 dark:border-gray-600"
        {% if content_config and content_config.default.include_temporal_context %}checked{% endif %}>
      Temporal context <span class="text-xs text-gray-500 dark:text-gray-500">(check interval)</span>
    </label>

    {# Domain #}
    <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
      <input type="checkbox" name="content_config__include_domain" value="1"
        class="rounded border-gray-300 dark:border-gray-600"
        {% if content_config and content_config.default.include_domain %}checked{% endif %}>
      Domain
    </label>

  </div>
</details>
```

- [ ] **Step 3: Include the partial in all four form templates**

In each form template, add before the submit button:
```html
{% include "partials/notification_content_options.html" %}
```

Pass `content_config` from the template context when editing (see Step 5).

- [ ] **Step 4: Update dashboard routes to parse content_config from form POST**

In `src/dashboard/routes.py`, for all four POST handlers (local config create, local config edit, template create, template edit), add parsing of the `content_config__*` fields:

```python
from src.api.schemas.content_config import ContentConfig, ContentOptions

def _parse_content_config_from_form(form: dict) -> dict | None:
    """Extract content_config fields from a flat form POST dict."""
    opts = ContentOptions(
        include_diff_snippet="content_config__include_diff_snippet" in form,
        include_diff_full="content_config__include_diff_full" in form,
        include_temporal_context="content_config__include_temporal_context" in form,
        include_domain="content_config__include_domain" in form,
        diff_snippet_lines=int(form.get("content_config__diff_snippet_lines", 10)),
    )
    # Only store if any option differs from default (avoid writing null-equivalent dicts)
    default = ContentOptions()
    if opts == default:
        return None
    return ContentConfig(default=opts).model_dump()
```

Include this in each POST handler and pass `content_config=_parse_content_config_from_form(form)` to the relevant create/update API schema.

- [ ] **Step 5: Pass content_config to edit form templates**

In route handlers that render the edit forms (`notification_edit_form.html`, `notification_template_edit_form.html`), deserialise the stored dict:

```python
from src.api.schemas.content_config import ContentConfig

content_config = (
    ContentConfig.model_validate(nc.content_config) if nc.content_config else None
)
return templates.TemplateResponse(
    "partials/notification_edit_form.html",
    {"content_config": content_config, ...},
)
```

- [ ] **Step 6: Manual smoke test**

Start dev server:
```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8001 --reload
```

Open `https://watcher.exe.xyz:8001/` and:
1. Navigate to a watch → Notifications → Add Local
2. Expand "Content Options" — verify checkboxes appear
3. Check "Include diff snippet", set Lines to 5, save
4. Edit the same config — verify checkboxes are pre-filled
5. Repeat for a template in the template library

- [ ] **Step 7: Run full test suite**

```bash
uv run pytest -v
```
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add src/dashboard/templates/partials/notification_content_options.html \
  src/dashboard/templates/partials/notification_add_row.html \
  src/dashboard/templates/partials/notification_edit_form.html \
  src/dashboard/templates/partials/notification_template_add_row.html \
  src/dashboard/templates/partials/notification_template_edit_form.html \
  src/dashboard/routes.py
git commit -m "#88 feat: add Content Options UI section to notification add/edit forms"
```

---

## Task 10: Wire up and restart

- [ ] **Step 1: Restart systemd service**

```bash
sudo systemctl restart watcher
sudo journalctl -u watcher -f
```
Expected: no errors, service healthy

- [ ] **Step 2: Run full test suite one final time**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest -v
```
Expected: all pass

- [ ] **Step 3: Run linter**

```bash
uv run ruff check .
```
Expected: no errors

- [ ] **Step 4: Close the issue**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
gh issue close 88 --comment "Implemented: ContentOptions + ContentConfig schemas, DB migration, content builder, dispatcher body override, notify.py integration, worker metadata enrichment, API schema/route changes, dashboard UI."
```
