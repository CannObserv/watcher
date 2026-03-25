# Accessibility Tier 2: Form Hints & Row Keyboard Discoverability

**Date:** 2026-03-25
**Issue:** #35 (scoped — modal focus trapping moved to #39)

## Goal

Wire `aria-describedby` to form hints and error scaffolds. Improve keyboard discoverability of interactive elements in table rows.

## Approved Approach

### 1. Form hint + error `aria-describedby`

Add `aria-describedby` linking inputs to their hint text and a scaffolded error slot.

**Pattern:**
```html
<input id="interval" aria-describedby="interval-hint interval-error" ... />
<p id="interval-hint" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
  Format: 30s, 15m, 6h, 1d
</p>
<p id="interval-error" class="mt-1 text-xs text-red-600 dark:text-red-400" hidden></p>
```

**Conventions:**
- Hint ID: `{field_name}-hint`
- Error ID: `{field_name}-error`
- Error element is `hidden` by default — no visual change until validation populates it
- Both referenced in `aria-describedby` so screen readers announce context on focus

**Files:** `watch_form.html`, `notification_config_form.html` (if applicable), STYLE.md.

### 2. Table row keyboard discoverability

Keep rows non-clickable. Improve context and tab flow for existing interactive elements within rows.

**Changes:**
- Add descriptive `aria-label` to action buttons (e.g. `aria-label="Deactivate watch {{ watch.name }}"`)
- Ensure link/button tab order within rows is logical (view link → action buttons)
- Confirm `focus-visible` ring styling applies to links/buttons in table cells

**Files:** `watch_row.html`, `change_row.html`, other row partials, STYLE.md.

### Why not row-level clickability?

Making entire rows `role="link"` with `tabindex="0"` creates nested interactive elements (links and buttons inside a link-role container), which is an accessibility anti-pattern. Keeping discrete interactive elements is cleaner and more predictable for assistive technology.

## Testing Strategy

- Manual keyboard-only walkthrough of watch form and watch list
- Template-only changes — no automated tests needed

## Out of Scope

- Modal focus trapping (#39)
- Row-level click handlers
- Inline validation logic (only the `aria-describedby` scaffold)
