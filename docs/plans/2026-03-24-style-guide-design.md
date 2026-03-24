# Style Guide & Full Reskin — Design

**Date:** 2026-03-24

## Goal

Establish authoritative UI/UX guidelines for the watcher dashboard in `docs/STYLE.md`, update `AGENTS.md` with style conventions, and reskin all existing pages/partials to match. Align with Cannabis Observer sibling projects (power-map, address-validator, wslcb-licensing-tracker).

## Approved Approach

### Brand Identity

Adopt full Cannabis Observer brand:
- `co-purple` (#6d4488) as primary UI accent
- `co-green` (#8cbe69) reserved, not used as UI accent
- Brand icon in sidebar header and footer
- Footer emoji triad: 🌱🏛️🔍 (wrapped in `aria-hidden`)
- Favicon: purple magnifying glass matching brand color

### Color System

Define via `@theme` block in `input.css` (Tailwind v4 CSS-based config — no `tailwind.config.js`):

| Token | Hex | Purpose |
|---|---|---|
| `co-purple` (DEFAULT/600) | `#6d4488` | Primary accent |
| `co-purple-50` | `#f5f0f8` | Active nav bg (light) |
| `co-purple-100` | `#ebe1f1` | Focus rings (dark) |
| `co-purple-700` | `#5a3870` | Hover states |
| `co-purple-800` | `#472c59` | Active nav bg (dark) |
| `co-green` | `#8cbe69` | Reserved |

Semantic status colors (green/yellow/red/blue) stay as Tailwind defaults — never replaced with brand colors.

### Dark Mode

- Tailwind `darkMode: 'class'` — `<html class="dark">`
- Toggle button in sidebar/topbar: sun/moon icon
- `localStorage` key: `watcher-color-scheme` (values: `"dark"`, `"light"`, absent = OS)
- FOUC prevention: inline `<script>` in `<head>` before stylesheet
- `@media (prefers-color-scheme: dark)` fallback for no-JS

### Layout

- **Desktop (≥768px):** 240px fixed sidebar + `1fr` main content area
- **Mobile (<768px):** Full-width single column, sidebar becomes off-screen drawer (hamburger toggle)
- Sidebar: brand icon + "watcher" title, nav links, dark mode toggle
- Main: scrollable content area with footer inside
- Sticky topbar on mobile with hamburger + page title

### Accessibility (This Phase)

- Skip link targeting `#main-content`
- ARIA landmarks: `<nav aria-label="...">`, `<main id="main-content">`
- Focus rings: `focus-visible:ring-2 ring-co-purple-700` / `dark:ring-co-purple-100`
- 44px minimum touch targets on all interactive elements
- Decorative emoji wrapped in `<span aria-hidden="true">`
- HTMX swap targets: `aria-live="polite" aria-atomic="false"`
- `aria-busy="true"` auto-set during HTMX requests
- Reduced motion: `@media (prefers-reduced-motion: reduce)` collapses all animations/transitions

### Accessibility (Deferred — separate issue)

- Modal focus trapping (capture trigger, cycle Tab, Escape to close)
- `aria-describedby` on form hint text
- Interactive row focus management (`tabindex="0" role="link"`)

### Internationalization (This Phase — Minimal)

- `<html lang="en" dir="ltr">`
- `<meta charset="utf-8">`
- CSS logical properties for all new/reskinned code (`margin-inline-start` not `margin-left`)
- NFC normalization for DB text fields

### Internationalization (Deferred — separate issue)

- Move user-facing strings out of Python route handlers into templates
- RTL CSS rules (`[dir="rtl"]`)
- Babel/gettext integration with `_()` wrapping
- Language switcher UI

### Performance

- Pre-built Tailwind only (no CDN, no runtime JIT)
- System font stack: `ui-sans-serif, system-ui, sans-serif`
- `defer` on all non-critical `<script>` tags (exception: FOUC prevention)
- `BUILD_ID` env var for cache-busting (`?v={{ build_id }}`):
  - Set by systemd `ExecStartPre` at deploy (git SHA)
  - App reads env var, falls back to `"dev"` if unset
  - Exposed as Jinja2 global and in `/health` endpoint
- Explicit `width`/`height` on all images
- No CDN scripts — all JS vendored locally

### HTMX Patterns

- **OOB flash injection:** Flash macro emits `hx-swap-oob="beforeend"` into `#flash-region`
- **CSS loading states:** `.htmx-request` sets `opacity: 0.6; cursor: wait; pointer-events: none`
- **aria-busy:** Global `htmx:beforeRequest`/`htmx:afterSettle` listeners auto-set `aria-busy="true"` on swap targets
- **Graceful degradation:** All mutation routes provide non-HTMX `RedirectResponse` fallback
- **`_is_htmx()` helper:** Check `HX-Request` header with `HX-Boosted` guard

### Components

Full component library documented in STYLE.md:

1. **Stat cards** — metric display with label, value, optional trend
2. **Data tables** — sticky thead, row hover, responsive overflow-x-auto
3. **Status badges** — semantic colors (active/inactive/error/backoff), never brand-colored
4. **Flash messages** — success/info/warning/error levels, auto-dismiss (5s), hover pause, OOB injection
5. **Alert banners** — persistent, non-dismissible notices
6. **Filter controls** — button pills, search inputs, stacking on mobile
7. **Forms** — labeled inputs, focus rings, max-width constraint
8. **Diff views** — side-by-side with color-coded change types
9. **Danger zones** — destructive action containers with warning description + danger button
10. **Pagination** — top bar + sticky footer, page-size select (25/50/100/250)
11. **Detail grids** — `<dl>` label/value pairs, 2-col on desktop, 1-col on mobile
12. **Modals** — documented pattern (focus trapping deferred to accessibility tier 2)

### Responsive Breakpoints

| Breakpoint | Changes |
|---|---|
| `<768px` (mobile) | Sidebar → drawer, hamburger shown, grid collapses, padding reduces |
| `<640px` | Filter controls stack, detail grids collapse to 1-col |
| `≥768px` (md) | Desktop sidebar visible, 2-col layouts |
| `≥1024px` (lg) | 3-4 col stat grids |

### Implementation Scope

Full reskin of all existing pages and partials:
- `base.html` — sidebar, dark mode, skip link, ARIA, FOUC script, BUILD_ID cache-busting
- `pages/dashboard.html` — brand colors, stat cards, dark variants
- `pages/watches.html` — brand colors, filter buttons, table styling
- `pages/watch_detail.html` — detail grid, danger zone, dark variants
- `pages/watch_form.html` — form styling, focus rings
- `pages/domains.html` — table reskin, badge colors
- `pages/system.html` — system health cards
- `pages/audit_log.html` — table reskin
- `pages/change_detail.html` — diff view
- `pages/404.html` — brand styling
- All partials — dark variants, brand colors, accessibility attributes
- `input.css` — component classes, dark mode tokens, reduced motion
- `input.css` `@theme` block — brand colors (Tailwind v4 CSS-based config, no `tailwind.config.js`)
- `app.js` — dark mode toggle, HTMX aria-busy listeners, flash auto-dismiss
- New: FOUC prevention script, flash macro, `_is_htmx()` helper
- New: `BUILD_ID` env var reading + Jinja2 global

## Key Decisions

1. **Tailwind `dark:` variants over CSS custom properties** — consistent with address-validator and wslcb-licensing-tracker; leverages Tailwind tooling the project already uses
2. **`BUILD_ID` env var over git subprocess** — follows wslcb-licensing-tracker#110; no runtime git dependency
3. **Minimal i18n now** — logical properties and `lang`/`dir` only; string extraction and Babel deferred
4. **Full sidebar pattern** — 240px desktop, drawer on mobile; matches sibling project UX
5. **Semantic status colors never brand-colored** — green/yellow/red/blue must remain universally recognizable

## Out of Scope

- Modal focus trapping (deferred accessibility issue)
- Form hint `aria-describedby` (deferred accessibility issue)
- Interactive row focus management (deferred accessibility issue)
- String extraction / Babel / gettext (deferred i18n issue)
- RTL CSS rules (deferred i18n issue)
- Language switcher (deferred i18n issue)
- Data visualizations / charts (future work)
- Custom font loading (staying with system stack)
