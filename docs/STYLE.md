# watcher — Style Guide

Authoritative reference for the dashboard UI. Documents what IS implemented, not aspirations.

---

## 1. Brand Assets

| Asset | Path | Details |
|---|---|---|
| Icon (SVG) | `src/dashboard/static/images/cannabis_observer-icon-square.svg` | Square; used at 28x28 (sidebar) and 16x16 (footer) |
| Favicon | Same SVG, served as `type="image/svg+xml"` | `<link rel="icon">` in `base.html` |
| Footer emoji | `🌱🏛️🔍` | Wrapped in `<span aria-hidden="true">` — purely decorative |

All `<img>` tags for the icon carry `alt="" aria-hidden="true"` and explicit `width`/`height`.

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
- `.filter-pill`: `min-h-[44px]`
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
<a href="/watches" class="nav-link nav-link-active">Watches</a>
```

`.nav-link-active` adds purple background/text tinting.

### Data table

```html
<table class="data-table">
  <thead><tr><th>Column</th></tr></thead>
  <tbody><tr><td>Value</td></tr></tbody>
</table>
```

Sticky header with `z-10`, `box-shadow` separator. `th` is uppercase, `text-xs`, `tracking-wider`.

### Buttons

| Class | Appearance |
|---|---|
| `.btn` | Base: flex, rounded, 44px min-height, focus ring |
| `.btn-primary` | Purple bg, white text |
| `.btn-secondary` | White bg, gray border |
| `.btn-danger` | Red bg, white text |
| `.btn-danger-outline` | White bg, red border/text |
| `.btn-ghost` | Transparent, gray text, hover bg |

```html
<button class="btn btn-primary">Save</button>
<button class="btn btn-danger-outline">Delete</button>
```

### Filter pill

```html
<a href="?status=active" class="filter-pill">Active</a>
```

### Badges

```html
<span class="badge badge-active">Active</span>
<span class="badge badge-error">Error</span>
```

| Class | Color scheme |
|---|---|
| `.badge-active` | Green |
| `.badge-inactive` | Gray |
| `.badge-error` | Red |
| `.badge-warning` | Orange |
| `.badge-info` | Blue |

### Flash messages

```html
<div class="flash flash-success flex items-center justify-between mb-4"
     data-auto-dismiss role="alert">
  <span>Watch created.</span>
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

```html
<div class="danger-zone">
  <div>
    <div class="danger-zone__label">Delete this watch</div>
    <div class="danger-zone__desc">This action cannot be undone.</div>
  </div>
  <button class="btn btn-danger">Delete</button>
</div>
```

### Detail grid

```html
<dl class="detail-grid">
  <dt>URL</dt>
  <dd>https://example.com</dd>
</dl>
```

Two-column `dt`/`dd` grid: `minmax(140px, max-content) 1fr`.

### Form controls

```html
<label class="form-label" for="url">URL</label>
<input class="form-input" id="url" name="url" type="url">
```

### Links

```html
<a href="/watches/1" class="link">View details</a>
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

Dialog overlay with focus trapping. Focus trap implementation deferred to #35.

---

## 9. HTMX Patterns

- **Detection pattern**: When a route needs to distinguish HTMX partials from full-page requests, use `request.headers.get("HX-Request") and not request.headers.get("HX-Boosted")`. The `HX-Boosted` guard prevents boosted navigation from receiving bare fragments.
- **OOB flash**: Set `flash_oob_level` and `flash_oob_message`, then `{% include "partials/flash_oob.html" %}`. Swaps into `#flash-region` via `hx-swap-oob="beforeend"`.
- **Loading states**: `.htmx-request` class (auto-applied by htmx) sets `opacity: 0.6`, `cursor: wait`, `pointer-events: none` on the element and child buttons/inputs/selects.
- **`aria-busy`**: `src/dashboard/static/js/htmx-a11y.js` sets `aria-busy="true"` on the swap target during `htmx:beforeRequest`, removes it on `htmx:afterSettle`.
- **Graceful degradation**: Routes return full-page template for non-HTMX requests, partial for HTMX — standard redirect fallback.

---

## 10. Flash / Notification UX

- **Inline flash**: `{% include "partials/flash.html" %}` — renders from `flash` context variable.
- **OOB flash**: `{% include "partials/flash_oob.html" %}` — used in HTMX partial responses. Set `flash_oob_level` and `flash_oob_message` before including.
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
