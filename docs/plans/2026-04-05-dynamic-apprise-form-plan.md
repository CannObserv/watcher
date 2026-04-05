# Dynamic Apprise URL Builder — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the raw Apprise URL text input with a two-step plugin picker + token form that dynamically generates typed inputs from Apprise's own introspection API.

**Architecture:** A new `apprise_builder` module handles catalog introspection and URL assembly. Two new API endpoints expose the catalog to the dashboard. The `NotificationConfigCreate` schema gains a `{schema, tokens}` path alongside the existing `apprise_url` path. The dashboard add-notification form becomes two-step: plugin picker (populated from the catalog) then an HTMX-swapped token form.

**Tech Stack:** Python/FastAPI, Apprise 1.9.9+ introspection API (`apprise.Apprise().details()`), Pydantic v2, HTMX, Jinja2, pytest (unit + integration).

---

## File Map

**New files:**
- `src/core/notifications/apprise_builder.py` — catalog lookup + URL assembly logic
- `src/api/routes/apprise_plugins.py` — `GET /api/v1/apprise/plugins` and `.../plugins/{schema}`
- `src/api/schemas/apprise_plugin.py` — Pydantic response schemas for plugin list/detail
- `src/dashboard/templates/partials/apprise_plugin_form.html` — token form partial (HTMX target)
- `src/dashboard/templates/partials/apprise_raw_url_form.html` — raw URL fallback partial
- `tests/core/notifications/test_apprise_builder.py`
- `tests/api/test_apprise_plugins.py`

**Modified files:**
- `src/api/main.py` — mount `apprise_plugins_router`
- `src/api/schemas/notification_config.py` — union input: `apprise_url` OR `{schema, tokens}`
- `src/api/routes/notification_configs.py` — handle token-based creation (no logic change needed; schema validator resolves `apprise_url` before route sees it)
- `src/dashboard/routes.py` — add plugin form partial route; extend `watch_notification_create` to handle token form submission
- `src/dashboard/templates/partials/watch_notifications.html` — two-step add form

---

## Task 1: Plugin catalog + URL assembly (`apprise_builder.py`)

**Files:**
- Create: `src/core/notifications/apprise_builder.py`
- Create: `tests/core/notifications/test_apprise_builder.py`

This module is the foundation for everything else. All Apprise introspection lives here; no other module imports from `apprise` for catalog purposes.

### Data structures returned by this module

```python
# list_plugins() returns:
[{"schema": "discord", "service_name": "Discord", "category": "native"}, ...]

# get_plugin_detail("discord") returns:
{
    "schema": "discord",
    "service_name": "Discord",
    "tokens": {
        "webhook_id": {"name": "Webhook ID", "type": "string", "required": True, "private": True, "default": None, "values": None, "regex": None},
        "webhook_token": {"name": "Webhook Token", "type": "string", "required": True, "private": True, "default": None, "values": None, "regex": None},
        "botname": {"name": "Bot Name", "type": "string", "required": False, "private": False, "default": None, "values": None, "regex": None},
    },
    "variants": [],  # empty = no variant selector needed
}

# For Slack (divergent variants):
{
    "schema": "slack",
    "service_name": "Slack",
    "tokens": { ... all tokens ... },
    "variants": [
        {"label": "Slack (Legacy Token)", "required_token_names": ["token_a", "token_b", "token_c"]},
        {"label": "Slack (Bot Token)", "required_token_names": ["access_token"]},
    ],
}
```

**Variant detection logic:** Group the plugin's templates by the set of their required path tokens. If more than one distinct group exists, expose a variant per group. Label each variant as `"{service_name} ({token_names joined})"` — the UI can prettify this later.

**URL assembly logic:**
1. Look up the plugin in the internal catalog dict (keyed by first schema value)
2. If `variant_index` is given, filter to that variant's templates; else use all templates
3. Iterate templates in order; for each, extract `{token}` placeholders minus `{schema}`
4. Find the required tokens that appear in this template (intersection with the plugin's `required` set)
5. If all such required tokens are present in `submitted_tokens`, use this template
6. Substitute: replace `{schema}` with the plugin's first `secure_protocols` or `protocols` value; replace `{tok}` with `submitted_tokens[tok]`; strip any remaining unfilled `{optional}` placeholders with regex
7. Validate the assembled URL with `apprise.Apprise().add()`; raise `ValueError` if invalid

The `schema` token is never required from the user — it maps to the plugin's own scheme value.

- [ ] **Step 1: Write failing tests**

```python
# tests/core/notifications/test_apprise_builder.py
"""Unit tests for apprise_builder catalog + URL assembly."""

import pytest
from src.core.notifications.apprise_builder import (
    assemble_url,
    get_plugin_detail,
    list_plugins,
)


class TestListPlugins:
    def test_returns_list(self):
        plugins = list_plugins()
        assert isinstance(plugins, list)
        assert len(plugins) > 100

    def test_sorted_by_service_name(self):
        plugins = list_plugins()
        names = [p["service_name"] for p in plugins]
        assert names == sorted(names, key=str.lower)

    def test_contains_discord(self):
        plugins = list_plugins()
        schemas = [p["schema"] for p in plugins]
        assert "discord" in schemas

    def test_each_item_has_required_keys(self):
        plugins = list_plugins()
        for p in plugins:
            assert "schema" in p
            assert "service_name" in p
            assert "category" in p


class TestGetPluginDetail:
    def test_returns_detail_for_discord(self):
        detail = get_plugin_detail("discord")
        assert detail is not None
        assert detail["schema"] == "discord"
        assert detail["service_name"] == "Discord"

    def test_discord_has_required_tokens(self):
        detail = get_plugin_detail("discord")
        assert "webhook_id" in detail["tokens"]
        assert detail["tokens"]["webhook_id"]["required"] is True
        assert detail["tokens"]["webhook_id"]["private"] is True

    def test_schema_token_excluded(self):
        detail = get_plugin_detail("discord")
        assert "schema" not in detail["tokens"]

    def test_unknown_schema_returns_none(self):
        assert get_plugin_detail("notaschema") is None

    def test_slack_has_two_variants(self):
        detail = get_plugin_detail("slack")
        assert len(detail["variants"]) == 2

    def test_discord_has_no_variants(self):
        detail = get_plugin_detail("discord")
        assert detail["variants"] == []


class TestAssembleUrl:
    def test_discord_assembles_correctly(self):
        url = assemble_url("discord", {"webhook_id": "abc123", "webhook_token": "xyz789"})
        assert url.startswith("discord://abc123/xyz789")

    def test_assembled_url_is_valid_apprise_url(self):
        import apprise
        url = assemble_url("discord", {"webhook_id": "abc123", "webhook_token": "xyz789"})
        ap = apprise.Apprise()
        assert ap.add(url)

    def test_missing_required_token_raises_value_error(self):
        with pytest.raises(ValueError, match="required"):
            assemble_url("discord", {"webhook_id": "abc123"})

    def test_unknown_schema_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown"):
            assemble_url("notaschema", {})

    def test_slack_legacy_variant(self):
        url = assemble_url(
            "slack",
            {"token_a": "T111", "token_b": "B222", "token_c": "C333"},
            variant_index=0,
        )
        assert url.startswith("slack://")
        assert "T111" in url

    def test_slack_bot_token_variant(self):
        url = assemble_url(
            "slack",
            {"access_token": "xoxb-abc"},
            variant_index=1,
        )
        assert url.startswith("slack://")
        assert "xoxb-abc" in url
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
cd /home/exedev/watcher/.worktrees/75-dynamic-apprise-form
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/core/notifications/test_apprise_builder.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError` or `ImportError` — module doesn't exist yet.

- [ ] **Step 3: Implement `apprise_builder.py`**

```python
# src/core/notifications/apprise_builder.py
"""Apprise plugin catalog introspection and URL assembly."""

import re
from functools import lru_cache
from typing import Any

import apprise

from src.core.logging import get_logger

logger = get_logger(__name__)


def _get_scheme(plugin_entry: dict) -> str | None:
    """Return the primary URL scheme for a plugin entry."""
    for key in ("secure_protocols", "protocols"):
        protos = plugin_entry.get(key)
        if protos:
            return protos[0]
    return None


@lru_cache(maxsize=1)
def _build_catalog() -> dict[str, dict]:
    """Build a dict mapping scheme → plugin entry. Cached after first call."""
    details = apprise.Apprise().details()
    catalog: dict[str, dict] = {}
    for entry in details.get("schemas", []):
        scheme = _get_scheme(entry)
        if scheme and scheme not in catalog:
            catalog[scheme] = entry
    return catalog


def _extract_path_tokens(template: str) -> set[str]:
    """Return token names referenced in a URL template, excluding 'schema'."""
    return set(re.findall(r"\{(\w+)\}", template)) - {"schema"}


def _detect_variants(plugin_entry: dict) -> list[dict]:
    """
    Group templates by their required-token set.
    Returns [] if only one group (no variant selector needed).
    """
    tokens = plugin_entry["details"]["tokens"]
    required_set = {k for k, v in tokens.items() if v.get("required") and k != "schema"}
    templates = plugin_entry["details"]["templates"]

    groups: dict[frozenset, list[int]] = {}
    for i, template in enumerate(templates):
        path_tokens = _extract_path_tokens(template)
        used_required = frozenset(path_tokens & required_set)
        groups.setdefault(used_required, []).append(i)

    if len(groups) <= 1:
        return []

    variants = []
    service_name = plugin_entry["service_name"]
    for required_names, _indices in groups.items():
        if required_names:
            label = f"{service_name} ({', '.join(sorted(required_names))})"
        else:
            label = f"{service_name} (no required tokens)"
        variants.append({
            "label": label,
            "required_token_names": sorted(required_names),
        })
    return variants


def _build_token_meta(tokens_dict: dict) -> dict[str, dict]:
    """Convert raw Apprise token defs to clean dicts, excluding the 'schema' token."""
    result = {}
    for name, raw in tokens_dict.items():
        if name == "schema" or "alias_of" in raw:
            continue
        values = raw.get("values")
        if values and not isinstance(values, list):
            # frozenset or other non-list — convert
            try:
                values = sorted(values)
            except TypeError:
                values = list(values)
        regex_val = raw.get("regex")
        regex_str = regex_val[0] if isinstance(regex_val, (list, tuple)) else regex_val
        result[name] = {
            "name": raw.get("name", name),
            "type": raw.get("type", "string"),
            "required": bool(raw.get("required", False)),
            "private": bool(raw.get("private", False)),
            "default": raw.get("default"),
            "values": values if isinstance(values, list) else None,
            "regex": regex_str,
        }
    return result


def list_plugins() -> list[dict]:
    """Return sorted list of {schema, service_name, category} for all plugins."""
    catalog = _build_catalog()
    items = [
        {
            "schema": scheme,
            "service_name": entry["service_name"],
            "category": entry.get("category"),
        }
        for scheme, entry in catalog.items()
    ]
    return sorted(items, key=lambda x: x["service_name"].lower())


def get_plugin_detail(schema: str) -> dict | None:
    """
    Return token defs and variant info for a plugin, or None if unknown.

    Returns: {schema, service_name, tokens: {name: TokenMeta}, variants: [...]}
    """
    catalog = _build_catalog()
    entry = catalog.get(schema.lower())
    if not entry:
        return None
    tokens = _build_token_meta(entry["details"]["tokens"])
    variants = _detect_variants(entry)
    return {
        "schema": schema.lower(),
        "service_name": entry["service_name"],
        "tokens": tokens,
        "variants": variants,
    }


def assemble_url(
    schema: str,
    tokens: dict[str, str],
    variant_index: int | None = None,
) -> str:
    """
    Assemble a valid Apprise URL from a plugin schema and token values.

    Raises ValueError if schema is unknown, required tokens are missing,
    or the assembled URL fails Apprise validation.
    """
    catalog = _build_catalog()
    entry = catalog.get(schema.lower())
    if not entry:
        raise ValueError(f"Unknown Apprise plugin schema: {schema!r}")

    scheme = _get_scheme(entry)
    all_tokens = entry["details"]["tokens"]
    required_set = {k for k, v in all_tokens.items() if v.get("required") and k != "schema"}
    templates = list(entry["details"]["templates"])

    # Filter to variant templates if requested
    if variant_index is not None:
        variants = _detect_variants(entry)
        if variants and 0 <= variant_index < len(variants):
            variant_required = set(variants[variant_index]["required_token_names"])
            groups: dict[frozenset, list[int]] = {}
            for i, t in enumerate(templates):
                path_tokens = _extract_path_tokens(t)
                used_required = frozenset(path_tokens & required_set)
                groups.setdefault(used_required, []).append(i)
            variant_indices = list(groups.values())[variant_index]
            templates = [templates[i] for i in variant_indices]

    for template in templates:
        path_tokens = _extract_path_tokens(template)
        needed_required = path_tokens & required_set
        if not needed_required.issubset(tokens.keys()):
            continue
        # Substitute schema value
        url = template.replace("{schema}", scheme or schema)
        # Substitute provided tokens
        for name, value in tokens.items():
            url = url.replace("{" + name + "}", str(value))
        # Strip remaining unfilled optional tokens (path segments)
        url = re.sub(r"/\{[^}]+\}", "", url)
        url = re.sub(r"\{[^}]+\}", "", url)
        # Validate
        ap = apprise.Apprise()
        if ap.add(url):
            return url
        logger.debug("assembled url failed apprise validation", extra={"url": url})

    # Report which required tokens are missing
    provided = set(tokens.keys())
    missing = required_set - provided
    if missing:
        raise ValueError(
            f"Missing required tokens for {schema!r}: {sorted(missing)}"
        )
    raise ValueError(f"Could not assemble a valid Apprise URL for schema {schema!r}")
```

- [ ] **Step 4: Run tests, confirm they pass**

```bash
uv run pytest tests/core/notifications/test_apprise_builder.py -v
```

Expected: all 14 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/core/notifications/apprise_builder.py tests/core/notifications/test_apprise_builder.py
git commit -m "#75 feat: add apprise_builder — plugin catalog and URL assembly"
```

---

## Task 2: Apprise plugins API endpoints

**Files:**
- Create: `src/api/schemas/apprise_plugin.py`
- Create: `src/api/routes/apprise_plugins.py`
- Modify: `src/api/main.py` (2 lines)
- Create: `tests/api/test_apprise_plugins.py`

- [ ] **Step 1: Write failing tests**

These tests hit the live API; no DB needed (no `pytest.mark.integration`). Use the standard `client` fixture.

```python
# tests/api/test_apprise_plugins.py
"""Tests for GET /api/v1/apprise/plugins endpoints."""


class TestListApprisePlugins:
    async def test_returns_200(self, client):
        resp = await client.get("/api/v1/apprise/plugins")
        assert resp.status_code == 200

    async def test_returns_list(self, client):
        data = (await client.get("/api/v1/apprise/plugins")).json()
        assert isinstance(data, list)
        assert len(data) > 100

    async def test_sorted_by_service_name(self, client):
        data = (await client.get("/api/v1/apprise/plugins")).json()
        names = [p["service_name"] for p in data]
        assert names == sorted(names, key=str.lower)

    async def test_contains_discord(self, client):
        data = (await client.get("/api/v1/apprise/plugins")).json()
        schemas = [p["schema"] for p in data]
        assert "discord" in schemas

    async def test_item_shape(self, client):
        data = (await client.get("/api/v1/apprise/plugins")).json()
        item = next(p for p in data if p["schema"] == "discord")
        assert set(item.keys()) >= {"schema", "service_name", "category"}


class TestGetApprisePlugin:
    async def test_discord_returns_200(self, client):
        resp = await client.get("/api/v1/apprise/plugins/discord")
        assert resp.status_code == 200

    async def test_discord_response_shape(self, client):
        data = (await client.get("/api/v1/apprise/plugins/discord")).json()
        assert data["schema"] == "discord"
        assert data["service_name"] == "Discord"
        assert "tokens" in data
        assert "variants" in data

    async def test_discord_has_webhook_id_token(self, client):
        data = (await client.get("/api/v1/apprise/plugins/discord")).json()
        assert "webhook_id" in data["tokens"]
        assert data["tokens"]["webhook_id"]["required"] is True
        assert data["tokens"]["webhook_id"]["private"] is True

    async def test_discord_no_schema_token(self, client):
        data = (await client.get("/api/v1/apprise/plugins/discord")).json()
        assert "schema" not in data["tokens"]

    async def test_unknown_schema_returns_404(self, client):
        resp = await client.get("/api/v1/apprise/plugins/notaschema")
        assert resp.status_code == 404

    async def test_slack_has_two_variants(self, client):
        data = (await client.get("/api/v1/apprise/plugins/slack")).json()
        assert len(data["variants"]) == 2

    async def test_discord_has_no_variants(self, client):
        data = (await client.get("/api/v1/apprise/plugins/discord")).json()
        assert data["variants"] == []
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
uv run pytest tests/api/test_apprise_plugins.py -v 2>&1 | head -20
```

Expected: 404 errors — route not mounted yet.

- [ ] **Step 3: Write Pydantic schemas**

```python
# src/api/schemas/apprise_plugin.py
"""Pydantic response schemas for the Apprise plugin catalog endpoints."""

from typing import Any

from pydantic import BaseModel


class TokenMeta(BaseModel):
    """Metadata for a single Apprise plugin token."""

    name: str
    type: str
    required: bool
    private: bool
    default: Any = None
    values: list[str] | None = None
    regex: str | None = None


class PluginVariant(BaseModel):
    """A template variant for plugins with divergent required-token sets."""

    label: str
    required_token_names: list[str]


class PluginListItem(BaseModel):
    """Summary item for the plugin list endpoint."""

    schema: str
    service_name: str
    category: str | None = None


class PluginDetail(BaseModel):
    """Full plugin detail including token definitions and variants."""

    schema: str
    service_name: str
    tokens: dict[str, TokenMeta]
    variants: list[PluginVariant]
```

- [ ] **Step 4: Write the route**

```python
# src/api/routes/apprise_plugins.py
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
        schema=detail["schema"],
        service_name=detail["service_name"],
        tokens={k: TokenMeta(**v) for k, v in detail["tokens"].items()},
        variants=[PluginVariant(**v) for v in detail["variants"]],
    )
```

- [ ] **Step 5: Mount the router in `main.py`**

In `src/api/main.py`, add after the existing imports:

```python
from src.api.routes.apprise_plugins import router as apprise_plugins_router
```

And after `v1_router.include_router(probe_router)`:

```python
v1_router.include_router(apprise_plugins_router)
```

- [ ] **Step 6: Run tests, confirm they pass**

```bash
uv run pytest tests/api/test_apprise_plugins.py -v
```

Expected: all 11 tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/api/schemas/apprise_plugin.py src/api/routes/apprise_plugins.py src/api/main.py tests/api/test_apprise_plugins.py
git commit -m "#75 feat: add /api/v1/apprise/plugins endpoints"
```

---

## Task 3: Extend `NotificationConfigCreate` to accept `{schema, tokens}`

**Files:**
- Modify: `src/api/schemas/notification_config.py`
- Modify: `tests/api/test_notification_configs.py` (add new test class; existing tests must continue passing)

The route handler (`notification_configs.py`) does NOT need changes — it already reads `data.apprise_url`, which the schema validator will have resolved before the route sees it.

- [ ] **Step 1: Write failing tests**

Add to `tests/api/test_notification_configs.py` (these are integration tests, inside the existing `pytestmark = pytest.mark.integration` module):

```python
class TestCreateNotificationConfigFromTokens:
    async def test_create_discord_from_tokens(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={
                "schema": "discord",
                "tokens": {"webhook_id": "abc123", "webhook_token": "xyz789"},
                "events": ["change_detected"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["channel_hint"] == "discord"
        assert "apprise_url" not in data

    async def test_missing_required_token_returns_422(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={
                "schema": "discord",
                "tokens": {"webhook_id": "abc123"},  # missing webhook_token
            },
        )
        assert resp.status_code == 422

    async def test_unknown_schema_returns_422(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"schema": "notaschema", "tokens": {}},
        )
        assert resp.status_code == 422

    async def test_neither_url_nor_schema_returns_422(self, client):
        watch_id = await _make_watch(client)
        resp = await client.post(
            f"/api/v1/watches/{watch_id}/notifications",
            json={"events": ["change_detected"]},
        )
        assert resp.status_code == 422
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
uv run pytest tests/api/test_notification_configs.py::TestCreateNotificationConfigFromTokens -v -m integration
```

Expected: 422 (because schema field is not accepted yet) or validation errors.

- [ ] **Step 3: Update `NotificationConfigCreate`**

Replace the current `NotificationConfigCreate` class in `src/api/schemas/notification_config.py` with:

```python
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.notifications.apprise_builder import assemble_url


class NotificationConfigCreate(BaseModel):
    """
    Request body for creating a notification config.

    Accepts either:
    - apprise_url (raw Apprise URL string), or
    - schema + tokens (assembled server-side into an Apprise URL).
    """

    apprise_url: str | None = None
    schema: str | None = None
    tokens: dict[str, str] | None = None
    events: list[str] = Field(default_factory=lambda: ["change_detected"])

    @model_validator(mode="after")
    def resolve_apprise_url(self) -> "NotificationConfigCreate":
        if self.apprise_url is not None:
            # Raw URL path — validate it
            validate_apprise_url(self.apprise_url)
        elif self.schema is not None:
            # Token path — assemble the URL
            try:
                self.apprise_url = assemble_url(self.schema, self.tokens or {})
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        else:
            raise ValueError(
                "Provide either 'apprise_url' or 'schema' + 'tokens'."
            )
        return self

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str]) -> list[str]:
        invalid = [e for e in v if e not in _VALID_EVENT_TYPES]
        if invalid:
            raise ValueError(
                f"Unknown event type(s): {invalid}. Valid types: {sorted(_VALID_EVENT_TYPES)}"
            )
        return v
```

Note: add `from pydantic import model_validator` to the imports, and add `from src.core.notifications.apprise_builder import assemble_url`.

- [ ] **Step 4: Run the new tests + all existing notification tests**

```bash
uv run pytest tests/api/test_notification_configs.py -v -m integration
```

Expected: all existing tests pass, all 4 new tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/api/schemas/notification_config.py tests/api/test_notification_configs.py
git commit -m "#75 feat: extend NotificationConfigCreate to accept schema+tokens"
```

---

## Task 4: Dashboard plugin form partial route + templates

**Files:**
- Modify: `src/dashboard/routes.py` (add one route)
- Create: `src/dashboard/templates/partials/apprise_plugin_form.html`
- Create: `src/dashboard/templates/partials/apprise_raw_url_form.html`
- Create: `tests/dashboard/test_apprise_plugin_form.py`

The route: `GET /partials/apprise-plugin-form?schema=discord[&variant=0]` returns token inputs.
The route also handles `?raw=1` to return the plain URL text input.

- [ ] **Step 1: Write failing tests**

```python
# tests/dashboard/test_apprise_plugin_form.py
"""Unit tests for the apprise plugin form partial route."""

from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient


async def _get(schema: str | None = None, raw: bool = False, variant: int = 0):
    from src.api.dependencies import get_db_session
    from src.api.main import app

    async def override_session():
        yield MagicMock()

    app.dependency_overrides[get_db_session] = override_session
    try:
        params = {}
        if schema:
            params["schema"] = schema
        if raw:
            params["raw"] = "1"
        if variant:
            params["variant"] = str(variant)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/partials/apprise-plugin-form", params=params)
    finally:
        app.dependency_overrides.clear()


class TestApprisePluginFormPartial:
    async def test_discord_returns_200(self):
        resp = await _get(schema="discord")
        assert resp.status_code == 200

    async def test_unknown_schema_returns_404(self):
        resp = await _get(schema="notaschema")
        assert resp.status_code == 404

    async def test_discord_has_hidden_schema_input(self):
        resp = await _get(schema="discord")
        assert 'name="schema"' in resp.text
        assert 'value="discord"' in resp.text

    async def test_discord_webhook_id_is_password_input(self):
        resp = await _get(schema="discord")
        # webhook_id is private=True → type="password"
        assert 'name="tok_webhook_id"' in resp.text
        assert 'type="password"' in resp.text

    async def test_discord_optional_token_in_advanced_section(self):
        resp = await _get(schema="discord")
        # botname is optional — should appear in advanced disclosure
        assert "tok_botname" in resp.text

    async def test_slack_has_variant_selector(self):
        resp = await _get(schema="slack")
        # Slack has 2 variants — variant selector must be present
        assert "variant" in resp.text.lower()

    async def test_raw_mode_returns_url_input(self):
        resp = await _get(raw=True)
        assert resp.status_code == 200
        assert 'name="apprise_url"' in resp.text
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
uv run pytest tests/dashboard/test_apprise_plugin_form.py -v 2>&1 | head -20
```

Expected: 404 — route doesn't exist yet.

- [ ] **Step 3: Add the route to `src/dashboard/routes.py`**

Add to the imports at the top of `src/dashboard/routes.py`:
```python
from src.core.notifications.apprise_builder import get_plugin_detail
```

Add the route (near the other partial routes, after `partial_watch_notifications`):

```python
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
    return templates.TemplateResponse(
        "partials/apprise_plugin_form.html",
        {"request": request, "plugin": detail, "variant": variant},
    )
```

- [ ] **Step 4: Write `apprise_raw_url_form.html`**

```html
{# Raw URL fallback input — swapped in when user clicks "Enter URL manually". #}
{# Provides a plain Apprise URL text field. #}
<div id="plugin-token-form">
  <label for="apprise_url" class="form-label">Apprise URL</label>
  <input
    type="text"
    id="apprise_url"
    name="apprise_url"
    class="form-input mt-1"
    placeholder="slack://T.../A.../T.../#channel"
    required
    aria-required="true">
</div>
```

- [ ] **Step 5: Write `apprise_plugin_form.html`**

Token type → input type mapping:
- `string` + `private=True` → `type="password"` with show/hide toggle button
- `string` → `type="text"`
- `bool` → `<input type="checkbox">`
- `int` → `<input type="number" step="1">`
- `float` → `<input type="number" step="any">`
- `choice:string` / `choice:int` → `<select>` with `values`
- `list:string` → `type="text"` with helper text "Separate multiple values with commas"

Token field name: `tok_{token_name}` (prefixed to avoid collision with other form fields).

```html
{#
  Apprise plugin token form partial.
  Expects: plugin (dict with schema, service_name, tokens, variants), variant (int).
  Injects: hidden schema field + typed token inputs.
  Tokens named tok_{name} to avoid collision with other form fields.
#}

{# Separate required and optional tokens for ordering #}
{% set required_tokens = plugin.tokens.items() | selectattr('1.required') | list %}
{% set optional_tokens = plugin.tokens.items() | rejectattr('1.required') | list %}

<div id="plugin-token-form">
  {# Hidden schema field for POST handler #}
  <input type="hidden" name="schema" value="{{ plugin.schema }}">

  {# Variant selector — only shown when plugin has divergent template variants #}
  {% if plugin.variants | length > 1 %}
  <div class="mb-4">
    <label class="form-label mb-1">Variant</label>
    <div class="flex flex-wrap gap-3" role="radiogroup" aria-label="Select {{ plugin.service_name }} variant">
      {% for v in plugin.variants %}
      <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
        <input
          type="radio"
          name="variant"
          value="{{ loop.index0 }}"
          {% if loop.index0 == variant %}checked{% endif %}
          hx-get="/partials/apprise-plugin-form"
          hx-vals='{"schema": "{{ plugin.schema }}"}'
          hx-include="[name='variant']"
          hx-target="#plugin-token-form"
          hx-swap="outerHTML"
          class="border-gray-300 dark:border-gray-600">
        {{ v.label }}
      </label>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  {# Required tokens #}
  {% for name, tok in required_tokens %}
  <div class="mb-3">
    <label for="tok_{{ name }}" class="form-label">
      {{ tok.name }}
      <span class="text-red-500 dark:text-red-400 ms-0.5" aria-hidden="true">*</span>
    </label>
    {% if tok.type.startswith('choice') %}
    <select id="tok_{{ name }}" name="tok_{{ name }}" class="form-input mt-1" required aria-required="true">
      {% for val in tok.values %}
      <option value="{{ val }}" {% if tok.default == val %}selected{% endif %}>{{ val }}</option>
      {% endfor %}
    </select>
    {% elif tok.type == 'bool' %}
    <input type="checkbox" id="tok_{{ name }}" name="tok_{{ name }}" value="yes"
           {% if tok.default %}checked{% endif %}
           class="rounded border-gray-300 dark:border-gray-600 mt-1">
    {% elif tok.private %}
    <div class="relative mt-1">
      <input type="password" id="tok_{{ name }}" name="tok_{{ name }}"
             class="form-input pe-10"
             required aria-required="true"
             {% if tok.regex %}pattern="{{ tok.regex }}"{% endif %}>
      <button type="button"
              onclick="var i=this.previousElementSibling;i.type=i.type==='password'?'text':'password'"
              class="absolute inset-y-0 end-0 flex items-center px-3 text-gray-400 dark:text-gray-500"
              aria-label="Toggle {{ tok.name }} visibility">
        <span aria-hidden="true">👁</span>
      </button>
    </div>
    {% elif tok.type == 'list:string' %}
    <input type="text" id="tok_{{ name }}" name="tok_{{ name }}"
           class="form-input mt-1"
           required aria-required="true"
           {% if tok.default %}value="{{ tok.default }}"{% endif %}
           {% if tok.regex %}pattern="{{ tok.regex }}"{% endif %}
           aria-describedby="tok_{{ name }}_hint">
    <p id="tok_{{ name }}_hint" class="text-xs text-gray-500 dark:text-gray-400 mt-1">Separate multiple values with commas</p>
    {% else %}
    <input type="{{ 'number' if tok.type in ('int', 'float') else 'text' }}"
           {% if tok.type == 'int' %}step="1"{% elif tok.type == 'float' %}step="any"{% endif %}
           id="tok_{{ name }}" name="tok_{{ name }}"
           class="form-input mt-1"
           required aria-required="true"
           {% if tok.default is not none %}value="{{ tok.default }}"{% endif %}
           {% if tok.regex %}pattern="{{ tok.regex }}"{% endif %}>
    {% endif %}
  </div>
  {% endfor %}

  {# Optional tokens — collapsed under disclosure #}
  {% if optional_tokens %}
  <details class="mt-2">
    <summary class="text-sm text-gray-500 dark:text-gray-400 cursor-pointer select-none hover:text-gray-700 dark:hover:text-gray-300">
      Advanced options
    </summary>
    <div class="mt-3 space-y-3">
      {% for name, tok in optional_tokens %}
      <div>
        <label for="tok_{{ name }}" class="form-label">{{ tok.name }}</label>
        {% if tok.type.startswith('choice') %}
        <select id="tok_{{ name }}" name="tok_{{ name }}" class="form-input mt-1">
          <option value="">— default —</option>
          {% for val in tok.values %}
          <option value="{{ val }}" {% if tok.default == val %}selected{% endif %}>{{ val }}</option>
          {% endfor %}
        </select>
        {% elif tok.type == 'bool' %}
        <input type="checkbox" id="tok_{{ name }}" name="tok_{{ name }}" value="yes"
               {% if tok.default %}checked{% endif %}
               class="rounded border-gray-300 dark:border-gray-600 mt-1">
        {% elif tok.private %}
        <div class="relative mt-1">
          <input type="password" id="tok_{{ name }}" name="tok_{{ name }}"
                 class="form-input pe-10"
                 {% if tok.regex %}pattern="{{ tok.regex }}"{% endif %}>
          <button type="button"
                  onclick="var i=this.previousElementSibling;i.type=i.type==='password'?'text':'password'"
                  class="absolute inset-y-0 end-0 flex items-center px-3 text-gray-400 dark:text-gray-500"
                  aria-label="Toggle {{ tok.name }} visibility">
            <span aria-hidden="true">👁</span>
          </button>
        </div>
        {% elif tok.type == 'list:string' %}
        <input type="text" id="tok_{{ name }}" name="tok_{{ name }}"
               class="form-input mt-1"
               {% if tok.default %}value="{{ tok.default }}"{% endif %}
               {% if tok.regex %}pattern="{{ tok.regex }}"{% endif %}
               aria-describedby="tok_{{ name }}_hint">
        <p id="tok_{{ name }}_hint" class="text-xs text-gray-500 dark:text-gray-400 mt-1">Separate multiple values with commas</p>
        {% else %}
        <input type="{{ 'number' if tok.type in ('int', 'float') else 'text' }}"
               {% if tok.type == 'int' %}step="1"{% elif tok.type == 'float' %}step="any"{% endif %}
               id="tok_{{ name }}" name="tok_{{ name }}"
               class="form-input mt-1"
               {% if tok.default is not none %}value="{{ tok.default }}"{% endif %}
               {% if tok.regex %}pattern="{{ tok.regex }}"{% endif %}>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </details>
  {% endif %}
</div>
```

- [ ] **Step 6: Run tests, confirm they pass**

```bash
uv run pytest tests/dashboard/test_apprise_plugin_form.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/routes.py \
        src/dashboard/templates/partials/apprise_plugin_form.html \
        src/dashboard/templates/partials/apprise_raw_url_form.html \
        tests/dashboard/test_apprise_plugin_form.py
git commit -m "#75 feat: add apprise plugin form partial route and templates"
```

---

## Task 5: Extend dashboard POST handler to accept token form submissions

**Files:**
- Modify: `src/dashboard/routes.py` (`watch_notification_create` function)
- Modify: `tests/dashboard/test_watch_notifications_partial.py` (add test class)

The form now submits `schema=discord` + `tok_webhook_id=...` + `tok_webhook_token=...` instead of `apprise_url=...`. The dashboard handler must detect which mode was submitted and construct the right API payload.

Token field names in form: `tok_{name}` (e.g., `tok_webhook_id`). The handler strips the `tok_` prefix to build the `tokens` dict.

- [ ] **Step 1: Write failing tests**

Add to `tests/dashboard/test_watch_notifications_partial.py`:

```python
class TestWatchNotificationCreateFromTokens:
    """POST /watches/{id}/notifications/new with schema+tokens payload."""

    async def _post_token_form(
        self, watch_id: str, schema: str, token_fields: dict,
        mock_watch=None, events=None,
    ):
        from src.api.dependencies import get_db_session
        from src.api.main import app

        async def override_session():
            yield MagicMock()

        app.dependency_overrides[get_db_session] = override_session
        try:
            with (
                patch(
                    "src.dashboard.routes.get_watch_detail",
                    new_callable=AsyncMock,
                    return_value=mock_watch or _make_mock_watch(),
                ),
                patch(
                    "src.dashboard.routes.get_watch_notifications",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
                patch("src.dashboard.routes.encrypt_apprise_url", return_value="encrypted"),
                patch("src.dashboard.routes.extract_channel_hint", return_value=schema),
                patch("src.dashboard.routes.assemble_url", return_value=f"{schema}://assembled"),
            ):
                form_data = {"schema": schema, **(events or {})}
                form_data.update({f"tok_{k}": v for k, v in token_fields.items()})
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    return await client.post(
                        f"/watches/{watch_id}/notifications/new",
                        data=form_data,
                    )
        finally:
            app.dependency_overrides.clear()

    async def test_token_form_submission_returns_200(self):
        watch = _make_mock_watch()
        resp = await self._post_token_form(
            str(watch.id), "discord",
            {"webhook_id": "abc123", "webhook_token": "xyz789"},
            mock_watch=watch,
        )
        assert resp.status_code == 200

    async def test_unknown_schema_shows_error(self):
        watch = _make_mock_watch()
        with patch(
            "src.dashboard.routes.assemble_url",
            side_effect=ValueError("Unknown Apprise plugin schema"),
        ):
            from src.api.dependencies import get_db_session
            from src.api.main import app

            async def override_session():
                yield MagicMock()

            app.dependency_overrides[get_db_session] = override_session
            try:
                with (
                    patch("src.dashboard.routes.get_watch_detail", new_callable=AsyncMock, return_value=watch),
                    patch("src.dashboard.routes.get_watch_notifications", new_callable=AsyncMock, return_value=[]),
                ):
                    transport = ASGITransport(app=app)
                    async with AsyncClient(transport=transport, base_url="http://test") as client:
                        resp = await client.post(
                            f"/watches/{watch.id}/notifications/new",
                            data={"schema": "notaschema", "tok_x": "y"},
                        )
                assert "Unknown" in resp.text
            finally:
                app.dependency_overrides.clear()
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
uv run pytest tests/dashboard/test_watch_notifications_partial.py::TestWatchNotificationCreateFromTokens -v
```

- [ ] **Step 3: Update `watch_notification_create` in `src/dashboard/routes.py`**

Add `from src.core.notifications.apprise_builder import assemble_url` to the imports.

Replace the `watch_notification_create` function:

```python
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
    schema_val = form.get("schema") or ""

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
                    "error": str(exc),
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
                    "error": str(exc),
                },
            )

    valid_event_values = {e.value for e in WatchEventType}
    invalid = [e for e in events if e not in valid_event_values]
    if invalid:
        notifications = await get_watch_notifications(session, watch.id)
        return templates.TemplateResponse(
            "partials/watch_notifications.html",
            {
                "request": request,
                "watch": watch,
                "notifications": notifications,
                "error": f"Unknown event types: {invalid}",
            },
        )

    config = NotificationConfig(
        watch_id=watch.id,
        apprise_url=encrypt_apprise_url(apprise_url),
        channel_hint=extract_channel_hint(apprise_url),
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
        {"request": request, "watch": watch, "notifications": notifications},
    )
```

Note: the old handler signature used `apprise_url: str = Form(...)`. The new one reads from `request.form()` directly to handle both modes. Remove the old `apprise_url` `Form` parameter.

- [ ] **Step 4: Run new tests + full dashboard test suite**

```bash
uv run pytest tests/dashboard/test_watch_notifications_partial.py -v
```

Expected: all tests pass (including pre-existing ones).

- [ ] **Step 5: Commit**

```bash
git add src/dashboard/routes.py tests/dashboard/test_watch_notifications_partial.py
git commit -m "#75 feat: extend dashboard POST handler to accept plugin token form"
```

---

## Task 6: Update `watch_notifications.html` to two-step add form

**Files:**
- Modify: `src/dashboard/templates/partials/watch_notifications.html`
- Modify: `src/dashboard/routes.py` — pass `apprise_plugins` to notifications partial context

The add form needs:
1. A plugin picker `<select id="channel-picker">` populated with all plugins
2. A `<div id="plugin-token-form">` initially empty, filled by HTMX on selection
3. A "Enter URL manually" button that loads the raw URL partial
4. Events fieldset (unchanged)
5. Submit button

The plugin list is injected via the template context — `partial_watch_notifications` (and `watch_notification_create`) pass `apprise_plugins=list_plugins()`.

- [ ] **Step 1: Update `partial_watch_notifications` to pass plugin list**

In `src/dashboard/routes.py`, update `partial_watch_notifications`:

```python
from src.core.notifications.apprise_builder import get_plugin_detail, list_plugins

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
            "apprise_plugins": list_plugins(),
        },
    )
```

Also pass `apprise_plugins=list_plugins()` in all other places that render `watch_notifications.html` (the error returns in `watch_notification_create`, and the success return). Check `src/dashboard/routes.py` for all `TemplateResponse("partials/watch_notifications.html", ...)` calls and add `"apprise_plugins": list_plugins()` to each context dict.

- [ ] **Step 2: Update `watch_notifications.html` add form section**

Replace lines 74–139 (the add form section, from `{% if error %}` to the end of `</form>`) with:

```html
{# Error display #}
{% if error %}
<div class="text-sm text-red-600 dark:text-red-400 mb-3" role="alert">{{ error }}</div>
{% endif %}

{# Add notification form — two-step: pick plugin → fill token form #}
<form
  id="add-notification-form"
  hx-post="/watches/{{ watch.id }}/notifications/new"
  hx-target="#watch-notifications"
  hx-swap="innerHTML"
  class="mt-4 space-y-4 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
  <h4 class="text-sm font-semibold text-gray-900 dark:text-white">Add notification</h4>

  {# Step 1: Plugin picker #}
  <div>
    <label for="channel-picker" class="form-label">Notification channel</label>
    <div class="flex items-center gap-3 mt-1">
      <select
        id="channel-picker"
        name="_channel_picker"
        class="form-input"
        hx-get="/partials/apprise-plugin-form"
        hx-target="#plugin-token-form"
        hx-swap="outerHTML"
        hx-vals='js:{schema: document.getElementById("channel-picker").value}'
        hx-trigger="change"
        aria-label="Select notification plugin">
        <option value="">— pick a channel —</option>
        {% for plugin in apprise_plugins %}
        <option value="{{ plugin.schema }}">{{ plugin.service_name }}</option>
        {% endfor %}
      </select>
      <button
        type="button"
        hx-get="/partials/apprise-plugin-form?raw=1"
        hx-target="#plugin-token-form"
        hx-swap="outerHTML"
        class="btn btn-secondary text-xs min-h-[44px] shrink-0">
        Enter URL manually
      </button>
    </div>
  </div>

  {# Step 2: Token fields (HTMX target) — empty until plugin selected #}
  <div id="plugin-token-form"></div>

  {# Events (always visible) #}
  <fieldset>
    <legend class="form-label mb-2">Events</legend>
    <div class="flex flex-wrap gap-4">
      <label class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-500 min-h-[44px]" aria-label="Watch Created (not applicable — watch already exists)">
        {% if "watch_created" in (form_events | default([])) %}
        <input type="hidden" name="events" value="watch_created">
        {% endif %}
        <input type="checkbox" name="events" value="watch_created"
               {% if "watch_created" in (form_events | default([])) %}checked{% endif %}
               disabled class="rounded border-gray-300 dark:border-gray-600 opacity-50 cursor-not-allowed">
        Watch Created
      </label>
      <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
        <input type="checkbox" name="events" value="change_detected" checked class="rounded border-gray-300 dark:border-gray-600">
        Change Detected
      </label>
      <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
        <input type="checkbox" name="events" value="watch_error" class="rounded border-gray-300 dark:border-gray-600">
        Watch Error
      </label>
      <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
        <input type="checkbox" name="events" value="watch_recovered" class="rounded border-gray-300 dark:border-gray-600">
        Watch Recovered
      </label>
      <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
        <input type="checkbox" name="events" value="watch_paused" class="rounded border-gray-300 dark:border-gray-600">
        Watch Paused
      </label>
      <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
        <input type="checkbox" name="events" value="watch_resumed" class="rounded border-gray-300 dark:border-gray-600">
        Watch Resumed
      </label>
    </div>
  </fieldset>

  <div class="flex items-center gap-3">
    <button type="submit" class="btn btn-primary min-h-[44px] htmx-request:opacity-60">
      Add
    </button>
  </div>
</form>
```

- [ ] **Step 3: Run full test suite**

```bash
uv run pytest --no-cov -m "not integration" -q
```

Expected: all previously passing tests still pass. Fix any failures before continuing.

- [ ] **Step 4: Manual smoke test**

Start the dev server (already running on port 8001), open `https://watcher.exe.xyz:8001/`, navigate to any watch detail page. Verify:
1. The "Add notification" form shows a plugin picker select with 100+ options
2. Selecting "Discord" loads the token form (two password fields + optional botname)
3. Selecting "Slack" loads a variant selector
4. Clicking "Enter URL manually" loads the raw URL input
5. Submitting a filled Discord form creates a config and shows it in the list

- [ ] **Step 5: Run integration tests**

```bash
uv run pytest -m integration -q
```

Expected: all integration tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/partials/watch_notifications.html
git commit -m "#75 feat: two-step plugin picker + token form in add-notification UI"
```

---

## Task 7: Final cleanup and merge

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest -q
```

Expected: all tests pass (unit + integration).

- [ ] **Step 2: Lint**

```bash
uv run ruff check .
```

Fix any issues.

- [ ] **Step 3: Merge to main and restart service**

```bash
git checkout main
git merge 75-dynamic-apprise-form --no-ff -m "#75 feat: dynamic Apprise URL builder — plugin picker + token form"
sudo systemctl restart watcher
```

- [ ] **Step 4: Verify live service**

```bash
sudo journalctl -u watcher -f --no-pager | head -20
```

Expected: service starts cleanly, no errors.
