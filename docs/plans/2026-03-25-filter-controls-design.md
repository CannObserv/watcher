# Filter Controls Redesign

**Date:** 2026-03-25
**Status:** Approved

## Goal

Replace the ambiguous `.filter-pill` button pattern with two purpose-built controls that clearly communicate selection behavior: a segmented control for single-select status filters, and a chip group for multi-select event type filters. Establish a project-wide standard documented in STYLE.md.

## Problem

The current `.filter-pill` buttons have several UX issues:

- Button styling suggests "action" (navigate, submit), not "filter selection"
- `.filter-pill-active` class is referenced in templates but has **no CSS definition** — zero visual feedback on the active filter
- "All" option reads as if it should activate all other buttons
- Nothing communicates whether multi-select is possible
- Gray default styling reads as disabled/inactive

## Approved Approach

Two distinct controls for two distinct behaviors:

### Segmented Control (single-select status filters)

**Used in:** domains list, watches list, domain detail watches sub-table

**HTML:** `<fieldset>` with visually-hidden `<legend>`, `<label>` elements wrapping `<input type="radio">`. Radio inputs are visually hidden; labels styled as connected segments.

```html
<form method="get">
  <fieldset class="segment-group" role="radiogroup" aria-label="Filter by status">
    <label class="segment">
      <input type="radio" name="status" value="" checked>
      <span>All</span>
    </label>
    <label class="segment">
      <input type="radio" name="status" value="active">
      <span>Active</span>
    </label>
    <!-- ... -->
  </fieldset>
  <noscript><button type="submit" class="btn btn-secondary">Apply</button></noscript>
</form>
```

**Visual treatment:**
- Container: single connected bar with `border border-gray-300 dark:border-gray-600 rounded-lg`
- Segments separated by internal borders (not gaps)
- Inactive: transparent bg, `text-gray-600 dark:text-gray-400`
- Active (`input:checked + span`): `bg-co-purple-600 text-white` — solid purple fill
- Hover (inactive): `bg-gray-100 dark:bg-gray-700`
- Focus-visible: purple outline ring (project convention)
- Min-height 44px per segment (WCAG touch target)
- First/last segments: `rounded-l-lg` / `rounded-r-lg`

**No-JS fallback:** `<form method="get">` with `<noscript>` submit button. HTMX `hx-get` on radio change for progressive enhancement.

### Chip Group (multi-select event type filters)

**Used in:** audit log

**HTML:** `<fieldset>` with `<label>` elements wrapping `<input type="checkbox">`. Chips are disconnected (gap between them), reinforcing "pick many."

```html
<form method="get">
  <fieldset class="chip-group" aria-label="Filter by event type">
    <legend class="sr-only">Event types</legend>
    <label class="chip">
      <input type="checkbox" name="event_type" value="watch.created" checked>
      <span>watch.created</span>
    </label>
    <!-- ... -->
  </fieldset>
  <noscript><button type="submit" class="btn btn-secondary">Apply</button></noscript>
</form>
```

**Visual treatment:**
- Container: `flex flex-wrap gap-2` — no connected border
- Inactive: `border border-gray-300 dark:border-gray-600 bg-transparent text-gray-600 dark:text-gray-400 rounded-full` — outlined pill
- Active (`input:checked + span`): `bg-co-purple-100 dark:bg-co-purple-900 border-co-purple-600 text-co-purple-700 dark:text-co-purple-300` — tinted purple fill + purple border
- Hover (inactive): `bg-gray-100 dark:bg-gray-700`
- Focus-visible: purple outline ring
- Min-height 44px, `rounded-full` for pill shape
- No checkmark icon — fill color shift is sufficient

**No-JS fallback:** Same `<form method="get">` + `<noscript>` pattern.

### Visual Distinction Summary

| Property        | Segmented Control      | Chip Group                    |
|-----------------|------------------------|-------------------------------|
| Shape           | Connected bar, rounded | Separate pills, fully rounded |
| Active state    | Solid purple, white text | Light purple tint, purple border/text |
| Selection       | Single (radio)         | Multiple (checkbox)           |
| Affordance      | "Pick one"             | "Toggle any"                  |

## CSS Classes

| Class            | Purpose                              |
|------------------|--------------------------------------|
| `.segment-group` | Flex container, border, rounded bar  |
| `.segment`       | Individual radio label               |
| `.chip-group`    | Flex wrap container with gap         |
| `.chip`          | Individual checkbox label            |

## Migration

- **Domains list, watches list, domain detail:** replace `.filter-pill` with `.segment-group` / `.segment`
- **Audit log:** replace `.filter-pill` with `.chip-group` / `.chip`
- **Remove** `.filter-pill` from `input.css` entirely (no survivors)
- **Update** `STYLE.md` with the new pattern documentation

## Key Decisions

1. **Segmented control for single-select, chip group for multi-select** — visual distinction reinforces behavioral difference
2. **Radio/checkbox semantics** — native form controls provide accessibility and no-JS fallback for free
3. **Pure CSS active states** via `input:checked + span` — no JavaScript required for visual feedback
4. **Solid vs. tinted purple** — segmented uses solid `co-purple-600` fill; chips use lighter tint to differentiate
5. **Remove `.filter-pill` entirely** — clean break, no legacy class

## Out of Scope

- Audit log multi-select backend changes (currently single-select; chip group enables future multi-select)
- Filter persistence across page navigations (e.g., localStorage)
- Animated transitions between states
