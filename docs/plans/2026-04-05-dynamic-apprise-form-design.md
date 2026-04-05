# Dynamic Apprise URL Builder — Design

**Date:** 2026-04-05
**Issue:** #75

## Goal

Replace the raw Apprise URL text input with a dynamic, plugin-aware form that constructs valid Apprise URLs from typed token fields — without hardcoding any plugin-specific knowledge in the UI.

## Approved Approach

Option A: Plugin picker + server-assembled URL.

Two-step add form:
1. User picks a plugin from a searchable list (populated from `apprise.Apprise().details()`)
2. HTMX swaps in a generated token form for that plugin
3. User fills typed fields; server assembles the Apprise URL, validates, encrypts, stores

Raw URL fallback ("Enter URL manually") always available for power users. No DB schema changes — the assembled URL remains the stored artifact.

## Architecture

### New API Endpoints

- `GET /api/v1/apprise/plugins` — list of `{schema, service_name, category}` for all 122 plugins, sorted by `service_name`
- `GET /api/v1/apprise/plugins/{schema}` — token definitions + templates for one plugin; 404 for unknown schema

### Extended Existing Endpoint

`POST /api/v1/watches/{id}/notifications` accepts either:
- `{apprise_url: str}` — existing raw URL path (unchanged)
- `{schema: str, tokens: dict}` — new path; server assembles URL, validates, encrypts, stores

### New Dashboard Route

- `GET /partials/apprise-plugin-form/{schema}` — HTMX endpoint returning the token form partial for a selected plugin

### Updated Dashboard Form

The add-notification form becomes two-step:
1. Plugin picker (`<select>` with search or text filter)
2. HTMX-swapped token fields

## URL Assembly Logic

Server-side, no client JS needed:

1. Fetch templates for the plugin from `apprise.Apprise().details()`
2. Iterate templates in order; select the first template where all required tokens are present in submitted values
3. Substitute `{token}` placeholders; join `list:string` tokens with `/`
4. Validate assembled URL with `apprise.Apprise().add()`; return 422 if invalid
5. Encrypt and store as normal

For plugins with divergent template variants (different required token sets — e.g., Slack legacy token vs. bot token), render a **variant selector** (radio/select) above the token fields that constrains which template branch is active.

## Token → Input Type Mapping

| Apprise type | Input rendered |
|---|---|
| `string` | `<input type="text">` |
| `string` + `private=True` | `<input type="password">` with show/hide toggle |
| `bool` | Toggle checkbox |
| `int` / `float` | `<input type="number">` |
| `choice:string` / `choice:int` | `<select>` with `values` list |
| `list:string` | Comma-separated text input with hint label |

Additional rules:
- Skip `schema` token — internal, not user-facing
- Required tokens rendered first; optional tokens under a "Show advanced options" disclosure
- `name` field used as the label
- `regex` mapped to HTML `pattern` attribute for client-side hint
- `default` values pre-fill inputs
- `args` (query-string params) omitted — too advanced; power users use raw URL fallback

## Testing Strategy

**Unit:**
- URL assembly — one test per template-selection branch (required tokens present, missing, variant selection)
- Token→input type mapping — parametrized over all 7 Apprise types
- `GET /api/v1/apprise/plugins` and `GET /api/v1/apprise/plugins/{schema}` — response shape, 404 for unknown schema

**Integration:**
- `POST /api/v1/watches/{id}/notifications` with `{schema, tokens}` — valid tokens assemble and store; invalid tokens return 422
- Assembled URL roundtrip: tokens → URL → `apprise.Apprise().add()` validates

**Dashboard:**
- `GET /partials/apprise-plugin-form/{schema}` — correct input types for a known plugin (e.g., Discord: two password fields + one optional text)
- Variant selector rendered for plugins with divergent template branches
- Form submission produces stored config with correct `channel_hint`

No mocking of `apprise.Apprise().details()` — pure in-process call, no I/O.

## Key Decisions

- **No DB schema changes.** Assembled URL is still the stored artifact; tokens are ephemeral form state.
- **Apprise's own catalog drives the UI.** No hardcoded plugin schemas. When Apprise adds or updates plugins, the form updates automatically.
- **Raw URL fallback always present.** Power users and edge-case plugins remain supported.
- **`args` omitted from generated form.** Keeps the UI simple; negligible real-world impact.

## Out of Scope

- Editing existing configs via token form (tokens not stored; delete-and-recreate is acceptable for now)
- Reusable/shared notification configs across watches (tracked in #74)
- Storing token values alongside the URL (deferred to #74 design)
