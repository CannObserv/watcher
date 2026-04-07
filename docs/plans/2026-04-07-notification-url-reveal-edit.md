# Notification URL Reveal & Edit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users view (show/hide) and edit the Apprise URL for existing notification configs directly from the dashboard.

**Architecture:** Fernet decryption already exists (`decrypt_apprise_url`). The dashboard notification partial routes decrypt each URL into a companion dict passed to the template. The template embeds URLs in `<details>/<summary>` for show/hide with no extra requests. An "Edit" button per config swaps the `<li>` row with an HTMX-loaded edit form; on save, the POST route re-encrypts the new URL and refreshes the notification list. The REST API PATCH endpoint is extended to support `apprise_url` updates for API callers.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Jinja2, HTMX, Tailwind v4, Fernet (cryptography), Apprise, pytest, uv

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/api/schemas/notification_config.py` | Modify | Add `apprise_url` field to `NotificationConfigUpdate` with validation |
| `src/api/routes/notification_configs.py` | Modify | PATCH handler: re-encrypt URL + update `channel_hint` when `apprise_url` provided |
| `src/dashboard/routes.py` | Modify | Decrypt URLs into `notification_urls` dict for all notification partial routes; add GET edit-form + POST edit routes; fix `toggle` route missing `apprise_plugins` |
| `src/dashboard/templates/partials/watch_notifications.html` | Modify | Add `<details>/<summary>` URL reveal + "Edit" button per config row |
| `src/dashboard/templates/partials/notification_edit_form.html` | Create | Edit form `<li>` replacement: URL input + event checkboxes + Save/Cancel |
| `tests/api/test_notification_configs.py` | Modify | Add PATCH tests for `apprise_url` update + invalid URL |
| `tests/dashboard/test_watch_notifications_partial.py` | Modify | Add tests for URL reveal in list + edit form route + edit POST |

---

## Task 1: Extend REST API PATCH to accept `apprise_url`

**Files:**
- Modify: `src/api/schemas/notification_config.py`
- Modify: `src/api/routes/notification_configs.py`
- Test: `tests/api/test_notification_configs.py`

- [ ] **Step 1.1: Write failing integration tests**

Append to `class TestPatchNotificationConfig` in `tests/api/test_notification_configs.py`:

```python
async def test_patch_apprise_url_updates_stored_url(self, client):
    watch_id = await _make_watch(client)
    create_resp = await client.post(
        f"/api/v1/watches/{watch_id}/notifications",
        json={"apprise_url": VALID_URL},
    )
    config_id = create_resp.json()["id"]
    new_url = "json://updated.example.com/notify"
    resp = await client.patch(
        f"/api/v1/watches/{watch_id}/notifications/{config_id}",
        json={"apprise_url": new_url},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "apprise_url" not in data  # never exposed
    assert data["channel_hint"] == "json"  # re-derived from new URL

async def test_patch_invalid_apprise_url_returns_422(self, client):
    watch_id = await _make_watch(client)
    create_resp = await client.post(
        f"/api/v1/watches/{watch_id}/notifications",
        json={"apprise_url": VALID_URL},
    )
    config_id = create_resp.json()["id"]
    resp = await client.patch(
        f"/api/v1/watches/{watch_id}/notifications/{config_id}",
        json={"apprise_url": INVALID_URL},
    )
    assert resp.status_code == 422
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
cd /home/exedev/watcher
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/api/test_notification_configs.py::TestPatchNotificationConfig::test_patch_apprise_url_updates_stored_url tests/api/test_notification_configs.py::TestPatchNotificationConfig::test_patch_invalid_apprise_url_returns_422 -v -m integration
```

Expected: FAIL (PATCH ignores `apprise_url`, response doesn't reflect update)

- [ ] **Step 1.3: Add `apprise_url` to `NotificationConfigUpdate` schema**

In `src/api/schemas/notification_config.py`, update `NotificationConfigUpdate`:

```python
class NotificationConfigUpdate(BaseModel):
    """Request body for PATCH — all fields optional."""

    is_active: bool | None = None
    events: list[str] | None = None
    apprise_url: str | None = None

    @field_validator("events")
    @classmethod
    def validate_events(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return validate_event_list(v)

    @field_validator("apprise_url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return validate_apprise_url(v)
```

- [ ] **Step 1.4: Update PATCH handler to encrypt and store new URL**

In `src/api/routes/notification_configs.py`, add `encrypt_apprise_url` to imports (it's already imported), then update the `update_notification_config` handler:

```python
from src.core.notifications.apprise_builder import get_service_name
# (get_service_name already imported)
```

Update the handler body (after the existing `if data.is_active` / `if data.events` blocks):

```python
    if data.apprise_url is not None:
        nc.apprise_url = encrypt_apprise_url(data.apprise_url)
        nc.channel_hint = extract_channel_hint(data.apprise_url)
```

Also add `extract_channel_hint` to the imports from `src.api.schemas.notification_config`:

```python
from src.api.schemas.notification_config import (
    NotificationConfigCreate,
    NotificationConfigResponse,
    NotificationConfigUpdate,
    extract_channel_hint,
)
```

- [ ] **Step 1.5: Run tests to verify they pass**

```bash
uv run pytest tests/api/test_notification_configs.py::TestPatchNotificationConfig -v -m integration
```

Expected: all PASS

- [ ] **Step 1.6: Run full test suite to check for regressions**

```bash
uv run pytest tests/api/test_notification_configs.py -v -m integration
```

Expected: all PASS

- [ ] **Step 1.7: Commit**

```bash
git add src/api/schemas/notification_config.py src/api/routes/notification_configs.py tests/api/test_notification_configs.py
git commit -m "feat: PATCH notification config accepts apprise_url update"
```

---

## Task 2: Decrypt URLs in dashboard notification routes + Show URL reveal

**Files:**
- Modify: `src/dashboard/routes.py`
- Modify: `src/dashboard/templates/partials/watch_notifications.html`
- Test: `tests/dashboard/test_watch_notifications_partial.py`

- [ ] **Step 2.1: Update `_make_mock_nc` helper to include `apprise_url`**

In `tests/dashboard/test_watch_notifications_partial.py`, update `_make_mock_nc`:

```python
def _make_mock_nc(
    nc_id=None,
    channel_hint="slack",
    events=None,
    is_active=True,
    apprise_url="encrypted_token",
):
    """Build a minimal NotificationConfig-like mock."""
    nc = MagicMock()
    nc.id = nc_id or ULID()
    nc.channel_hint = channel_hint
    nc.events = events if events is not None else ["change_detected"]
    nc.is_active = is_active
    nc.apprise_url = apprise_url
    return nc
```

- [ ] **Step 2.2: Write failing tests for URL reveal in notification list**

Append to `class TestWatchNotificationsPartialRoute` in `tests/dashboard/test_watch_notifications_partial.py`:

```python
async def test_renders_decrypted_url_in_details(self):
    """Notification list reveals decrypted URL via <details> element."""
    watch = _make_mock_watch()
    nc = _make_mock_nc(apprise_url="some_fernet_token")
    with patch("src.dashboard.routes.decrypt_apprise_url", return_value="discord://abc/def/ghi"):
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
    assert resp.status_code == 200
    assert b"discord://abc/def/ghi" in resp.content
    assert b"Show URL" in resp.content

async def test_url_reveal_uses_details_element(self):
    """Show URL toggle uses native <details> element for accessibility."""
    watch = _make_mock_watch()
    nc = _make_mock_nc()
    with patch("src.dashboard.routes.decrypt_apprise_url", return_value="slack://T/A/T"):
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
    assert b"<details" in resp.content
    assert b"Show URL" in resp.content
```

- [ ] **Step 2.3: Run tests to verify they fail**

```bash
uv run pytest tests/dashboard/test_watch_notifications_partial.py::TestWatchNotificationsPartialRoute::test_renders_decrypted_url_in_details tests/dashboard/test_watch_notifications_partial.py::TestWatchNotificationsPartialRoute::test_url_reveal_uses_details_element -v
```

Expected: FAIL

- [ ] **Step 2.4: Decrypt URLs in all notification partial routes in `routes.py`**

Add `decrypt_apprise_url` to the imports at the top of `src/dashboard/routes.py`:

```python
from src.core.crypto import decrypt_apprise_url, encrypt_apprise_url
```

Create a helper function near the top of the notification-related route section (around line 1328):

```python
def _decrypt_notification_urls(notifications: list) -> dict[str, str]:
    """Decrypt Apprise URLs for a list of NotificationConfig objects.

    Returns a mapping from config ID (str) to plaintext URL.
    Silently stores an error placeholder if decryption fails.
    """
    result = {}
    for nc in notifications:
        try:
            result[str(nc.id)] = decrypt_apprise_url(nc.apprise_url)
        except Exception:
            result[str(nc.id)] = "(decryption error)"
    return result
```

Update `partial_watch_notifications` route to decrypt and pass URLs:

```python
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
```

Also update `watch_notification_toggle` (around line 1486) to include `notification_urls` and `apprise_plugins` (fixing the pre-existing missing `apprise_plugins` bug):

```python
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
```

In `watch_notification_create` (around line 1386), there are exactly **4** `TemplateResponse` calls that render `watch_notifications.html`. Add `"notification_urls": _decrypt_notification_urls(notifications),` to each. The four locations are:

1. **Token assembly error branch** (around line 1415): the response returning after `assemble_url` raises `ValueError`
2. **Raw URL validation error branch** (around line 1432): the response returning after `validate_apprise_url` raises `ValueError`
3. **Events validation error branch** (around line 1447): the response returning after `validate_event_list` raises `ValueError`
4. **Success response** (around line 1475): the response returning after `await session.commit()`

Each block already has `notifications = await get_watch_notifications(session, watch.id)` just before the `TemplateResponse`; add `"notification_urls": _decrypt_notification_urls(notifications),` to the context dict in all four.

Also update the watch detail page route. In `watch_detail_page` (starting at line 188), the context dict is built at lines 243–263. Add `notification_urls` on the line after `"notifications": notifications,` (line 248):

```python
    context = {
        "request": request,
        "active_page": "watches",
        "watch": watch,
        "profiles": profiles,
        "notifications": notifications,
        "notification_urls": _decrypt_notification_urls(notifications),  # ← add this line
        "field_contexts": field_contexts,
        ...
    }
```

- [ ] **Step 2.5: Update `watch_notifications.html` to add URL reveal**

In `src/dashboard/templates/partials/watch_notifications.html`, replace the left content div (the channel hint + chips block) with:

```html
      {# Left: channel hint + event chips + inactive badge + URL reveal #}
      <div class="flex flex-wrap items-center gap-3 min-w-0">
        <span class="font-medium text-gray-900 dark:text-white truncate">{{ nc.channel_hint }}</span>
        <span class="flex flex-wrap gap-1" aria-label="Subscribed events">
          {% for evt in nc.events %}
          <span class="badge badge-info">{{ event_titles.get(evt, evt) }}</span>
          {% endfor %}
        </span>
        {% if not nc.is_active %}
        <span class="badge badge-inactive shrink-0">Inactive</span>
        {% endif %}
        {% if notification_urls is defined and nc.id|string in notification_urls %}
        <details class="w-full mt-1">
          <summary class="text-xs text-gray-500 dark:text-gray-400 cursor-pointer select-none hover:text-gray-700 dark:hover:text-gray-300 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-co-purple-600 rounded">
            Show URL
          </summary>
          <code class="block mt-1 text-xs font-mono break-all text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 p-2 rounded">{{ notification_urls[nc.id|string] }}</code>
        </details>
        {% endif %}
      </div>
```

- [ ] **Step 2.6: Run tests to verify they pass**

```bash
uv run pytest tests/dashboard/test_watch_notifications_partial.py::TestWatchNotificationsPartialRoute -v
```

Expected: all PASS

- [ ] **Step 2.7: Run full dashboard test suite**

```bash
uv run pytest tests/dashboard/ -v
```

Expected: all PASS

- [ ] **Step 2.8: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/partials/watch_notifications.html tests/dashboard/test_watch_notifications_partial.py
git commit -m "feat: decrypt and reveal Apprise URLs in notification list"
```

---

## Task 3: Edit notification form — GET route + template

**Files:**
- Create: `src/dashboard/templates/partials/notification_edit_form.html`
- Modify: `src/dashboard/routes.py`
- Test: `tests/dashboard/test_watch_notifications_partial.py`

- [ ] **Step 3.1: Write a GET helper in the test file**

Add `_get_dashboard` as a module-level async helper function in `tests/dashboard/test_watch_notifications_partial.py`. Place it right after the existing `_post_dashboard` function (around line 204), before `class TestWatchNotificationToggleRoute`. This follows the same session-mocking pattern as `_post_dashboard`:

```python
async def _get_dashboard(
    path: str, mock_watch=None, mock_session=None
):
    """Make an authenticated GET to a dashboard route with mocked dependencies.

    Patches get_watch_detail with mock_watch and injects mock_session via
    the DB dependency override (session.get, session.commit etc. are all set
    on the mock before calling this helper).
    """
    from src.api.dependencies import get_db_session
    from src.api.main import app

    _session = mock_session or MagicMock()

    async def override_session():
        yield _session

    app.dependency_overrides[get_db_session] = override_session
    try:
        with patch(
            "src.dashboard.routes.get_watch_detail",
            new_callable=AsyncMock,
            return_value=mock_watch,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get(path)
    finally:
        app.dependency_overrides.clear()
```

The `_session` mock works because the dependency override yields it to the route handler's `session` parameter; callers set `session.get = AsyncMock(return_value=nc)` etc. on the mock before passing it in.

- [ ] **Step 3.2: Write failing tests for the GET edit-form route**

Append to `tests/dashboard/test_watch_notifications_partial.py`:

```python
class TestNotificationEditFormRoute:
    """GET /watches/{watch_id}/notifications/{config_id}/edit-form"""

    async def test_returns_200_with_decrypted_url(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc(apprise_url="encrypted_blob")
        nc.watch_id = watch.id
        session = MagicMock()
        session.get = AsyncMock(return_value=nc)
        with patch("src.dashboard.routes.decrypt_apprise_url", return_value="discord://T/A/T"):
            resp = await _get_dashboard(
                f"/watches/{watch.id}/notifications/{nc.id}/edit-form",
                mock_watch=watch,
                mock_session=session,
            )
        assert resp.status_code == 200
        assert b"discord://T/A/T" in resp.content

    async def test_returns_form_with_events_checked(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc(events=["change_detected", "watch_error"])
        nc.watch_id = watch.id
        session = MagicMock()
        session.get = AsyncMock(return_value=nc)
        with patch("src.dashboard.routes.decrypt_apprise_url", return_value="json://x.com"):
            resp = await _get_dashboard(
                f"/watches/{watch.id}/notifications/{nc.id}/edit-form",
                mock_watch=watch,
                mock_session=session,
            )
        assert resp.status_code == 200
        assert b"watch_error" in resp.content

    async def test_returns_404_for_missing_watch(self):
        resp = await _get_dashboard(
            "/watches/01ZZZZZZZZZZZZZZZZZZZZZZZZ/notifications/01ZZZZZZZZZZZZZZZZZZZZZZZZ/edit-form",
            mock_watch=None,
        )
        assert resp.status_code == 404

    async def test_returns_404_for_config_not_belonging_to_watch(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc()
        nc.watch_id = MagicMock()  # different — won't equal watch.id
        session = MagicMock()
        session.get = AsyncMock(return_value=nc)
        resp = await _get_dashboard(
            f"/watches/{watch.id}/notifications/{nc.id}/edit-form",
            mock_watch=watch,
            mock_session=session,
        )
        assert resp.status_code == 404
```

- [ ] **Step 3.3: Run tests to verify they fail**

```bash
uv run pytest tests/dashboard/test_watch_notifications_partial.py::TestNotificationEditFormRoute -v
```

Expected: FAIL (route doesn't exist)

- [ ] **Step 3.4: Create `notification_edit_form.html` template**

Create `src/dashboard/templates/partials/notification_edit_form.html`:

```html
{#
  Edit form for an existing notification config.
  Replaces the <li> row for the config being edited.
  Expects: watch, nc (NotificationConfig), decrypted_url (str), event_titles (global).
#}
<li id="nc-{{ nc.id }}"
    class="stat-card text-sm">
  <form
    hx-post="/watches/{{ watch.id }}/notifications/{{ nc.id }}/edit"
    hx-target="#watch-notifications-list"
    hx-swap="outerHTML"
    class="space-y-4">

    <h4 class="text-sm font-semibold text-gray-900 dark:text-white">
      Edit {{ nc.channel_hint }} notification
    </h4>

    {# Apprise URL input #}
    <div>
      <label for="edit-url-{{ nc.id }}" class="form-label">
        Apprise URL
      </label>
      <input
        type="text"
        id="edit-url-{{ nc.id }}"
        name="apprise_url"
        value="{{ decrypted_url }}"
        class="form-input mt-1"
        required
        aria-required="true">
    </div>

    {# Event checkboxes — mirrors the add form #}
    <fieldset>
      <legend class="form-label mb-2">Events</legend>
      <div class="flex flex-wrap gap-4">
        <label class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-500 min-h-[44px]"
               aria-label="Watch Created (not applicable — watch already exists)">
          {% if "watch_created" in nc.events %}
          <input type="hidden" name="events" value="watch_created">
          {% endif %}
          <input type="checkbox" name="events" value="watch_created"
                 {% if "watch_created" in nc.events %}checked{% endif %}
                 disabled
                 class="rounded border-gray-300 dark:border-gray-600 opacity-50 cursor-not-allowed">
          Watch Created
        </label>
        <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
          <input type="checkbox" name="events" value="change_detected"
                 {% if "change_detected" in nc.events %}checked{% endif %}
                 class="rounded border-gray-300 dark:border-gray-600">
          Change Detected
        </label>
        <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
          <input type="checkbox" name="events" value="watch_error"
                 {% if "watch_error" in nc.events %}checked{% endif %}
                 class="rounded border-gray-300 dark:border-gray-600">
          Watch Error
        </label>
        <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
          <input type="checkbox" name="events" value="watch_recovered"
                 {% if "watch_recovered" in nc.events %}checked{% endif %}
                 class="rounded border-gray-300 dark:border-gray-600">
          Watch Recovered
        </label>
        <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
          <input type="checkbox" name="events" value="watch_paused"
                 {% if "watch_paused" in nc.events %}checked{% endif %}
                 class="rounded border-gray-300 dark:border-gray-600">
          Watch Paused
        </label>
        <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
          <input type="checkbox" name="events" value="watch_resumed"
                 {% if "watch_resumed" in nc.events %}checked{% endif %}
                 class="rounded border-gray-300 dark:border-gray-600">
          Watch Resumed
        </label>
      </div>
    </fieldset>

    {# Error display #}
    {% if error %}
    <div class="text-sm text-red-600 dark:text-red-400" role="alert">{{ error }}</div>
    {% endif %}

    <div class="flex items-center gap-2">
      <button type="submit"
              class="btn btn-primary text-xs min-h-[44px] htmx-request:opacity-60">
        Save
      </button>
      <button type="button"
              hx-get="/partials/watch-notifications/{{ watch.id }}"
              hx-target="#watch-notifications-list"
              hx-swap="outerHTML"
              class="btn btn-secondary text-xs min-h-[44px]">
        Cancel
      </button>
    </div>
  </form>
</li>
```

- [ ] **Step 3.5: Add the GET edit-form route to `src/dashboard/routes.py`**

Insert after the `watch_notification_toggle` route (around line 1508):

```python
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
```

- [ ] **Step 3.6: Run tests to verify they pass**

```bash
uv run pytest tests/dashboard/test_watch_notifications_partial.py::TestNotificationEditFormRoute -v
```

Expected: all PASS

- [ ] **Step 3.7: Commit**

```bash
git add src/dashboard/routes.py src/dashboard/templates/partials/notification_edit_form.html tests/dashboard/test_watch_notifications_partial.py
git commit -m "feat: add notification edit form route and template"
```

---

## Task 4: Edit POST route — save updated notification config

**Files:**
- Modify: `src/dashboard/routes.py`
- Test: `tests/dashboard/test_watch_notifications_partial.py`

- [ ] **Step 4.1: Write failing tests for the POST edit route**

Append to `tests/dashboard/test_watch_notifications_partial.py`:

```python
class TestNotificationEditRoute:
    """POST /watches/{watch_id}/notifications/{config_id}/edit"""

    async def test_valid_url_saves_and_returns_list(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc()
        nc.watch_id = watch.id
        session = MagicMock()
        session.get = AsyncMock(return_value=nc)
        session.commit = AsyncMock()
        with patch("src.dashboard.routes.encrypt_apprise_url", return_value="new_encrypted"):
            resp = await _post_dashboard(
                f"/watches/{watch.id}/notifications/{nc.id}/edit",
                form_data={
                    "apprise_url": "json://hooks.example.com/notify",
                    "events": "change_detected",
                },
                mock_watch=watch,
                mock_session=session,
            )
        assert resp.status_code == 200
        assert b"watch-notifications-list" in resp.content
        assert nc.apprise_url == "new_encrypted"

    async def test_updates_events(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc(events=["change_detected"])
        nc.watch_id = watch.id
        session = MagicMock()
        session.get = AsyncMock(return_value=nc)
        session.commit = AsyncMock()
        with patch("src.dashboard.routes.encrypt_apprise_url", return_value="enc"):
            resp = await _post_dashboard(
                f"/watches/{watch.id}/notifications/{nc.id}/edit",
                form_data={
                    "apprise_url": "json://hooks.example.com",
                    "events": ["change_detected", "watch_error"],
                },
                mock_watch=watch,
                mock_session=session,
            )
        assert resp.status_code == 200
        assert nc.events == ["change_detected", "watch_error"]

    async def test_invalid_url_returns_edit_form_with_error(self):
        watch = _make_mock_watch()
        nc = _make_mock_nc()
        nc.watch_id = watch.id
        session = MagicMock()
        session.get = AsyncMock(return_value=nc)
        with patch("src.dashboard.routes.decrypt_apprise_url", return_value="json://old.com"):
            resp = await _post_dashboard(
                f"/watches/{watch.id}/notifications/{nc.id}/edit",
                form_data={
                    "apprise_url": "notascheme://bad",
                    "events": "change_detected",
                },
                mock_watch=watch,
                mock_session=session,
            )
        assert resp.status_code == 200
        # Returns the edit form again with an error message
        assert b"edit-url-" in resp.content or b"Apprise URL" in resp.content

    async def test_missing_watch_returns_404(self):
        resp = await _post_dashboard(
            "/watches/01ZZZZZZZZZZZZZZZZZZZZZZZZ/notifications/01ZZZZZZZZZZZZZZZZZZZZZZZZ/edit",
            form_data={"apprise_url": "json://x.com", "events": "change_detected"},
            mock_watch=None,
        )
        assert resp.status_code == 404
```

- [ ] **Step 4.2: Run tests to verify they fail**

```bash
uv run pytest tests/dashboard/test_watch_notifications_partial.py::TestNotificationEditRoute -v
```

Expected: FAIL (route doesn't exist)

- [ ] **Step 4.3: Add the POST edit route to `src/dashboard/routes.py`**

Insert after the GET edit-form route added in Task 3:

```python
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
```

- [ ] **Step 4.4: Run tests to verify they pass**

```bash
uv run pytest tests/dashboard/test_watch_notifications_partial.py::TestNotificationEditRoute -v
```

Expected: all PASS

- [ ] **Step 4.5: Commit**

```bash
git add src/dashboard/routes.py tests/dashboard/test_watch_notifications_partial.py
git commit -m "feat: add notification edit POST route"
```

---

## Task 5: Wire "Edit" button into the notification list template

**Files:**
- Modify: `src/dashboard/templates/partials/watch_notifications.html`
- Test: `tests/dashboard/test_watch_notifications_partial.py`

- [ ] **Step 5.1: Write failing test for the Edit button**

Append to `class TestWatchNotificationsPartialRoute`:

```python
async def test_edit_button_present_for_each_config(self):
    """Edit button appears per notification config, targeting the config row."""
    watch = _make_mock_watch()
    nc = _make_mock_nc()
    with patch("src.dashboard.routes.decrypt_apprise_url", return_value="json://x.com"):
        resp = await self._get(str(watch.id), mock_watch=watch, mock_notifications=[nc])
    assert resp.status_code == 200
    assert b"edit-form" in resp.content  # hx-get URL includes "edit-form"
    assert b"Edit" in resp.content
```

- [ ] **Step 5.2: Run test to verify it fails**

```bash
uv run pytest "tests/dashboard/test_watch_notifications_partial.py::TestWatchNotificationsPartialRoute::test_edit_button_present_for_each_config" -v
```

Expected: FAIL

- [ ] **Step 5.3: Add "Edit" button to the notification list template**

In `src/dashboard/templates/partials/watch_notifications.html`, add the Edit button inside the `{# Right: action buttons #}` div, before the Toggle button:

```html
      {# Right: action buttons #}
      <div class="flex items-center gap-2 shrink-0">
        {# Edit URL and events #}
        <button
          hx-get="/watches/{{ watch.id }}/notifications/{{ nc.id }}/edit-form"
          hx-target="#nc-{{ nc.id }}"
          hx-swap="outerHTML"
          class="btn btn-secondary text-xs min-h-[44px] htmx-request:opacity-60"
          aria-label="Edit {{ nc.channel_hint }} notification URL and events">
          Edit
        </button>

        {# Toggle active / inactive #}
        ...
```

- [ ] **Step 5.4: Run tests to verify they pass**

```bash
uv run pytest tests/dashboard/test_watch_notifications_partial.py -v
```

Expected: all PASS

- [ ] **Step 5.5: Run full test suite**

```bash
uv run pytest -v
```

Expected: all PASS (or only pre-existing integration test failures if DB unavailable)

- [ ] **Step 5.6: Commit**

```bash
git add src/dashboard/templates/partials/watch_notifications.html tests/dashboard/test_watch_notifications_partial.py
git commit -m "feat: add Edit button to notification config rows"
```

---

## Task 6: Final verification and service restart

- [ ] **Step 6.1: Run full test suite including integration tests**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest -v -m integration
uv run pytest -v -m "not integration"
```

Expected: all PASS

- [ ] **Step 6.2: Lint check**

```bash
uv run ruff check .
```

Expected: no errors

- [ ] **Step 6.3: Merge to main and restart service**

```bash
sudo systemctl restart watcher
sudo journalctl -u watcher -f --no-pager -n 20
```

Expected: service starts clean, no import errors

- [ ] **Step 6.4: Manual smoke test**

- Open the dashboard, navigate to any watch with notifications
- Confirm "Show URL" details element appears per config
- Click "Show URL" — verify the plaintext URL is visible
- Click "Edit" — verify the edit form loads with the URL pre-filled
- Change the URL to a different valid Apprise URL, click Save
- Verify the notification list refreshes and `channel_hint` updated if scheme changed
- Verify Cancel re-loads the list without saving
