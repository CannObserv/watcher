# watcher — Style Guide

Authoritative reference for the dashboard UI. Documents what IS implemented, not aspirations. The component library and the
HTMX/flash interaction patterns live in [UI.md](UI.md).

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

- **Mechanism**: Tailwind `darkMode: class` via `@custom-variant dark (&:where(.dark, .dark *));` in `input.css`. **Purely class-based** — there is no `@media (prefers-color-scheme)` dark path, so `<html>` carries `.dark` (dark) or no class (light); "system" is resolved to one of those by JS, not by CSS.
- **localStorage key**: `watcher-color-scheme` — three states:

  | Value | Behavior |
  |---|---|
  | `"light"` | Force light (no `.dark` class) |
  | `"dark"` | Force dark (`.dark` on `<html>`) |
  | absent | **System** — follow OS `prefers-color-scheme`; the third state, written by *clearing* the key (`removeItem`), not a `"system"` literal |

- **Three-state toggle**: clicking a theme-toggle cycles the *stored* preference **light → system → dark → light**. The cycle is driven off the stored value, not the rendered class — `system` (OS light) and explicit `light` both render classless and are indistinguishable by class alone. Reaching **system** clears the key, so the FOUC script needs no extra case (absent already means follow-OS). Because the dark variant is class-only, `dark-mode.js` resolves system → `.dark` via `matchMedia` at apply time and re-resolves on OS theme changes while system is active; it dispatches `watcher:theme-changed` when the rendered scheme flips (consumed by `diff-viewer.js`).
- **FOUC prevention**: Inline `<script>` in `<head>` (before stylesheet) reads localStorage + `prefers-color-scheme` and adds `.dark` to `<html>` synchronously when stored `"dark"` or absent-and-OS-dark.
- **`<noscript>` fallback**: `<style>` block applies `color-scheme: dark` via `prefers-color-scheme` media query when JS is disabled.
- **Toggle buttons**: `button#theme-toggle` (desktop sidebar) and `button#theme-toggle-mobile` (mobile topbar). Both render a neutral default (empty `[data-theme-icon]` span + `aria-label="Color theme"`); `dark-mode.js` (via its `META` map — the single source of truth) populates the **current-state** affordance on load and after each `htmx:afterSettle`: ☀ Light · ◑ System · ☽ Dark, with an `aria-label` naming the state and the next action. A CSS placeholder (`[data-theme-icon]:empty::before { content: "◑" }`) shows the neutral system glyph until JS fills the span, so the button never renders blank pre-/no-JS.
- **Implementation**: `src/dashboard/static/js/dark-mode.js` — document-level click delegation (registered once; defensive against HTMX swaps — the toggle buttons live in persistent chrome and watcher uses no hx-boost) cycling the stored preference; latches an in-memory fallback if `localStorage` throws. Behavior covered by `tests/dashboard/js/dark-mode.test.mjs` (run in-suite via `tests/dashboard/test_dark_mode_js.py`).

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

All interactive elements enforce the WCAG 2.1 AA 44px minimum height.

**The rule (one idiom — #203): component classes own the 44px guarantee.**
These bake in `min-h-[44px]`, so **never restate it on an element that carries
one**:

- `.btn` (and every `.btn-*` variant)
- `.nav-link`
- `.form-input`
- `.toggle`
- `.segment span` (the segmented-control option — height is on the inner `span`, not the bare `.segment` label)
- `.chip span` (the chip-group option — height is on the inner `span`, not the bare `.chip` label)

Use explicit `min-h-[44px]` **only on bare interactive elements that have no
component class** — `<a>`, `<label>` wrapping a checkbox/radio, a
component-less `<button>` (e.g. a sortable column header). These are the *only*
places it belongs; the explicit token is then the signal "this element has no
component class."

Exceptions:

- **Square icon buttons** (mobile hamburger/close) add `min-w-[44px]` for width
  — `.btn` guarantees height, not width. Keep `min-w-[44px]`, drop the
  redundant `min-h-[44px]`: `class="btn btn-ghost p-2 min-w-[44px]"`.
- **`.chip-xs`** is a deliberate sub-44px class for dense token-insert chips
  (notification variable chips). It is the only sanctioned target under 44px;
  do not use it for primary actions.

Never use `min-h-0` to shrink a component target below 44px — it is a latent
a11y violation.

**Guard:** `tests/dashboard/test_touch_targets.py` (runs in the default `uv run
pytest` suite) and `scripts/check-touch-targets.sh` fail on redundant
`min-h-[44px]` on a `.btn` or on any `min-h-0` in a template.

---

## 8. Accessibility (WCAG 2.1 AA)

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

## 9. Internationalization Groundwork

- `<html lang="en" dir="ltr">` — explicit language and direction.
- `<meta charset="utf-8">` — first element in `<head>`.
- **CSS logical properties**: Templates use `ms-*` / `me-*` (margin-inline-start/end), not `ml-*` / `mr-*`. Example: close button uses `ms-4`.
- **NFC normalization**: Planned for content processing; not yet enforced in templates.

---

## 10. Performance

- **No CDN scripts**: All JS vendored locally in `src/dashboard/static/js/` (htmx, app, dark-mode, htmx-a11y).
- **`defer`**: All `<script>` tags use `defer` — except the inline FOUC-prevention script in `<head>` (must run synchronously).
- **Cache-busting**: `BUILD_ID` env var (default `"dev"`) exposed as `{{ build_id }}` in templates. All static assets loaded with `?v={{ build_id }}`. Set in `src/dashboard/__init__.py`.
- **System font stack**: No custom fonts loaded. Tailwind default font stack.
- **Explicit image dimensions**: All `<img>` tags include `width` and `height` attributes to prevent layout shift.
- **Pre-built Tailwind**: Compile CSS before deploy. Source: `src/dashboard/static/css/input.css` → Output: `src/dashboard/static/css/output.css`.

---

## 11. Overriding Vendored CSS

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
