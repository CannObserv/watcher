# URL Validation in Pydantic Schemas

**Issue:** #43
**Date:** 2026-03-26

## Goal

Add URL shape validation to all URL-typed fields in the API schemas. Prevent persistence of malformed URLs like `"not-a-url"` through `WatchCreate` and `WatchUpdate`.

## Approach

Add a reusable `HttpUrlStr` annotated type to `src/api/schemas/types.py`. This type validates using Pydantic's `HttpUrl` internally but resolves to a plain `str`, avoiding `Url` object friction with httpx, SQLAlchemy, and `from_attributes=True` serialization.

### Why not raw `HttpUrl`?

Existing usage in notification configs (webhook.py, slack.py) shows `HttpUrl` fields are immediately `str()`-ed before use. The `Url` object properties are never accessed. Using `HttpUrl` directly on Watch schemas would add ORM serialization overhead on every response and require `str()` calls at every consumption point.

### Fields changed

| Schema | Field | Before | After |
|---|---|---|---|
| `WatchCreate` | `url` | `str` | `HttpUrlStr` |
| `WatchUpdate` | `effective_url` | `str \| None` | `HttpUrlStr \| None` |
| `WatchResponse` | `url` | `str` | `HttpUrlStr` |
| `WatchResponse` | `effective_url` | `str \| None` | `HttpUrlStr \| None` |

### Implementation

`HttpUrlStr`: `Annotated[str, BeforeValidator(...)]` that parses through Pydantic's `HttpUrl` and returns `str(result)`.

## Out of scope

- Notification config schemas (already validated via `HttpUrl`)
- Database model changes
- Probe logic changes

## Key decisions

1. **Custom type over raw Pydantic type** — avoids `Url` object friction, consistent with `ULIDStr` pattern
2. **Apply to response schemas too** — defense in depth, negligible cost since it's a plain `str`
3. **`http` and `https` only** — `HttpUrl` enforces this by default, which matches our domain (web monitoring)
