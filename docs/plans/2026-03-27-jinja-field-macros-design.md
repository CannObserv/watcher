# Jinja Field Macros — Design

**Issue:** #48
**Date:** 2026-03-27

## Goal

Eliminate the 10-line `{% with %}` + `{% set %}` boilerplate repeated at every `watch_field` and `domain_field` include site. Replace with a single macro call per field.

## Approved Approach

Introduce a dedicated macros file with two macros — `watch_field(ctx)` and `domain_field(ctx)` — each of which does the destructuring internally and delegates to the existing partial via `{% include %}`.

## Key Decisions

### New file: `src/dashboard/templates/macros/fields.html`

Contains two macros:

```jinja
{% macro watch_field(ctx) %}
{% set field_name = ctx.field_name %}
{% set field_label = ctx.field_label %}
... (all 10 sets) ...
{% include "partials/watch_field.html" %}
{% endmacro %}

{% macro domain_field(ctx) %}
... same for domain_field.html ...
{% endmacro %}
```

Both macros are imported `with context` so they inherit `watch`/`domain` from the caller's scope — needed by the partials for HTMX URL construction.

### watch_detail.html

Add import at top:
```jinja
{% from "macros/fields.html" import watch_field with context %}
```

Replace each 12-line block with a single call:
```jinja
{{ watch_field(field_contexts["name"]) }}
```

Applies to all three sections (Details, Schedule, Configuration loop).

### domain_detail.html + route

The `domain_detail_page` route currently passes `domain` as a flat object; the template sets field variables manually. Change:

1. Route (`domain_detail_page`): build `field_contexts` using the existing `_field_context()` helper for all domain fields (same pattern watch detail already uses).
2. Template: add import, replace bare `{% set %}...{% include %}` blocks with `{{ domain_field(field_contexts["key"]) }}`.

### Partials left unchanged

`partials/watch_field.html` and `partials/domain_field.html` are left as-is — they are rendered directly by HTMX partial endpoints which pass flat variables via template context. No changes to HTMX routes.

## Out of Scope

- Merging `watch_field.html` and `domain_field.html` into a single partial
- Any changes to HTMX routes or response rendering
- Business logic changes beyond adding `field_contexts` to the domain detail context
