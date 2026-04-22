# Notification Inline-to-Page Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace inline table-row add/edit forms for notification configs with dedicated full pages across three contexts (template library, watch local configs, domain defaults), eliminating the sticky-header overlap and buried Save/Cancel UX problems.

**Architecture:** Each context gets a GET page route + updated POST route (redirect-on-success, re-render-on-error). Five new page templates extend `base.html` with breadcrumb nav. New-record pages (create) embed the plugin builder channel picker and include content/overrides/preview card partials directly. Edit pages wrap `notification_form.html`. Single-click mutations (toggle, test, delete, assign/unassign/copy) stay HTMX. The assign-from-library inline picker stays inline. Four inline form partials are deleted.

**Tech Stack:** FastAPI, Jinja2, HTMX, Python 3.12+, pytest (integration), httpx

---

## File Map

### Create
| File | Purpose |
|---|---|
| `src/dashboard/templates/pages/notification_new.html` | Template library new-record full page |
| `src/dashboard/templates/pages/notification_edit.html` | Template library edit full page |
| `src/dashboard/templates/pages/watch_notification_new.html` | Watch local-config new-record full page |
| `src/dashboard/templates/pages/watch_notification_edit.html` | Watch local-config edit full page |
| `src/dashboard/templates/pages/domain_notification_new.html` | Domain-default new-record full page |

### Modify
| File | Change |
|---|---|
| `src/dashboard/routes.py` | Add 5 GET page routes; add 1 POST route; update 5 POST handlers (redirect-on-success); remove 6 old inline routes |
| `src/dashboard/templates/pages/notifications.html` | "New Template" button → `<a href="/notifications/new">`; remove `?edit=` JS block |
| `src/dashboard/templates/partials/notification_template_row.html` | Edit `<button hx-get>` → `<a href="/notifications/{id}/edit">` |
| `src/dashboard/templates/pages/watch_detail.html` | "+ Add Local" `<button hx-get>` → `<a href="/watches/{id}/notifications/new">` |
| `src/dashboard/templates/partials/watch_notifications.html` | Local-config Edit `<button hx-get>` → `<a href="/watches/{id}/notifications/{nc_id}/edit">` |
| `src/dashboard/templates/partials/domain_nc_defaults.html` | "+ Create New" `<button hx-get>` → `<a href="/domains/{name}/notifications/new">`; update both `?edit=` Edit links → `/notifications/{id}/edit` |
| `tests/dashboard/test_notifications_page.py` | Replace HTMX-era inline tests with page-route tests |
| `tests/dashboard/test_watch_nc_form_migration.py` | Point tests at new page routes; delete stale HTMX-header assertions |
| `tests/dashboard/test_domain_nc_form_migration.py` | Point tests at new page routes; update POST URL in content-config classes |

### Delete
| File | Reason |
|---|---|
| `src/dashboard/templates/partials/notification_add_row.html` | Replaced by `/watches/{id}/notifications/new` page |
| `src/dashboard/templates/partials/notification_edit_form.html` | Replaced by `/watches/{id}/notifications/{nc_id}/edit` page |
| `src/dashboard/templates/partials/notification_template_add_row.html` | Replaced by `/notifications/new` page |
| `src/dashboard/templates/partials/notification_template_edit_form.html` | Replaced by `/notifications/{id}/edit` page |
| `src/dashboard/templates/partials/domain_nc_template_add_row.html` | Replaced by `/domains/{name}/notifications/new` page |

---

## Task 1: Template Library — New Template Page

**Files:**
- Create: `src/dashboard/templates/pages/notification_new.html`
- Modify: `src/dashboard/routes.py` — add `GET /notifications/new`, update `POST /notifications/new`
- Test: `tests/dashboard/test_notifications_page.py`

### Context

`notification_template_add_row.html` is the model for the new page. It uses a custom channel-picker section (plugin builder + manual mode toggle with HTMX swapping `#plugin-token-form`) and includes content/overrides/preview card partials via `{% with form_id="tpl-new" %}`. The new page copies this structure but:
- extends `base.html` instead of living in a `<tr>`
- uses `<form method="post" action="/notifications/new">` (no `hx-post`)
- Cancel is `<a href="/notifications">` instead of an HTMX reload

`POST /notifications/new` currently returns an HTMX partial on success and uses `HX-Request` guard + `HX-Retarget`/`HX-Reswap` on error. Change: always `303 /notifications` on success; on error render the full page (HTTP 200) with the `error` variable populated and form values re-populated.

- [ ] **Step 1: Write failing tests**

In `tests/dashboard/test_notifications_page.py`, add:

```python
@pytest.mark.integration
async def test_notification_new_page_loads(client: AsyncClient):
    resp = await client.get("/notifications/new")
    assert resp.status_code == 200
    assert b"New Notification Template" in resp.content
    assert b"plugin_schema" in resp.content  # channel picker present


@pytest.mark.integration
async def test_create_template_redirects_on_success(client: AsyncClient, db_session):
    resp = await client.post(
        "/notifications/new",
        data={
            "title": "Ops Alert",
            "apprise_url": VALID_URL,
            "events": ["change_detected"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/notifications"


@pytest.mark.integration
async def test_create_template_rerenders_page_on_title_error(client: AsyncClient):
    resp = await client.post(
        "/notifications/new",
        data={"title": "", "apprise_url": VALID_URL, "events": ["change_detected"]},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"Title is required" in resp.content
    assert b"New Notification Template" in resp.content
```

Also delete (or update) the existing `test_notifications_add_row_returns_form` test — the `/notifications/add-row` route is going away. Delete `test_create_template_via_dashboard_form` which asserts `status_code == 200` on HTMX POST — the new behaviour is 303.

- [ ] **Step 2: Run tests — confirm they fail**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest tests/dashboard/test_notifications_page.py -v -m integration 2>&1 | tail -30
```

Expected: `test_notification_new_page_loads` — 404; `test_create_template_redirects_on_success` — fails (200 not 303).

- [ ] **Step 3: Create the page template**

`src/dashboard/templates/pages/notification_new.html`:

```html
{% extends "base.html" %}
{% block title %}New Notification Template — Watcher{% endblock %}
{% block content %}
<nav aria-label="Breadcrumb" class="mb-1">
  <ol class="flex items-center gap-1 text-sm text-gray-500 dark:text-gray-400">
    <li><a href="/notifications" class="link">Notification Templates</a></li>
    <li aria-hidden="true">/</li>
    <li aria-current="page">New</li>
  </ol>
</nav>
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">New Notification Template</h2>

<form method="post" action="/notifications/new" class="space-y-4 max-w-3xl">

  {# Title #}
  <div>
    <label for="nf-title-tpl-new" class="form-label">
      Title <span class="text-red-500" aria-hidden="true">*</span>
    </label>
    <input type="text" id="nf-title-tpl-new" name="title"
           value="{{ title or '' }}" maxlength="100" required aria-required="true"
           class="form-input mt-1" placeholder="e.g. Ops Slack Channel">
  </div>

  {# Channel picker (plugin builder + manual toggle) — copy from notification_template_add_row.html #}
  {# Use picker id="tpl-new-channel-picker", _url_mode name="_url_mode_tpl_new",
     hx-target="#tpl-new-plugin-token-form" to avoid DOM id collisions #}
  <div>
    <label for="tpl-new-channel-picker" class="form-label">Notification channel</label>
    <div class="flex items-center gap-3 mt-1">
      <select
        id="tpl-new-channel-picker"
        name="_channel_picker"
        class="form-input"
        hx-get="/partials/apprise-plugin-form"
        hx-target="#tpl-new-plugin-token-form"
        hx-swap="outerHTML"
        hx-vals='js:{schema: htmx.find("#tpl-new-channel-picker").value}'
        hx-trigger="change"
        onchange="document.querySelector('input[name=_url_mode_tpl_new][value=builder]').checked = true"
        aria-label="Select notification plugin">
        <option value="">— pick a channel —</option>
        {% for plugin in apprise_plugins %}
        <option value="{{ plugin.plugin_schema }}">{{ plugin.service_name }}</option>
        {% endfor %}
      </select>
      <fieldset class="segment-group shrink-0" aria-label="URL entry mode">
        <legend class="sr-only">URL entry mode</legend>
        <label class="segment">
          <input type="radio" name="_url_mode_tpl_new" value="builder" checked
            hx-get="/partials/apprise-plugin-form"
            hx-vals='js:{schema: htmx.find("#tpl-new-channel-picker").value}'
            hx-target="#tpl-new-plugin-token-form"
            hx-swap="outerHTML"
            hx-trigger="click[htmx.find('#tpl-new-channel-picker').value !== '']"
            onclick="if(!htmx.find('#tpl-new-channel-picker').value) htmx.find('#tpl-new-plugin-token-form').innerHTML=''">
          <span class="text-xs">Builder</span>
        </label>
        <label class="segment">
          <input type="radio" name="_url_mode_tpl_new" value="manual"
            hx-get="/partials/apprise-plugin-form?raw=1"
            hx-target="#tpl-new-plugin-token-form"
            hx-swap="outerHTML">
          <span class="text-xs">Manual</span>
        </label>
      </fieldset>
    </div>
  </div>
  <div id="tpl-new-plugin-token-form"></div>

  {# Events — all event types enabled #}
  <fieldset>
    <legend class="form-label mb-2">Events</legend>
    <div class="flex flex-wrap gap-4">
      {% for et_value, et_label in event_titles.items() %}
      <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
        <input type="checkbox" name="events" value="{{ et_value }}"
               {% if events and et_value in events %}checked{% elif not events and et_value == "change_detected" %}checked{% endif %}
               class="rounded border-gray-300 dark:border-gray-600">
        {{ et_label }}
      </label>
      {% endfor %}
    </div>
  </fieldset>

  {# Global default toggle #}
  <div class="flex items-center gap-3 min-h-[44px]">
    <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer">
      <input type="checkbox" name="is_global_default" value="1"
             {% if is_global_default %}checked{% endif %}
             class="rounded border-gray-300 dark:border-gray-600">
      Mark as global default
    </label>
    <span class="text-xs text-gray-500 dark:text-gray-400">(auto-applied to all new watches)</span>
  </div>

  {% with form_id="tpl-new", content_config=content_config %}
  {% include "partials/notification_form_content_card.html" %}
  {% include "partials/notification_form_overrides_card.html" %}
  {% include "partials/notification_form_preview_card.html" %}
  {% endwith %}

  {% if error %}
  <div role="alert" class="text-sm text-red-600 dark:text-red-400">{{ error }}</div>
  {% endif %}

  <div class="flex items-center gap-2 pt-2">
    <button type="submit" class="btn btn-primary min-h-[44px]">Create</button>
    <a href="/notifications" class="btn btn-secondary min-h-[44px]">Cancel</a>
  </div>

</form>
{% endblock %}
```

- [ ] **Step 4: Add `GET /notifications/new` route to routes.py**

Insert before the existing `GET /notifications/add-row` handler (around line 2738):

```python
@router.get("/notifications/new")
async def notification_template_new_page(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
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
```

- [ ] **Step 5: Update `POST /notifications/new` handler**

Replace the current `notification_template_create` function body. Key changes:
- Remove the `if request.headers.get("HX-Request") != "true": return RedirectResponse(...)` guard
- On success: `return RedirectResponse(url="/notifications", status_code=303)`
- On error: render `pages/notification_new.html` (not the add-row partial) with `error=` and repopulated form values
- Remove `HX-Retarget`/`HX-Reswap` headers from error responses

**Important:** define `_page_error` *after* `form = await request.form()` so the closure captures `form` correctly.

Error helper pattern (replaces inline `return templates.TemplateResponse(request, "partials/notification_template_add_row.html", ...)` calls):

```python
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
```

Success path (after `await session.commit()`):
```python
return RedirectResponse(url="/notifications", status_code=303)
```

- [ ] **Step 6: Run tests — confirm they pass**

```bash
uv run pytest tests/dashboard/test_notifications_page.py -v -m integration 2>&1 | tail -30
```

Expected: all passing.

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/templates/pages/notification_new.html src/dashboard/routes.py tests/dashboard/test_notifications_page.py
git commit -m "#103 feat: template library new-record dedicated page"
```

---

## Task 2: Template Library — Edit Template Page

**Files:**
- Create: `src/dashboard/templates/pages/notification_edit.html`
- Modify: `src/dashboard/routes.py` — add `GET /notifications/{id}/edit`, update `POST /notifications/{id}/edit`
- Modify: `src/dashboard/templates/partials/notification_template_row.html`
- Test: `tests/dashboard/test_notifications_page.py`

### Context

`notification_template_edit_form.html` is the model. It wraps `notification_form.html` in a `<form hx-post>`. The new page wraps the same include in `<form method="post" action="/notifications/{id}/edit">`. The edit form uses raw URL input (no builder); `notification_form.html` already provides this.

The current edit POST handler:
- Has `if request.headers.get("HX-Request") != "true": return RedirectResponse(...)` guard
- Returns `partials/notification_template_list.html` on success
- Returns `partials/notification_template_edit_form.html` with `HX-Retarget`/`HX-Reswap` on error

Change to: redirect `303 /notifications` on success; render `pages/notification_edit.html` on error.

The Edit button in `notification_template_row.html` currently fires HTMX. Change to plain `<a href="/notifications/{tpl.id}/edit">`.

`watch_notifications.html` already has `<a href="/notifications?edit={{ tpl.id }}">` for global/domain/assigned-template Edit — update these to `<a href="/notifications/{{ tpl.id }}/edit">` (no `?edit=` param) while at it.

- [ ] **Step 1: Write failing tests**

In `tests/dashboard/test_notifications_page.py`, add:

```python
@pytest.mark.integration
async def test_notification_edit_page_loads(client: AsyncClient, db_session):
    tpl = await _make_template(db_session, title="Edit Me")
    await db_session.commit()
    resp = await client.get(f"/notifications/{tpl.id}/edit")
    assert resp.status_code == 200
    assert b"Edit Me" in resp.content
    assert b"apprise_url" in resp.content


@pytest.mark.integration
async def test_edit_template_redirects_on_success(client: AsyncClient, db_session):
    tpl = await _make_template(db_session)
    await db_session.commit()
    resp = await client.post(
        f"/notifications/{tpl.id}/edit",
        data={"title": "Updated", "apprise_url": VALID_URL, "events": ["change_detected"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/notifications"


@pytest.mark.integration
async def test_edit_template_rerenders_page_on_error(client: AsyncClient, db_session):
    tpl = await _make_template(db_session)
    await db_session.commit()
    resp = await client.post(
        f"/notifications/{tpl.id}/edit",
        data={"title": "X", "apprise_url": "not-a-valid-url", "events": ["change_detected"]},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"Edit" in resp.content
    assert b"not-a-valid-url" in resp.content or b"invalid" in resp.content.lower()
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
uv run pytest tests/dashboard/test_notifications_page.py::test_notification_edit_page_loads -v -m integration
```

Expected: 404.

- [ ] **Step 3: Create the page template**

`src/dashboard/templates/pages/notification_edit.html`:

```html
{% extends "base.html" %}
{% block title %}Edit {{ tpl.title }} — Watcher{% endblock %}
{% block content %}
<nav aria-label="Breadcrumb" class="mb-1">
  <ol class="flex items-center gap-1 text-sm text-gray-500 dark:text-gray-400">
    <li><a href="/notifications" class="link">Notification Templates</a></li>
    <li aria-hidden="true">/</li>
    <li aria-current="page">{{ tpl.title }}</li>
  </ol>
</nav>
<div class="flex items-center justify-between mb-6">
  <h2 class="text-2xl font-bold text-gray-900 dark:text-white">Edit Notification Template</h2>
  {% if watch_count or domain_count %}
  <p class="text-xs text-gray-500 dark:text-gray-400">
    Used by {{ watch_count }} watch{{ "es" if watch_count != 1 else "" }}
    and {{ domain_count }} domain{{ "s" if domain_count != 1 else "" }}.
  </p>
  {% endif %}
</div>

<form method="post" action="/notifications/{{ tpl.id }}/edit" class="max-w-3xl">

  {% if decryption_failed %}
  <div role="alert" class="mb-4 text-sm text-amber-600 dark:text-amber-400">
    Could not decrypt the stored URL — the field has been left blank. Enter the full Apprise URL to update it.
  </div>
  {% endif %}

  {% if error %}
  <div role="alert" class="mb-4 text-sm text-red-600 dark:text-red-400">{{ error }}</div>
  {% endif %}

  {% with
    form_id="tpl-" ~ (tpl.id|string),
    title=tpl.title,
    apprise_url=decrypted_url,
    events=tpl.events,
    show_global_default=True,
    is_global_default=tpl.is_global_default,
    show_preview=True,
    content_config=content_config
  %}
  {% include "partials/notification_form.html" %}
  {% endwith %}

  <div class="flex items-center gap-2 mt-4">
    <button type="submit" class="btn btn-primary min-h-[44px]">Save</button>
    <a href="/notifications" class="btn btn-secondary min-h-[44px]">Cancel</a>
  </div>

</form>
{% endblock %}
```

- [ ] **Step 4: Add `GET /notifications/{id}/edit` route and update POST handler**

Add the GET route after `GET /notifications/new` in routes.py:

```python
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
            "decrypted_url": decrypted_url,
            "decryption_failed": decryption_failed,
            "watch_count": watch_count,
            "domain_count": domain_count,
            "content_config": content_config,
            "error": None,
        },
    )
```

Update `notification_template_edit` (POST handler):
- Remove `if request.headers.get("HX-Request") != "true": return RedirectResponse(...)` guard
- On success: `return RedirectResponse(url="/notifications", status_code=303)`
- In `_edit_error`: render `pages/notification_edit.html` instead of `partials/notification_template_edit_form.html`; remove `HX-Retarget`/`HX-Reswap` headers from the error response

The `_edit_error` inner function should be updated to render:
```python
return templates.TemplateResponse(
    request,
    "pages/notification_edit.html",
    {
        "active_page": "notifications",
        "tpl": tpl,
        "decrypted_url": apprise_url,  # show what was submitted
        "decryption_failed": False,
        "watch_count": watch_count,
        "domain_count": domain_count,
        "content_config": content_config_err,
        "error": error_msg,
    },
)
```

- [ ] **Step 5: Update `notification_template_row.html` Edit button**

Replace the `<button hx-get="/notifications/{{ tpl.id }}/edit-form" ...>Edit</button>` with:

```html
<a href="/notifications/{{ tpl.id }}/edit"
   class="btn btn-secondary text-xs min-h-[44px]"
   aria-label="Edit {{ tpl.title }} notification template">
  Edit
</a>
```

- [ ] **Step 6: Update `watch_notifications.html` and `domain_nc_defaults.html` — Edit links**

Both templates contain `href="/notifications?edit={{ tpl.id }}"` that relied on the `<script>` block (being removed in Task 6) to trigger the HTMX edit-form swap. These must be updated to plain navigation links.

In `watch_notifications.html`: replace all three `href="/notifications?edit={{ tpl.id }}"` occurrences (global rows, domain rows, assigned rows) with `href="/notifications/{{ tpl.id }}/edit"`.

In `domain_nc_defaults.html`: replace both `href="/notifications?edit={{ tpl.id }}"` occurrences (global section row, line ~41; domain-assigned section row, line ~122) with `href="/notifications/{{ tpl.id }}/edit"`.

- [ ] **Step 7: Run tests — confirm they pass**

```bash
uv run pytest tests/dashboard/test_notifications_page.py -v -m integration 2>&1 | tail -30
```

- [ ] **Step 8: Commit**

```bash
git add src/dashboard/templates/pages/notification_edit.html \
        src/dashboard/templates/partials/notification_template_row.html \
        src/dashboard/templates/partials/watch_notifications.html \
        src/dashboard/routes.py \
        tests/dashboard/test_notifications_page.py
git commit -m "#103 feat: template library edit dedicated page"
```

---

## Task 3: Watch Local Config — New Record Page

**Files:**
- Create: `src/dashboard/templates/pages/watch_notification_new.html`
- Modify: `src/dashboard/routes.py` — add `GET /watches/{id}/notifications/new`, update `POST /watches/{id}/notifications/new`
- Modify: `src/dashboard/templates/pages/watch_detail.html`
- Test: `tests/dashboard/test_watch_nc_form_migration.py`

### Context

`notification_add_row.html` is the model. The new page:
- Has an **optional** title field (watch NCs don't require a title)
- Has the plugin builder channel picker — `watch_created` event is **disabled** (irrelevant once a watch exists)
- Includes content/overrides/preview card partials

`POST /watches/{watch_id}/notifications/new` currently returns `_render_watch_notifications(...)` (an HTMX partial). Change: redirect `303 /watches/{watch_id}#notifications` on success; render the full page on error.

`watch_detail.html` "+ Add Local" button fires HTMX. Change to `<a href="/watches/{watch.id}/notifications/new">`.

- [ ] **Step 1: Write failing tests**

In `tests/dashboard/test_watch_nc_form_migration.py`, replace or add:

```python
@pytest.mark.integration
class TestWatchNcNewPage:
    async def test_new_page_loads(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.get(f"/watches/{watch.id}/notifications/new")
        assert resp.status_code == 200
        assert b"plugin_schema" in resp.content
        assert b"watch_created" in resp.content  # disabled checkbox still present

    async def test_create_redirects_on_success(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/new",
            data={"apprise_url": VALID_URL, "events": ["change_detected"]},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/watches/{watch.id}" in resp.headers["location"]

    async def test_create_rerenders_page_on_error(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/new",
            data={"apprise_url": "bad-url", "events": ["change_detected"]},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert b"plugin_schema" in resp.content
```

Also update `TestWatchNcAddRowMigrated` — its test that hits `/watches/{id}/notifications/add-row` (which will be removed in Task 6) can be adapted or marked as "will be deleted in Task 6".

- [ ] **Step 2: Run tests — confirm they fail**

```bash
uv run pytest tests/dashboard/test_watch_nc_form_migration.py::TestWatchNcNewPage -v -m integration
```

Expected: 404.

- [ ] **Step 3: Create the page template**

`src/dashboard/templates/pages/watch_notification_new.html`:

```html
{% extends "base.html" %}
{% block title %}Add Notification — {{ watch.name }} — Watcher{% endblock %}
{% block content %}
<nav aria-label="Breadcrumb" class="mb-1">
  <ol class="flex items-center gap-1 text-sm text-gray-500 dark:text-gray-400">
    <li><a href="/watches/{{ watch.id }}" class="link">{{ watch.name }}</a></li>
    <li aria-hidden="true">/</li>
    <li aria-current="page">Add Notification</li>
  </ol>
</nav>
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">Add Notification</h2>

<form method="post" action="/watches/{{ watch.id }}/notifications/new" class="space-y-4 max-w-3xl">

  {# Title — optional for watch NCs #}
  <div>
    <label for="wnc-new-title" class="form-label">
      Title <span class="text-gray-400 dark:text-gray-500 font-normal">(optional)</span>
    </label>
    <input type="text" id="wnc-new-title" name="title"
           value="{{ title or '' }}" maxlength="100"
           class="form-input mt-1" placeholder="e.g. Slack ops channel">
  </div>

  {# Channel picker — same structure as notification_add_row.html
     but ids scoped to avoid collisions on pages that might have multiple forms #}
  <div>
    <label for="wnc-new-channel-picker" class="form-label">Notification channel</label>
    <div class="flex items-center gap-3 mt-1">
      <select
        id="wnc-new-channel-picker"
        name="_channel_picker"
        class="form-input"
        hx-get="/partials/apprise-plugin-form"
        hx-target="#wnc-new-plugin-token-form"
        hx-swap="outerHTML"
        hx-vals='js:{schema: htmx.find("#wnc-new-channel-picker").value}'
        hx-trigger="change"
        onchange="document.querySelector('input[name=_url_mode_wnc_new][value=builder]').checked = true"
        aria-label="Select notification plugin">
        <option value="">— pick a channel —</option>
        {% for plugin in apprise_plugins %}
        <option value="{{ plugin.plugin_schema }}">{{ plugin.service_name }}</option>
        {% endfor %}
      </select>
      <fieldset class="segment-group shrink-0" aria-label="URL entry mode">
        <legend class="sr-only">URL entry mode</legend>
        <label class="segment">
          <input type="radio" name="_url_mode_wnc_new" value="builder" checked
            hx-get="/partials/apprise-plugin-form"
            hx-vals='js:{schema: htmx.find("#wnc-new-channel-picker").value}'
            hx-target="#wnc-new-plugin-token-form"
            hx-swap="outerHTML"
            hx-trigger="click[htmx.find('#wnc-new-channel-picker').value !== '']"
            onclick="if(!htmx.find('#wnc-new-channel-picker').value) htmx.find('#wnc-new-plugin-token-form').innerHTML=''">
          <span class="text-xs">Builder</span>
        </label>
        <label class="segment">
          <input type="radio" name="_url_mode_wnc_new" value="manual"
            hx-get="/partials/apprise-plugin-form?raw=1"
            hx-target="#wnc-new-plugin-token-form"
            hx-swap="outerHTML">
          <span class="text-xs">Manual</span>
        </label>
      </fieldset>
    </div>
  </div>
  <div id="wnc-new-plugin-token-form"></div>

  {# Events — watch_created disabled (watch already exists) #}
  <fieldset>
    <legend class="form-label mb-2">Events</legend>
    <div class="flex flex-wrap gap-4">
      <label class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-500 min-h-[44px]"
             aria-label="Watch Created (not applicable — watch already exists)">
        <input type="checkbox" name="events" value="watch_created"
               disabled class="rounded border-gray-300 dark:border-gray-600 opacity-50 cursor-not-allowed">
        {{ event_titles.get("watch_created", "Watch Created") }}
      </label>
      {% for et_value, et_label in event_titles.items() %}
      {% if et_value != "watch_created" %}
      <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
        <input type="checkbox" name="events" value="{{ et_value }}"
               {% if events and et_value in events %}checked{% elif not events and et_value == "change_detected" %}checked{% endif %}
               class="rounded border-gray-300 dark:border-gray-600">
        {{ et_label }}
      </label>
      {% endif %}
      {% endfor %}
    </div>
  </fieldset>

  {% with form_id="wnc-new-" ~ (watch.id|string), content_config=content_config %}
  {% include "partials/notification_form_content_card.html" %}
  {% include "partials/notification_form_overrides_card.html" %}
  {% include "partials/notification_form_preview_card.html" %}
  {% endwith %}

  {% if error %}
  <div role="alert" class="text-sm text-red-600 dark:text-red-400">{{ error }}</div>
  {% endif %}

  <div class="flex items-center gap-2 pt-2">
    <button type="submit" class="btn btn-primary min-h-[44px]">Add</button>
    <a href="/watches/{{ watch.id }}#notifications" class="btn btn-secondary min-h-[44px]">Cancel</a>
  </div>

</form>
{% endblock %}
```

- [ ] **Step 4: Add `GET /watches/{watch_id}/notifications/new` route**

Insert before existing `GET /watches/{watch_id}/notifications/add-row` (around line 1849) in routes.py:

```python
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
```

- [ ] **Step 5: Update `POST /watches/{watch_id}/notifications/new` handler**

In `watch_notification_create`, replace all `return templates.TemplateResponse(request, "partials/notification_add_row.html", ...)` error responses with:

```python
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
```

Remove `headers={"HX-Retarget": ..., "HX-Reswap": ...}` from all error responses.

Replace the final `return await _render_watch_notifications(request, watch, session)` with:

```python
return RedirectResponse(url=f"/watches/{watch_id}#notifications", status_code=303)
```

- [ ] **Step 6: Update `watch_detail.html` "+ Add Local" button**

Replace:
```html
<button
  hx-get="/watches/{{ watch.id }}/notifications/add-row"
  hx-target="#notifications-tbody"
  hx-swap="afterbegin"
  hx-on::before-request="if(document.getElementById('notification-add-row')){event.preventDefault();}"
  hx-on::after-request="if(event.detail.successful){const e=document.getElementById('notifications-empty-state');if(e)e.remove();}"
  class="btn btn-secondary text-xs min-h-[44px] htmx-request:opacity-60">
  + Add Local
</button>
```

With:
```html
<a href="/watches/{{ watch.id }}/notifications/new"
   class="btn btn-secondary text-xs min-h-[44px]">
  + Add Local
</a>
```

- [ ] **Step 7: Run tests — confirm they pass**

```bash
uv run pytest tests/dashboard/test_watch_nc_form_migration.py::TestWatchNcNewPage -v -m integration
```

- [ ] **Step 8: Commit**

```bash
git add src/dashboard/templates/pages/watch_notification_new.html \
        src/dashboard/templates/pages/watch_detail.html \
        src/dashboard/routes.py \
        tests/dashboard/test_watch_nc_form_migration.py
git commit -m "#103 feat: watch local-config new-record dedicated page"
```

---

## Task 4: Watch Local Config — Edit Page

**Files:**
- Create: `src/dashboard/templates/pages/watch_notification_edit.html`
- Modify: `src/dashboard/routes.py` — add `GET /watches/{id}/notifications/{nc_id}/edit`, update `POST /watches/{id}/notifications/{nc_id}/edit`
- Modify: `src/dashboard/templates/partials/watch_notifications.html`
- Test: `tests/dashboard/test_watch_nc_form_migration.py`

### Context

`notification_edit_form.html` is the model — it already uses `notification_form.html`. The new edit page wraps the same include in a full page. Edit uses raw URL input; no plugin builder.

`POST /watches/{watch_id}/notifications/{config_id}/edit` currently returns the notifications partial or re-renders the edit form with HTMX retarget. Change: redirect `303 /watches/{watch_id}#notifications` on success; render full page on error (no `HX-Retarget`/`HX-Reswap`).

The local config Edit button in `watch_notifications.html` fires HTMX. Change to `<a href="/watches/{id}/notifications/{nc_id}/edit">`.

- [ ] **Step 1: Write failing tests**

In `tests/dashboard/test_watch_nc_form_migration.py`, add:

```python
@pytest.mark.integration
class TestWatchNcEditPage:
    async def test_edit_page_loads(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.get(f"/watches/{watch.id}/notifications/{nc.id}/edit")
        assert resp.status_code == 200
        assert b"apprise_url" in resp.content

    async def test_edit_redirects_on_success(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/{nc.id}/edit",
            data={"apprise_url": VALID_URL, "events": ["change_detected"]},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/watches/{watch.id}" in resp.headers["location"]

    async def test_edit_rerenders_page_on_error(self, client: AsyncClient, db_session):
        watch = await _make_watch(db_session)
        nc = await _make_nc(db_session, watch)
        resp = await client.post(
            f"/watches/{watch.id}/notifications/{nc.id}/edit",
            data={"apprise_url": "bad-url", "events": ["change_detected"]},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert b"apprise_url" in resp.content
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
uv run pytest tests/dashboard/test_watch_nc_form_migration.py::TestWatchNcEditPage -v -m integration
```

- [ ] **Step 3: Create the page template**

`src/dashboard/templates/pages/watch_notification_edit.html`:

```html
{% extends "base.html" %}
{% block title %}Edit Notification — {{ watch.name }} — Watcher{% endblock %}
{% block content %}
<nav aria-label="Breadcrumb" class="mb-1">
  <ol class="flex items-center gap-1 text-sm text-gray-500 dark:text-gray-400">
    <li><a href="/watches/{{ watch.id }}" class="link">{{ watch.name }}</a></li>
    <li aria-hidden="true">/</li>
    <li aria-current="page">Edit Notification</li>
  </ol>
</nav>
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">Edit Notification</h2>

<form method="post" action="/watches/{{ watch.id }}/notifications/{{ nc.id }}/edit"
      class="max-w-3xl">

  {% if decryption_failed %}
  <div role="alert" class="mb-4 text-sm text-amber-600 dark:text-amber-400">
    Could not decrypt the stored URL — the field has been left blank. Enter the full Apprise URL to update it.
  </div>
  {% endif %}

  {% if error %}
  <div role="alert" class="mb-4 text-sm text-red-600 dark:text-red-400">{{ error }}</div>
  {% endif %}

  {% with
    form_id="wnc-" ~ (nc.id|string),
    title=nc.title,
    apprise_url=decrypted_url,
    events=nc.events,
    show_global_default=False,
    is_global_default=False,
    show_preview=True,
    content_config=content_config
  %}
  {% include "partials/notification_form.html" %}
  {% endwith %}

  <div class="flex items-center gap-2 mt-4">
    <button type="submit" class="btn btn-primary min-h-[44px]">Save</button>
    <a href="/watches/{{ watch.id }}#notifications" class="btn btn-secondary min-h-[44px]">Cancel</a>
  </div>

</form>
{% endblock %}
```

- [ ] **Step 4: Add `GET` route and update POST handler**

Add before `GET /watches/{watch_id}/notifications/{config_id}/edit-form` (around line 2022):

```python
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
```

In `watch_notification_edit` (POST), replace both error `return` statements (currently rendering `notification_edit_form.html`) with:

```python
try:
    decrypted_url = decrypt_apprise_url(nc.apprise_url)
except (InvalidToken, ValueError):
    decrypted_url = ""
_cc = _parse_content_config_from_form(form)
return templates.TemplateResponse(
    request,
    "pages/watch_notification_edit.html",
    {
        "watch": watch,
        "nc": nc,
        "decrypted_url": apprise_url,  # show what was submitted
        "decryption_failed": False,
        "content_config": ContentConfig.model_validate(_cc) if _cc else None,
        "error": str(exc),
    },
)
```

Remove `HX-Retarget`/`HX-Reswap` headers. Replace the final `return await _render_watch_notifications(...)` with:

```python
return RedirectResponse(url=f"/watches/{watch_id}#notifications", status_code=303)
```

- [ ] **Step 5: Update `watch_notifications.html` local config Edit button**

Replace:
```html
<button
  hx-get="/watches/{{ watch.id }}/notifications/{{ nc.id }}/edit-form"
  hx-target="#nc-{{ nc.id }}"
  hx-swap="outerHTML"
  class="btn btn-secondary text-xs min-h-[44px] htmx-request:opacity-60"
  aria-label="Edit {{ nc.channel_hint }} notification URL and events">
  Edit
</button>
```

With:
```html
<a href="/watches/{{ watch.id }}/notifications/{{ nc.id }}/edit"
   class="btn btn-secondary text-xs min-h-[44px]"
   aria-label="Edit {{ nc.channel_hint }} notification URL and events">
  Edit
</a>
```

- [ ] **Step 6: Run tests — confirm they pass**

```bash
uv run pytest tests/dashboard/test_watch_nc_form_migration.py -v -m integration 2>&1 | tail -30
```

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/templates/pages/watch_notification_edit.html \
        src/dashboard/templates/partials/watch_notifications.html \
        src/dashboard/routes.py \
        tests/dashboard/test_watch_nc_form_migration.py
git commit -m "#103 feat: watch local-config edit dedicated page"
```

---

## Task 5: Domain Default — New Template Page

**Files:**
- Create: `src/dashboard/templates/pages/domain_notification_new.html`
- Modify: `src/dashboard/routes.py` — add `GET /domains/{name}/notifications/new`, add `POST /domains/{name}/notifications/new`
- Modify: `src/dashboard/templates/partials/domain_nc_defaults.html`
- Test: `tests/dashboard/test_domain_nc_form_migration.py`

### Context

`domain_nc_template_add_row.html` is the model. The new page is nearly identical to `notification_new.html` (template library new), except:
- Breadcrumb points back to `/domains/{name}` instead of `/notifications`
- No `is_global_default` toggle — domain templates are never global defaults
- POST action is `/domains/{name}/notifications/new`
- On success redirect to `/domains/{name}`

`POST /domains/{name}/notifications/new` is a new route (the old one was `POST /domains/{name}/nc-defaults/new`). The handler logic is identical — create `NotificationTemplate` + `DomainNcRef` — just at the new URL.

The "+ Create New" button in `domain_nc_defaults.html` currently fires HTMX. Change to `<a href="/domains/{domain_name}/notifications/new">`.

- [ ] **Step 1: Write failing tests**

In `tests/dashboard/test_domain_nc_form_migration.py`, add:

```python
@pytest.mark.integration
class TestDomainNcNewPage:
    async def test_new_page_loads(self, client: AsyncClient, db_session):
        await _ensure_domain(db_session, "example.com")
        resp = await client.get("/domains/example.com/notifications/new")
        assert resp.status_code == 200
        assert b"plugin_schema" in resp.content
        assert b"example.com" in resp.content

    async def test_create_redirects_on_success(self, client: AsyncClient, db_session):
        await _ensure_domain(db_session, "example.com")
        resp = await client.post(
            "/domains/example.com/notifications/new",
            data={
                "title": "Domain Alert",
                "apprise_url": "json://hooks.example.com/notify",
                "events": ["change_detected"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "/domains/example.com" in resp.headers["location"]

    async def test_create_rerenders_page_on_error(self, client: AsyncClient, db_session):
        await _ensure_domain(db_session, "example.com")
        resp = await client.post(
            "/domains/example.com/notifications/new",
            data={"title": "", "apprise_url": "json://x", "events": ["change_detected"]},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert b"Title is required" in resp.content
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
uv run pytest tests/dashboard/test_domain_nc_form_migration.py::TestDomainNcNewPage -v -m integration
```

- [ ] **Step 3: Create the page template**

`src/dashboard/templates/pages/domain_notification_new.html`:

```html
{% extends "base.html" %}
{% block title %}New Notification — {{ domain_name }} — Watcher{% endblock %}
{% block content %}
<nav aria-label="Breadcrumb" class="mb-1">
  <ol class="flex items-center gap-1 text-sm text-gray-500 dark:text-gray-400">
    <li><a href="/domains/{{ domain_name }}" class="link">{{ domain_name }}</a></li>
    <li aria-hidden="true">/</li>
    <li aria-current="page">New Notification</li>
  </ol>
</nav>
<h2 class="text-2xl font-bold text-gray-900 dark:text-white mb-6">New Notification Template</h2>
<p class="text-sm text-gray-600 dark:text-gray-400 mb-6">
  Creates a new template and sets it as a default for <strong>{{ domain_name }}</strong>.
</p>

<form method="post" action="/domains/{{ domain_name }}/notifications/new" class="space-y-4 max-w-3xl">

  {# Title — required #}
  <div>
    <label for="dn-new-title" class="form-label">
      Title <span class="text-red-500" aria-hidden="true">*</span>
    </label>
    <input type="text" id="dn-new-title" name="title"
           value="{{ title or '' }}" maxlength="100" required aria-required="true"
           class="form-input mt-1" placeholder="e.g. Domain Ops Slack">
  </div>

  {# Channel picker — same structure; scoped ids #}
  <div>
    <label for="dn-new-channel-picker" class="form-label">Notification channel</label>
    <div class="flex items-center gap-3 mt-1">
      <select
        id="dn-new-channel-picker"
        name="_channel_picker"
        class="form-input"
        hx-get="/partials/apprise-plugin-form"
        hx-target="#dn-new-plugin-token-form"
        hx-swap="outerHTML"
        hx-vals='js:{schema: htmx.find("#dn-new-channel-picker").value}'
        hx-trigger="change"
        onchange="document.querySelector('input[name=_url_mode_dn_new][value=builder]').checked = true"
        aria-label="Select notification plugin">
        <option value="">— pick a channel —</option>
        {% for plugin in apprise_plugins %}
        <option value="{{ plugin.plugin_schema }}">{{ plugin.service_name }}</option>
        {% endfor %}
      </select>
      <fieldset class="segment-group shrink-0" aria-label="URL entry mode">
        <legend class="sr-only">URL entry mode</legend>
        <label class="segment">
          <input type="radio" name="_url_mode_dn_new" value="builder" checked
            hx-get="/partials/apprise-plugin-form"
            hx-vals='js:{schema: htmx.find("#dn-new-channel-picker").value}'
            hx-target="#dn-new-plugin-token-form"
            hx-swap="outerHTML"
            hx-trigger="click[htmx.find('#dn-new-channel-picker').value !== '']"
            onclick="if(!htmx.find('#dn-new-channel-picker').value) htmx.find('#dn-new-plugin-token-form').innerHTML=''">
          <span class="text-xs">Builder</span>
        </label>
        <label class="segment">
          <input type="radio" name="_url_mode_dn_new" value="manual"
            hx-get="/partials/apprise-plugin-form?raw=1"
            hx-target="#dn-new-plugin-token-form"
            hx-swap="outerHTML">
          <span class="text-xs">Manual</span>
        </label>
      </fieldset>
    </div>
  </div>
  <div id="dn-new-plugin-token-form"></div>

  {# Events — all enabled #}
  <fieldset>
    <legend class="form-label mb-2">Events</legend>
    <div class="flex flex-wrap gap-4">
      {% for et_value, et_label in event_titles.items() %}
      <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer min-h-[44px]">
        <input type="checkbox" name="events" value="{{ et_value }}"
               {% if events and et_value in events %}checked{% elif not events and et_value == "change_detected" %}checked{% endif %}
               class="rounded border-gray-300 dark:border-gray-600">
        {{ et_label }}
      </label>
      {% endfor %}
    </div>
  </fieldset>

  {% with form_id="dn-new-" ~ domain_name, content_config=content_config %}
  {% include "partials/notification_form_content_card.html" %}
  {% include "partials/notification_form_overrides_card.html" %}
  {% include "partials/notification_form_preview_card.html" %}
  {% endwith %}

  {% if error %}
  <div role="alert" class="text-sm text-red-600 dark:text-red-400">{{ error }}</div>
  {% endif %}

  <div class="flex items-center gap-2 pt-2">
    <button type="submit" class="btn btn-primary min-h-[44px]">Create</button>
    <a href="/domains/{{ domain_name }}" class="btn btn-secondary min-h-[44px]">Cancel</a>
  </div>

</form>
{% endblock %}
```

- [ ] **Step 4: Add routes to routes.py**

Add GET + POST pair. Insert near the domain notification section (after line ~1460):

```python
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
```

- [ ] **Step 5: Update `domain_nc_defaults.html` "+ Create New" button**

Replace:
```html
<button
  hx-get="/domains/{{ domain_name }}/nc-defaults/add-template-row"
  hx-target="#domain-nc-tbody"
  hx-swap="afterbegin"
  hx-on::before-request="if(document.getElementById('domain-nc-add-row')){event.preventDefault();}"
  class="btn btn-secondary text-xs min-h-[44px] htmx-request:opacity-60">
  + Create New
</button>
```

With:
```html
<a href="/domains/{{ domain_name }}/notifications/new"
   class="btn btn-secondary text-xs min-h-[44px]">
  + Create New
</a>
```

- [ ] **Step 6: Run tests — confirm they pass**

```bash
uv run pytest tests/dashboard/test_domain_nc_form_migration.py -v -m integration 2>&1 | tail -30
```

- [ ] **Step 7: Commit**

```bash
git add src/dashboard/templates/pages/domain_notification_new.html \
        src/dashboard/templates/partials/domain_nc_defaults.html \
        src/dashboard/routes.py \
        tests/dashboard/test_domain_nc_form_migration.py
git commit -m "#103 feat: domain-default new-record dedicated page"
```

---

## Task 6: Cleanup — Delete Inline Routes, Partials, and Old Tests

**Files:**
- Modify: `src/dashboard/routes.py` — remove 6 routes
- Modify: `src/dashboard/templates/pages/notifications.html` — update "New Template" button, remove `?edit=` JS
- Delete: 5 partial templates
- Modify: `tests/dashboard/test_notifications_page.py` — remove/update stale tests
- Modify: `tests/dashboard/test_watch_nc_form_migration.py` — remove stale tests
- Modify: `tests/dashboard/test_domain_nc_form_migration.py` — remove stale tests

### Routes to remove from routes.py

| Route | Handler name | Approx. line |
|---|---|---|
| `GET /notifications/add-row` | `notification_template_add_row` | 2738 |
| `GET /notifications/{template_id}/edit-form` | `notification_template_edit_form` | 2866 |
| `GET /watches/{watch_id}/notifications/add-row` | `watch_notification_add_row` | 1849 |
| `GET /watches/{watch_id}/notifications/{config_id}/edit-form` | `watch_notification_edit_form` | 2022 |
| `GET /domains/{domain_name}/nc-defaults/add-template-row` | `domain_nc_defaults_add_template_row` | 1494 |
| `POST /domains/{domain_name}/nc-defaults/new` | `domain_nc_defaults_create_template` | 1516 |

### Partial templates to delete

```bash
rm src/dashboard/templates/partials/notification_add_row.html
rm src/dashboard/templates/partials/notification_edit_form.html
rm src/dashboard/templates/partials/notification_template_add_row.html
rm src/dashboard/templates/partials/notification_template_edit_form.html
rm src/dashboard/templates/partials/domain_nc_template_add_row.html
```

### `notifications.html` updates

1. Replace "New Template" HTMX button with plain link:

```html
<a href="/notifications/new" class="btn btn-primary">New Template</a>
```

2. Remove the `apprise_plugins` variable from `notifications_page` route context (no longer needed by the page).

3. Remove the `<script>` block at bottom of `notifications.html` that handles `?edit=<id>` auto-loading — the edit page is now a direct URL (`/notifications/{id}/edit`).

### Test cleanup

**`test_notifications_page.py`** — delete these functions (all test removed routes or assert HTMX-era behaviour):
- `test_notifications_add_row_returns_form` — tests `/notifications/add-row`
- `test_create_template_via_dashboard_form` — asserts `status_code == 200` on POST (now 303)
- `test_create_template_invalid_url_returns_form_with_error` — asserts 200 inline re-render (now full page; replace with a test asserting 200 + error text if not already added in Task 1)
- Any `test_edit_form_*` functions — test `/notifications/{id}/edit-form` route (removed)
- `test_edit_saves_title_and_returns_list` — asserts HTMX 200 response (now 303)
- `test_edit_invalid_url_returns_form_with_error` — asserts `HX-Retarget` header (removed)

The new tests added in Tasks 1 and 2 cover all these cases.

**`test_watch_nc_form_migration.py`** — delete these:
- `TestWatchNcAddRowMigrated` class — tests `/watches/{id}/notifications/add-row` (removed)
- `TestWatchNcEditFormMigrated` class — tests `/watches/{id}/notifications/{nc_id}/edit-form` (removed)
- Any test that asserts `HX-Retarget` on POST error response (e.g. `test_edit_error_preserves_toggle_state`) — that header is no longer set

**`test_domain_nc_form_migration.py`** — update/delete these:
- `TestDomainNcAddRowMigrated` class — tests `/domains/{name}/nc-defaults/add-template-row` (removed)
- `TestDomainNcCreatePersistsContentConfig` class — POSTs to `/domains/{name}/nc-defaults/new` (removed); update URL to `/domains/{name}/notifications/new`
- `TestDomainNcCreateErrorPathPreservesContentConfig` class — same URL update required; also update any assertions that check for `HX-Retarget` or HTMX partials in the response

- [ ] **Step 1: Remove the 6 route handlers from routes.py**

Identify each function by the `@router.get`/`@router.post` decorator + function name listed above. Delete the decorator + the complete function body.

- [ ] **Step 2: Delete the 5 partial templates**

```bash
rm src/dashboard/templates/partials/notification_add_row.html \
   src/dashboard/templates/partials/notification_edit_form.html \
   src/dashboard/templates/partials/notification_template_add_row.html \
   src/dashboard/templates/partials/notification_template_edit_form.html \
   src/dashboard/templates/partials/domain_nc_template_add_row.html
```

- [ ] **Step 3: Update `notifications.html`**

Replace the `<button hx-get="/notifications/add-row" ...>New Template</button>` with `<a href="/notifications/new" class="btn btn-primary">New Template</a>`.

Remove the `<script>` block (lines 40-54 in the current file).

- [ ] **Step 4: Remove `apprise_plugins` from `notifications_page` route**

In `notifications_page`, remove `apprise_plugins = list_plugins()` and the `"apprise_plugins": apprise_plugins` key from the context dict.

- [ ] **Step 5: Delete/update stale tests**

Follow the full list in the "Test cleanup" section above. Key actions:

**`test_notifications_page.py`**: delete `test_notifications_add_row_returns_form`, `test_create_template_via_dashboard_form`, `test_create_template_invalid_url_returns_form_with_error`, all `test_edit_form_*` functions, `test_edit_saves_title_and_returns_list`, `test_edit_invalid_url_returns_form_with_error`.

**`test_watch_nc_form_migration.py`**: delete `TestWatchNcAddRowMigrated`, `TestWatchNcEditFormMigrated`, and any test asserting `HX-Retarget` on edit POST errors.

**`test_domain_nc_form_migration.py`**: delete `TestDomainNcAddRowMigrated`; update `TestDomainNcCreatePersistsContentConfig` and `TestDomainNcCreateErrorPathPreservesContentConfig` to POST to `/domains/{name}/notifications/new` and remove any `HX-Retarget`/HTMX-partial assertions.

- [ ] **Step 6: Run the full test suite to confirm no regressions**

```bash
uv run pytest tests/dashboard/ -v -m integration 2>&1 | tail -50
```

Expected: all passing, no references to removed routes.

Also run ruff to catch any unused imports left behind by route deletions:

```bash
uv run ruff check src/dashboard/routes.py
```

Fix any unused imports flagged.

- [ ] **Step 7: Commit**

```bash
git add -u  # stage all modifications and deletions
git commit -m "#103 chore: remove inline notification add/edit form routes and partials"
```

---

## Task 7: Smoke Test and Restart

- [ ] **Step 1: Run full test suite**

```bash
export $(cat /etc/watcher/.env .env 2>/dev/null | xargs)
uv run pytest -m integration 2>&1 | tail -20
```

Expected: green.

- [ ] **Step 2: Merge to main and restart service**

```bash
git checkout main
git merge --no-ff -  # merge the feature branch, or just ensure commits land on main
sudo systemctl restart watcher
sudo journalctl -u watcher -f -n 30
```

- [ ] **Step 3: Verify in browser**

- `/notifications/new` — full page renders, plugin builder works, Create → redirect back to library
- `/notifications/{id}/edit` — pre-fills title and URL, Save → redirect back to library
- Watch detail → "+ Add Local" → navigates to `/watches/{id}/notifications/new`
- Watch detail, local config Edit → navigates to edit page
- Domain detail → "+ Create New" → navigates to `/domains/{name}/notifications/new`
- All table HTMX mutations (toggle, test, delete, assign, unassign, copy) still work inline
