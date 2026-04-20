# Notifications UI Redesign — Compose & Preview

**Date:** 2026-04-20
**Status:** Approved
**Supersedes in-flight:** #97 (per-event `title_template`/`body_template`) — solved structurally by this redesign.

## Goal

The notification template/config UI has grown dense: one long vertical form with nine additive toggles, two Jinja textareas, and eight nested per-event override accordions (each with nine more toggles). #97 would add another 16 textareas. Before piling on, step back and redesign for ease of use, legibility, and intuitiveness — with the explicit expectation that most users coast on defaults and only a small fraction ever customize.

## Problem Statement

User-ranked pain points (priority order):

1. **Defaults are invisible and uneditable.** Default titles/bodies live in `events.py` Python code; users can't see what they're starting from, let alone edit it.
2. **Events vs. customization feel tangled.** Picking *which* events to subscribe to is conceptually distinct from customizing *how each one renders*, but the form mixes them.
3. **No realtime feedback.** Flipping toggles produces no visible change until a real event fires.
4. **Cognitive load.** The full form is overwhelming — everything is presented at once with no progressive disclosure.
5. **Template variables are undiscoverable.** Jinja textareas have no hint about what `{{ vars }}` are usable.

Audience: multiple end users in-org. Design principle: **simple in front, power hidden behind a door.**

## Approved Approach — "Compose & Preview"

A two-column editor (desktop) that degrades to single-column with preview below (embedded/mobile). The form is broken into four distinct cards stacked vertically; a live preview renders on the right. Defaults are surfaced as editable starting values; per-event overrides are an optional drill-down.

### Layout

**Desktop (`lg:` breakpoint):**
```
┌──────────────────────────┬────────────────────┐
│ Basics                   │                    │
├──────────────────────────┤   Preview          │
│ Subscribe                │   (sticky)         │
├──────────────────────────┤                    │
│ Content                  │   Event: [Change   │
│  — additive toggles      │    Detected ▾]     │
│  — default title/body    │                    │
├──────────────────────────┤   ┌──────────────┐ │
│ Per-event overrides      │   │ title        │ │
│  (empty / cards)         │   │ body         │ │
├──────────────────────────┤   └──────────────┘ │
│ [Save] [Cancel]          │                    │
└──────────────────────────┴────────────────────┘
```

**Narrow/embedded contexts:** single column; preview stacked below the form (optionally collapsed to "Show preview" link in the tightest add-row case).

### Section 1 — Basics
Title, Apprise URL (keeps existing plugin picker / raw URL form), global-default toggle (template only).

### Section 2 — Subscribe
Event checkboxes in a 2-column grid. Once initial selection made, collapses to a chip-row summary ("3 events — Change Detected, Watch Error, Watch Recovered") with inline [Edit].

### Section 3 — Content (defaults)
Three blocks:

1. **Additive toggles** — the existing 9 checkboxes + `diff_snippet_lines`, regrouped under plain headings (`Changes` / `Context` / `Link`). No behavior change.
2. **"Default title" block** — read-only live-rendered preview of the title for the current preview event. `[Edit template ▾]` reveals a Jinja textarea pre-filled with the default Jinja source (`{{ event_label }}: {{ watch_name }}`), plus variable chips. Inline "Reset to default" and toggle-mute notice (Section 5).
3. **"Default body" block** — identical pattern. Body pre-fill includes the event default **plus** Jinja snippets for every currently-enabled additive toggle, stitched together. Rationale: toggles are the starting point, template is the end state — the user gets a full composed Jinja string to tweak, not a blank textarea.

Once a custom template exists, toggles are visually muted with: "Toggles are baked into your template. [Regenerate from toggles] · [Discard template]". Toggle state is still preserved in form so Regenerate works and Discard reverts cleanly.

### Section 4 — Per-event overrides (drill-down)
Empty state with `[+ Add override]` button; populated state shows one card per customized event. Each card is structurally identical to the Content card — same additive toggles, same [Edit template ▾] controls — but scoped to that event.

Adding an override:
- Button opens a picker of **subscribed events not yet overridden**.
- On selection, a new card is inserted, **pre-populated by copying the current default state** (toggles + any template overrides). User tweaks from there.
- Button disabled with hint if no events are available.

Removing an override: `[× Remove]` drops the card + clears that event's form fields.

**#97 is solved for free** — per-event `title_template` / `body_template` live inside the [Edit template ▾] controls of each override card.

### Section 5 — Variable discoverability
Chip row above each expanded Jinja textarea; click inserts `{{ name }}` at cursor (vanilla JS). `[See all variables]` expands an inline reference drawer with variables organized by scope (Always / `change_detected`-only / `watch_error`-only / Contextual).

Source of truth: a `TEMPLATE_VARIABLES` list in `default_templates.py`.

### Section 6 — Live preview

**Endpoint:** `POST /notifications/preview` — stateless, dashboard-auth, takes the full form state via `hx-include="closest form"` + a `preview_event` field selecting which event to simulate.

**Response:** HTML fragment (`partials/notification_preview.html`) with rendered title + body card, via the same `build_body()` / title-template pipeline the dispatcher uses.

**Wiring:**
- Form-level `hx-trigger="change delay:300ms, keyup delay:500ms"` → debounced re-render.
- Event selector `<select>` triggers on `change`; lists only subscribed events; marks overridden events with a small indicator.

**Mock data:** `src/core/notifications/preview_fixtures.py` holds a `MOCK_EVENT_FIXTURES` dict keyed by event type; `build_preview_event(event_type)` returns a well-formed `WatchEvent`. No real-watch data source in v1.

**Error surfacing:** preview uses a new `render_template_strict()` that raises on Jinja errors; endpoint catches and renders a red error card with line number and message. The existing `render_template()` (swallow + fallback) stays unchanged for dispatch — don't break notifications.

## Key Decisions and Rationale

### Unify default rendering — no "faithful mirror"
Remove `WatchEvent.title` / `WatchEvent.body` properties. Defaults become Jinja strings in `DEFAULT_TITLE_TEMPLATES` / `DEFAULT_BODY_TEMPLATES` dicts, rendered by the same pipeline as custom templates. Single source of truth; no drift risk; no golden-equivalence test burden. Enabled by pre-production status — no existing user templates to migrate.

### Custom template replaces all toggle output; toggles seed the pre-fill
Keep today's semantics: `body_template` set → full replacement (additive toggles ignored at dispatch time). But make toggles productive via the pre-fill composer: `compose_body_prefill(event_type, options)` returns `DEFAULT_BODY_TEMPLATES[event_type] + "\n\n" + ADDITIVE_BODY_SNIPPETS[toggle_N]…`. User clicks [Edit template], gets a full composed starting point reflecting current toggle state, edits from there.

### Override = full ContentOptions snapshot, seeded from current default
Matches current `resolve_options()` semantics (overrides wholly replace default for that event). UX-wise, pre-seeding the override from the current default state avoids the "scary blank slate" problem.

### Fixtures only for preview in v1
Real-watch data would require picking a watch, pulling a recent event, and handling sparse metadata. Fixtures give a faithful preview for every event type immediately; real-data preview can follow once the UI is in use.

### Separate `render_template_strict()` for preview, unchanged `render_template()` for dispatch
Preview must surface template errors; dispatch must never break. Two small functions, one semantic per function. Simpler than threading a flag.

### One shared `notification_form.html` partial for all four call sites
Template page, watch-NC edit, watch-NC create, domain-default create. Preview is conditional via `show_preview` param; `show_global_default` is template-only. Converges existing four divergent form-rendering paths.

## Data Model

No schema changes. Existing `ContentConfig` / `ContentOptions` already hold everything needed.

New code modules:
- `src/core/notifications/default_templates.py` — `DEFAULT_TITLE_TEMPLATES`, `DEFAULT_BODY_TEMPLATES`, `ADDITIVE_BODY_SNIPPETS`, `TEMPLATE_VARIABLES`, `compose_body_prefill()`, `compose_title_prefill()`.
- `src/core/notifications/preview_fixtures.py` — `MOCK_EVENT_FIXTURES`, `build_preview_event()`.

Modified:
- `src/core/notifications/events.py` — remove `WatchEvent.title` / `.body` properties; delete `_BODY_TEMPLATES`. Keep `EVENT_TITLES` (UI labels).
- `src/core/notifications/content.py` — `build_body()` reads `DEFAULT_BODY_TEMPLATES`; add `render_template_strict()`; extend `build_template_context()` with `event_label` and `change_summary`.
- `src/core/notifications/dispatcher.py` — title and body both flow through the new Jinja pipeline.

## Routes

| Route | Purpose |
|---|---|
| `POST /notifications/preview` | Stateless form-driven preview (all four contexts) |
| `GET /notifications/compose-body-prefill` | HTMX: returns pre-filled body textarea for current form state |
| `GET /notifications/compose-title-prefill` | HTMX: returns pre-filled title textarea for current form state |

All dashboard-auth; prefix unchanged from existing dashboard routes.

## Implementation Sequencing (landable steps)

| # | Change | Landable alone |
|---|---|---|
| 1 | Internal refactor: `default_templates.py`, unify dispatch rendering, remove `WatchEvent.title`/`.body`. No UI change. | ✅ |
| 2 | Preview endpoint + `preview_fixtures.py` + `render_template_strict()`. Not yet bound to UI. | ✅ |
| 3 | New `notification_form.html` partial + preview pane + variable chips/drawer. Template screen migrated. | ✅ |
| 4 | Watch-NC forms migrated to new partial. | ✅ |
| 5 | Domain-default forms migrated. Old `notification_content_options.html` deleted. | ✅ |

## Testing Strategy

- **Unit:** rendered output of `DEFAULT_TITLE_TEMPLATES` / `DEFAULT_BODY_TEMPLATES` matches pre-refactor `WatchEvent.title` / `.body` across a fixture matrix of all event types.
- **Unit:** `compose_body_prefill()` rendered through Jinja equals `build_body()` output for all toggle combinations on a canonical event.
- **Integration:** preview endpoint per event type returns expected title + body substrings; invalid Jinja renders the error card.
- **Integration:** existing form-parsing tests unchanged; add round-trip tests for override card add/remove.
- **Dashboard:** template page renders all four sections, variable chips and drawer render, preview pane updates on form change.
- **A11y:** `aria-live` on preview pane; `<section aria-labelledby>` landmarks per card; focus stays in-form across HTMX swaps; chip tooltips use `aria-describedby`.

## Out of Scope (v1)

- Preview driven by real watch/event data (fixtures only).
- Save/reuse of composed Jinja as "my starting template" presets.
- Template linter beyond Jinja's own error reporting.
- Per-event Apprise URL overrides.
- Test-template button changes — the existing separate action is unchanged.
- Dashboard-wide dark-mode or accessibility work beyond what this redesign directly touches.
