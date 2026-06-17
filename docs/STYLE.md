# watcher — Style Guide

Authoritative reference for the dashboard UI. Documents what IS implemented, not aspirations.

---

## 1. Brand Assets

| Asset | Path | Details |
|---|---|---|
| Cannabis Observer icon | `src/dashboard/static/images/cannabis_observer-icon-square.svg` | Org logo; 16x16 in footer |
| Cannabis Observer name | `src/dashboard/static/images/cannabis_observer-name.svg` | Org wordmark; available for future use |
| Magnifying glass icon | `src/dashboard/static/images/magnifying-glass.svg` | Project icon; `#17de6b` green; 28x28 in sidebar/drawer |
| Favicon | Inline data URI in `base.html` | Green magnifying glass (`#17de6b`), no external file |
| Footer emoji | `🌱🏛️🔍` | Wrapped in `<span aria-hidden="true">` — purely decorative |

All `<img>` tags for icons carry `alt="" aria-hidden="true"` and explicit `width`/`height`.

---

## 2. Color Palette

### Brand tokens (`@theme` block in `input.css`)

| Token | Hex | Usage |
|---|---|---|
| `--color-co-purple-50` | `#f5f0f8` | Active nav background (light) |
| `--color-co-purple-100` | `#ebe1f1` | Link hover text (dark) |
| `--color-co-purple-400` | `#a78bc4` | Active nav text (dark), focus rings (dark) |
| `--color-co-purple-600` | `#6d4488` | Primary buttons, links, focus rings (light) |
| `--color-co-purple-700` | `#5a3870` | Primary button hover |
| `--color-co-purple-800` | `#472c59` | Active nav background (dark) |
| `--color-co-green` | `#8cbe69` | Brand accent (not used in status) |

### Semantic status colors

Status badges and flashes use **Tailwind defaults only** — never brand purple/green.

| Semantic | Light bg | Light text | Dark bg | Dark text |
|---|---|---|---|---|
| Active/Success | `green-100` | `green-800` | `green-900` | `green-300` |
| Inactive | `gray-100` | `gray-500` | `gray-700` | `gray-400` |
| Error | `red-100` | `red-800` | `red-900` | `red-300` |
| Warning | `orange-100` / `yellow-50` | `orange-800` / `yellow-800` | `orange-900` / `yellow-900/30` | `orange-300` / `yellow-300` |
| Info | `blue-100` / `blue-50` | `blue-800` | `blue-900` / `blue-900/30` | `blue-300` |

---

## 3. Dark Mode

- **Mechanism**: Tailwind `darkMode: class` via `@custom-variant dark (&:where(.dark, .dark *));` in `input.css`.
- **localStorage key**: `watcher-color-scheme` — values: `"dark"`, `"light"`, or absent (follow system).
- **FOUC prevention**: Inline `<script>` in `<head>` (before stylesheet) reads localStorage and system preference, adds `.dark` class to `<html>` synchronously.
- **`<noscript>` fallback**: `<style>` block applies `color-scheme: dark` via `prefers-color-scheme` media query when JS is disabled.
- **Toggle**: `button#theme-toggle` (desktop sidebar) and `button#theme-toggle-mobile` (mobile topbar). Icon swaps between ☀ (sun, when dark) and ☽ (moon, when light). `aria-label` updates dynamically.
- **Implementation**: `src/dashboard/static/js/dark-mode.js` — toggles `.dark` on `<html>`, persists to localStorage.

---

## 4. CSS Design Token System

- **Tailwind v4** with `@theme` block in `src/dashboard/static/css/input.css`.
- Custom tokens use `--color-co-purple-*` naming (prefix `co-` = Cannabis Observer).
- Spacing, sizing, border-radius, shadows: **Tailwind defaults** — no custom tokens.
- Template source scanning: `@source "../../templates/**/*.html";` ensures Tailwind picks up classes from all templates.
- Compiled output: `src/dashboard/static/css/output.css`.

---

## 5. Layout

```
┌─────────────────────────────────────────────┐
│ <html>  flex h-full                         │
│ ┌──────────┬───────────────────────────────┐│
│ │ Sidebar  │  Mobile topbar (md:hidden)    ││
│ │ w-60     │  ┌─────────────────────────┐  ││
│ │ desktop  │  │ #flash-region           │  ││
│ │ only     │  │ (aria-live="polite")    │  ││
│ │          │  ├─────────────────────────┤  ││
│ │          │  │ #main-content           │  ││
│ │          │  │ flex-1 overflow-y-auto  │  ││
│ │          │  │ px-4 md:px-8 py-6       │  ││
│ │          │  │                         │  ││
│ │          │  │   <footer> (inside main) │  ││
│ │          │  └─────────────────────────┘  ││
│ └──────────┴───────────────────────────────┘│
└─────────────────────────────────────────────┘
```

- **Sidebar**: `w-60`, `hidden md:flex`, fixed-height column with logo, nav links, theme toggle at bottom.
- **Mobile drawer**: `w-65`, `fixed inset-0 z-40`, hidden by default. Hamburger (`#menu-toggle`) opens; close button, backdrop click, or Escape key closes. `role="dialog" aria-modal="true"`. Focus returns to toggle on close.
- **Flash region**: `#flash-region`, `aria-live="polite" aria-atomic="false"`, sits between topbar and main content.
- **Main content**: `#main-content`, scrollable, padded `px-4 md:px-8 py-6`.
- **Footer**: Inside `<main>`, `mt-12 pt-4`, border-top, centered, `text-xs text-gray-500 dark:text-gray-400`.

---

## 6. Responsive Breakpoints

| Breakpoint | Tailwind prefix | Behavior |
|---|---|---|
| < 640px | (default / `sm:`) | Single column, compact padding |
| < 768px | `md:` | Mobile: topbar + drawer instead of sidebar |
| >= 768px | `md:` | Desktop: sidebar visible, wider padding (`px-8`) |

Grid patterns: `grid-cols-1 sm:grid-cols-2 lg:grid-cols-4` for stat cards.

---

## 7. Touch Targets

All interactive elements enforce 44px minimum height:

- `.btn`: `min-h-[44px]`
- `.nav-link`: `min-h-[44px]`
- `.segment span`: `min-h-[44px]`
- `.chip span`: `min-h-[44px]`
- `.form-input`: `min-h-[44px]`
- Mobile hamburger/close: `min-h-[44px] min-w-[44px]`

---

## 8. Components

All component classes defined in `@layer components` in `src/dashboard/static/css/input.css`.

### Stat card

```html
<div class="stat-card">
  <dt class="text-sm text-gray-500">Label</dt>
  <dd class="text-2xl font-bold">42</dd>
</div>
```

### Navigation

```html
<a href="/watched-items" class="nav-link nav-link-active">Watched Items</a>
```

`.nav-link-active` adds purple background/text tinting.

### Data table

Always wrap with a border/clip container — `overflow-hidden` is required to clip the `thead` background inside `rounded-lg`:

```html
<div class="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
  <table class="data-table w-full">
    <thead>
      <tr>
        <th scope="col" class="w-full">Name</th>
        <th scope="col">Status</th>
        <th scope="col"><span class="sr-only">Actions</span></th>
      </tr>
    </thead>
    <tbody id="table-tbody">
      <tr id="row-{id}">
        <td class="font-medium text-gray-900 dark:text-white">…</td>
        <td><span class="badge badge-active">Active</span></td>
        <td class="whitespace-nowrap">
          <div class="flex items-center gap-2 justify-end">
            <button class="btn btn-secondary text-xs min-h-[44px]"
                    aria-label="Edit {name}">Edit</button>
            <button class="btn btn-danger-outline text-xs min-h-[44px]"
                    aria-label="Delete {name}">Delete</button>
          </div>
        </td>
      </tr>
    </tbody>
  </table>
</div>
```

Sticky header with `z-10`, `box-shadow` separator. `th` is uppercase, `text-xs`, `tracking-wider`.

**Actions column pattern:**
- `<th scope="col"><span class="sr-only">Actions</span></th>` — the `<th>` itself must be visible (not `class="sr-only"`) so the header row has a cell above the buttons; the `<span class="sr-only">` hides the text visually while remaining accessible
- `class="w-full"` on the primary content column forces the actions column to shrink to its content width, aligning buttons to the right edge of the row
- `whitespace-nowrap` on the action `<td>` prevents button wrapping at narrow widths

**Empty-state row:**

```html
<tr id="table-empty-state">
  <td colspan="3" class="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
    No items yet.
  </td>
</tr>
```

Give the empty-state `<tr>` a stable `id`. When rows are added via HTMX `afterbegin` swap (bypassing a full refresh), remove the empty-state row client-side — see §9 for the guard pattern. Cancel/refresh returns the full partial, which re-renders the empty state naturally if the table is still empty.

**Inline table row editing:**

Edit forms that replace a row use `hx-swap="outerHTML"` targeting the `<tr>`:

```html
<!-- data row -->
<tr id="row-{id}">…</tr>

<!-- edit button -->
<button
  hx-get="/watched-items/{id}/notifications/{nc_id}/edit-form"
  hx-target="#row-{id}"
  hx-swap="outerHTML">Edit</button>

<!-- edit form partial — must be a <tr> with the same id -->
<tr id="row-{id}">
  <td colspan="3" class="p-4">
    <form hx-post="…" hx-target="#table-container" hx-swap="innerHTML">…</form>
  </td>
</tr>
```

Cancel returns either the single refreshed `<tr>` (preferred — avoids full table re-render) or the full partial. The form `<tr>` must carry the same `id` as the data row so the `outerHTML` swap lands correctly.

### Buttons

| Class | Appearance |
|---|---|
| `.btn` | Base: flex, rounded, 44px min-height, focus ring |
| `.btn-primary` | Purple bg, white text |
| `.btn-secondary` | White bg, gray border |
| `.btn-danger` | Red bg, white text |
| `.btn-danger-outline` | White bg, red border/text |
| `.btn-ghost` | Transparent, gray text, hover bg |
| `.btn-edit` | Light purple tint bg, purple border/text; used for inline field Edit actions |

```html
<button class="btn btn-primary">Save</button>
<button class="btn btn-danger-outline">Delete</button>
<button class="btn btn-edit py-1 px-3 text-sm min-h-0">Edit</button>
```

### Segmented control (single-select filter)

Radio-based control for mutually exclusive filter options (e.g., status). Renders as a connected bar with `input:checked + span` for pure-CSS active state.

```html
<form method="get" action="/target">
  <fieldset class="segment-group" role="radiogroup" aria-label="Filter by status">
    <label class="segment">
      <input type="radio" name="status" value="" checked>
      <span>All</span>
    </label>
    <label class="segment">
      <input type="radio" name="status" value="active">
      <span>Active</span>
    </label>
  </fieldset>
  <noscript><button type="submit" class="btn btn-secondary">Apply</button></noscript>
</form>
```

| State | Appearance |
|---|---|
| Inactive | Transparent bg, gray text |
| Active (`input:checked + span`) | `co-purple-600` bg, white text |
| Hover (inactive) | Light gray bg |
| Focus-visible | Purple outline ring |

Used in: domains list, watches list, domain detail watches, change detail diff toggle.

### Chip group (multi-select filter)

Checkbox-based control for toggling multiple filter options (e.g., event types). Renders as separate pills with gaps.

```html
<form method="get" action="/target">
  <fieldset class="chip-group" aria-label="Filter by event type">
    <legend class="sr-only">Event types</legend>
    <label class="chip">
      <input type="checkbox" name="event_type" value="watch.created" checked>
      <span>watch.created</span>
    </label>
  </fieldset>
  <noscript><button type="submit" class="btn btn-secondary">Apply</button></noscript>
</form>
```

| State | Appearance |
|---|---|
| Inactive | Transparent bg, gray text, gray border, fully rounded |
| Active (`input:checked + span`) | Light purple tint, purple border, purple text |
| Hover (inactive) | Light gray bg |
| Focus-visible | Purple outline ring |

Used in: audit log.

### Badges

```html
<span class="badge badge-active">Active</span>
<span class="badge badge-error">Error</span>
```

| Class | Color scheme | Semantic use |
|---|---|---|
| `.badge-active` | Green | Watched item/domain is actively monitored |
| `.badge-inactive` | Gray | Watched item/domain exists but is paused |
| `.badge-archived` | Amber | Watched item is archived (soft-deleted, restorable) |
| `.badge-error` | Red | Processing error state |
| `.badge-warning` | Orange | Non-blocking warning |
| `.badge-info` | Blue | Informational |

### Flash messages

```html
<div class="flash flash-success flex items-center justify-between mb-4"
     data-auto-dismiss role="alert">
  <span>Watched item created.</span>
  <button type="button" class="ms-4 text-current opacity-60 hover:opacity-100"
          aria-label="Dismiss" onclick="this.parentElement.remove()">
    <span aria-hidden="true">&times;</span>
  </button>
</div>
```

Levels: `flash-success`, `flash-error`, `flash-info`, `flash-warning`.

### Alerts (persistent, non-dismissible)

```html
<div class="alert alert-warning">No watches configured.</div>
```

Variants: `.alert-notice` (blue), `.alert-warning` (yellow).

### Danger zone

The `.danger-zone` component provides a row with label+description on the left and an action button on the right:

```html
<div class="danger-zone">
  <div>
    <div class="danger-zone__label">Archive this watched item</div>
    <div class="danger-zone__desc">Deactivates and marks as archived. Can be restored.</div>
  </div>
  <button class="btn btn-danger-outline">Archive</button>
</div>
```

**Archive / Restore workflow:** The watched-item detail page wraps the `.danger-zone` row in a `<section>` with a red `<h3>`. Archive (`btn-danger-outline`) sets `archived_at` and flips `is_active=False`; once archived, Restore (`btn-secondary`) clears the flag and re-activates. (There is no hard-delete UI — the single-entity WatchedItem uses archive/restore only, #191.)

### Detail grid

```html
<dl class="detail-grid">
  <dt>URL</dt>
  <dd>https://example.com</dd>
</dl>
```

Two-column `dt`/`dd` grid: `minmax(140px, max-content) 1fr`.

### Toggle switch

Boolean toggle that auto-saves on change (no Edit/Save step).

```html
<label class="toggle">
  <input type="hidden" name="value" value="false">
  <input type="checkbox" name="value" value="true" checked
    hx-post="/watched-items/{id}/field/{field}"
    hx-target="#field-{field}"
    hx-swap="outerHTML"
    hx-include="closest form">
  <span class="toggle__track"><span class="toggle__thumb"></span></span>
  <span class="toggle__label">Label text</span>
</label>
```

The hidden input provides the `false` value when the checkbox is unchecked. Starlette returns the **last** value for duplicate keys, so the checkbox value (`true`) wins when checked. The toggle submits immediately on change; no explicit Save button needed.

### Inline field edit/save/cancel

Detail pages (watches, domains) use per-field inline editing via HTMX. Each field row swaps between **view mode** and **edit mode** in place (`hx-swap="outerHTML"` on `#field-{name}`).

**Editing requires JS.** The Edit button is `<button type="button">` with only `hx-get` — no `<a href>` or `<form>` fallback. Without HTMX, fields remain read-only.

**View mode** — no disabled form controls; values render as plain content:
- Text/number: plain `<span>` with the value. Unit (e.g. `s`, `rows`) appended in muted text.
- URL: `<a class="link" target="_blank" rel="noopener noreferrer">` hyperlink, with a Copy button (left of Edit) that writes to the clipboard and shows a flash confirmation.
- Textarea: a `<div>` with the same border-radius and padding as the edit textarea, but a lighter border (`border-gray-200 dark:border-gray-700`) to signal read-only. Content rendered with `whitespace-pre-wrap` to preserve line breaks.
- Select: plain `<span>` showing the matching option label (resolved by iterating `field_options`).
- Toggle: renders immediately without an Edit step (auto-saves on change).

**Edit mode** — the actual form control appears with Save + Cancel buttons:
- Text fields: `<input type="text">` filling available width (`1fr` in the layout grid).
- URL fields: `<input type="url">`.
- Number fields: `<input type="number" class="w-28">` — fixed narrow width.
- Textarea: `<textarea rows="3" class="form-input">`.
- Select: `<select class="form-input w-32">`.

**Layout — text/number/URL fields:**

Wide (`sm+`): 3-column grid `[label(min(18rem,40%)) | value/input(1fr) | buttons(auto)]` — all on one row.
Narrow: label + buttons on row 1; value/input spans both columns on row 2 below.

```
sm+: [Label / Hint]  [Value or Input (fills)]  [Edit / Save Cancel]
xs:  [Label / Hint]                             [Edit / Save Cancel]
     [Value or Input (full width)]
```

**Layout — textarea fields:**

Two-column grid `[label+hint(1fr) | buttons(auto)]` on the header row; textarea/container spans full width below.

```
[Label / Hint]  [Edit / Save Cancel]
[Textarea or bordered container      ]
```

Buttons use `self-start pt-0.5` to align with the top of the label+hint stack.

**Route conventions:**
- `GET /watched-items/{id}/field/{name}` — returns field partial in view mode (cancel).
- `GET /watched-items/{id}/field/{name}?mode=edit` — returns field partial in edit mode.
- `POST /watched-items/{id}/field/{name}` — saves and returns field partial in view mode.

### Form controls

```html
<label class="form-label" for="url">URL</label>
<input class="form-input" id="url" name="url" type="url">
<p id="url-error" class="mt-1 text-xs text-red-600 dark:text-red-400" hidden></p>
```

**Hint + error `aria-describedby` pattern:**

```html
<label class="form-label" for="interval">Check Interval</label>
<input class="form-input" id="interval" name="interval"
  aria-describedby="interval-hint">
<p id="interval-hint" class="mt-1 text-xs text-gray-500 dark:text-gray-400">Format: 30s, 15m, 6h, 1d</p>
<p id="interval-error" class="mt-1 text-xs text-red-600 dark:text-red-400" hidden></p>
```

- **ID convention**: `{field_name}-hint` for hints, `{field_name}-error` for validation errors.
- `aria-describedby` references hint IDs statically in the template. Omit `aria-describedby` entirely when no hint exists.
- **Error wiring is dynamic**: validation JS adds the error ID to `aria-describedby` when showing the error, and removes it when clearing. This avoids screen readers announcing empty hidden elements.
- Error element is `hidden` by default; remove `hidden` and populate text when validation fails.

### Links

```html
<a href="/watched-items/1" class="link">View details</a>
```

Purple with underline on hover.

### Skip link

```html
<a class="skip-link" href="#main-content">Skip to main content</a>
```

`sr-only` until focused; appears top-left with purple background.

### Pagination (pattern — for future use)

Top bar with result count + page-size `<select>` (options: 25, 50, 100, 250). Sticky footer with prev/next. Not yet implemented as a component class.

### Modal (pattern — for future use)

Dialog overlay with focus trapping. Focus trap implementation deferred to #39.

---

## 9. HTMX Patterns

- **Detection pattern**: When a route needs to distinguish HTMX partials from full-page requests, use `request.headers.get("HX-Request") and not request.headers.get("HX-Boosted")`. The `HX-Boosted` guard prevents boosted navigation from receiving bare fragments.
- **OOB flash**: Set `flash_oob_level` and `flash_oob_message`, then `{% include "partials/flash_oob.html" %}`. Swaps into `#flash-region` via `hx-swap-oob="beforeend"`.
- **Loading states**: `.htmx-request` class (auto-applied by htmx) sets `opacity: 0.6`, `cursor: wait`, `pointer-events: none` on the element and child buttons/inputs/selects.
- **`aria-busy`**: `src/dashboard/static/js/htmx-a11y.js` sets `aria-busy="true"` on the swap target during `htmx:beforeRequest`, removes it on `htmx:afterSettle`.
- **Graceful degradation**: Routes return full-page template for non-HTMX requests, partial for HTMX — standard redirect fallback.

### `hx-on::` event handlers

`hx-on::event-name` (double colon) is the shorthand for HTMX internal events (`htmx:*`). Single colon (`hx-on:click`) is for native DOM events. Do not mix the forms:

```html
hx-on::before-request="…"   <!-- htmx:beforeRequest -->
hx-on::after-request="…"    <!-- htmx:afterRequest -->
hx-on::after-settle="…"     <!-- htmx:afterSettle -->
hx-on:click="…"             <!-- native click event -->
```

**Idempotency guard** — prevent a button from firing if its target already exists in the DOM:

```html
<button
  hx-get="/watched-items/{id}/notifications/add-row"
  hx-target="#tbody"
  hx-swap="afterbegin"
  hx-on::before-request="if(document.getElementById('add-row')){event.preventDefault();}">
  + Add
</button>
```

`event.preventDefault()` on `htmx:beforeRequest` cancels the request entirely. Use this whenever a button inserts a unique element and should be a no-op if that element is already present.

**Success guard for `after-request` DOM manipulation** — always check `event.detail.successful` before touching the DOM:

```html
hx-on::after-request="if(event.detail.successful){const e=document.getElementById('empty-state');if(e)e.remove();}"
```

Without the guard, DOM manipulation fires on error responses (4xx/5xx) too.

### Form error retargeting

When a form validation error should keep the form visible with an inline error message — rather than replacing it — return the form partial with `HX-Retarget` and `HX-Reswap` headers:

```python
return templates.TemplateResponse(
    "partials/notification_add_row.html",
    {"request": request, "watched_item": watched_item, "error": str(exc), ...},
    headers={"HX-Retarget": "#add-row", "HX-Reswap": "outerHTML"},
)
```

HTMX processes these response headers and redirects the swap to `#add-row` instead of the original form target. The form template must render the `error` variable inline:

```html
{% if error %}
<div class="text-sm text-red-600 dark:text-red-400" role="alert">{{ error }}</div>
{% endif %}
```

Use this pattern instead of OOB flash for validation errors that are specific to a form still in the page — flash is for transient success/failure messages, not inline field errors.

---

## 10. Flash / Notification UX

- **Inline flash**: `{% include "partials/flash.html" %}` — renders from `flash` context variable.
- **OOB flash**: `{% include "partials/flash_oob.html" %}` — used in HTMX partial responses. Set `flash_oob_level` and `flash_oob_message` before including.
- **Programmatic flash (JS)**: `window.watcher.showFlash(level, message)` — creates and appends a flash element to `#flash-region` from client-side JS. Wires up auto-dismiss and hover-pause. Use for purely client-side events (e.g., clipboard copy confirmation) where no server round-trip is needed.
- **Levels**: `success`, `error`, `info`, `warning`.
- **Auto-dismiss**: 5 seconds (`DISMISS_MS` in `app.js`). Hover pauses timer; mouseleave restarts.
- **Close button**: `&times;` with `aria-label="Dismiss"`, removes parent on click. Uses `ms-4` (logical margin-inline-start).
- **Animation**: `flash-in` keyframe — 0.2s fade + slide-up.
- **XSS prevention**: Flash messages rendered via Jinja2 auto-escaping (`{{ flash.message }}`). No raw/safe filter used on user content.

---

## 11. Accessibility (WCAG 2.1 AA)

- **Decorative emoji**: Wrapped in `<span aria-hidden="true">`.
- **Focus rings**: `focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-co-purple-600` (light) / `dark:focus-visible:outline-co-purple-400` (dark). Applied on `.btn`, `.form-input`, `.skip-link`.
- **Icon-only buttons**: Must have `aria-label` (e.g., theme toggle, hamburger, close, dismiss).
- **Contextual action buttons**: Table row action buttons include `aria-label` with the entity name for screen reader context (e.g., `aria-label="Deactivate {{ watched_item.name }}"`).
- **Table rows are not clickable**: Rows contain discrete `<a>` and `<button>` elements — no `tabindex="0"` or `role="link"` on `<tr>`. This avoids the nested interactive elements anti-pattern (buttons/links inside a link-role container). Keyboard users tab through the individual interactive elements within each row.
- **HTMX live regions**: `#flash-region` has `aria-live="polite" aria-atomic="false"`. `aria-busy` auto-managed by `htmx-a11y.js`.
- **Skip link**: `.skip-link` — first element in `<body>`, targets `#main-content`.
- **Reduced motion**: Global `@media (prefers-reduced-motion: reduce)` forces `animation-duration` and `transition-duration` to `0.01ms`.
- **Muted text minimum**: `text-gray-500` (light) / `dark:text-gray-400` (dark) — used for secondary labels, table headers, footer.
- **No `title` attributes**: Tooltips not used; all info in visible text or `aria-label`.
- **Mobile drawer**: `role="dialog" aria-modal="true" aria-label="Navigation menu"`. Escape closes. Focus returns to trigger.

---

## 12. Internationalization Groundwork

- `<html lang="en" dir="ltr">` — explicit language and direction.
- `<meta charset="utf-8">` — first element in `<head>`.
- **CSS logical properties**: Templates use `ms-*` / `me-*` (margin-inline-start/end), not `ml-*` / `mr-*`. Example: close button uses `ms-4`.
- **NFC normalization**: Planned for content processing; not yet enforced in templates.

---

## 13. Performance

- **No CDN scripts**: All JS vendored locally in `src/dashboard/static/js/` (htmx, app, dark-mode, htmx-a11y).
- **`defer`**: All `<script>` tags use `defer` — except the inline FOUC-prevention script in `<head>` (must run synchronously).
- **Cache-busting**: `BUILD_ID` env var (default `"dev"`) exposed as `{{ build_id }}` in templates. All static assets loaded with `?v={{ build_id }}`. Set in `src/dashboard/__init__.py`.
- **System font stack**: No custom fonts loaded. Tailwind default font stack.
- **Explicit image dimensions**: All `<img>` tags include `width` and `height` attributes to prevent layout shift.
- **Pre-built Tailwind**: Compile CSS before deploy. Source: `src/dashboard/static/css/input.css` → Output: `src/dashboard/static/css/output.css`.

---

## 14. Overriding Vendored CSS

Tailwind v4 emits author rules inside cascade layers (`theme`, `base`, `components`, `utilities`). Per the CSS spec, **unlayered rules beat layered rules of any specificity**, so a third-party stylesheet loaded as raw `<link>` will override anything we write in `@layer components` regardless of how specific our selector is. Adding `!important` works but is a smell that compounds with each new vendor library.

**Pattern: place vendor CSS in a low-priority `vendor` layer.**

1. **Layer order is established by `input.css`.** The first directive is `@layer vendor;`, declared *before* `@import "tailwindcss";`. CSS layer order follows first-appearance, so `vendor` becomes the lowest-priority layer; Tailwind's own layers (and our `@layer components` overrides) all sort above it.

2. **`scripts/build-css.sh` wraps each vendor file in `@layer vendor { … }`.** For every `src/dashboard/static/css/vendor/*.min.css`, the build emits a `*.layered.css` sibling with the contents wrapped in a `vendor` layer block. Any leading `@charset` / `@import` directives are hoisted above the wrapper (CSS spec requires them at the top of the file); `@import` directives get a `layer(vendor)` suffix so the imported sheet sorts in the vendor layer. The wrapping is regenerated on every build — never edit the `*.layered.css` files by hand. **Note:** `--watch` mode only runs the wrap step once at startup; rerun `bash scripts/build-css.sh` after updating a vendor file mid-watch.

3. **Page templates load the `*.layered.css` variant**, not `*.min.css`. Example: `change_detail.html` loads `vendor/diff2html.layered.css`. The original minified file stays in `vendor/` as the source of truth and is checked into git as-is.

4. **Override rules go in `@layer components`** in `input.css`, with normal specificity and no `!important`. Example:

   ```css
   @layer components {
     .diff-mount .d2h-tag { display: none; }
   }
   ```

This pattern preserves page-scoped loading of vendor CSS (no global bundle bloat) and scales to any future vendored UI (Monaco, additional widgets) — drop the `*.min.css` into `vendor/`, link the `*.layered.css` from the page that needs it, write overrides in `@layer components` without `!important`.
