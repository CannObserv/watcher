# Snapshot Screenshot Preview

**Date:** 2026-03-31

## Goal

Add a screenshot thumbnail to the top of the watch detail page so users can visually verify the system is monitoring the right content. Screenshots are captured during the check pipeline using Playwright, stored alongside existing snapshot files, and displayed as a clickable thumbnail that opens the full-size image.

## Data Model

Add to `Snapshot`:

- `screenshot_path` — `Optional[String]`, path relative to `WATCHER_DATA_DIR` (e.g. `snapshots/{watch_id}/{snapshot_id}.png`)

No new tables. Screenshot is just another artifact of a snapshot, like `storage_path` and `text_path`.

## Pipeline Changes

In `pipeline.py`, **after** the existing hash/extract/diff/store steps:

1. Check if Playwright is available (try-import guard)
2. If available, launch headless Chromium, navigate to the watch URL, take a viewport screenshot (1280x800), save as PNG
3. Store at `snapshots/{watch_id}/{snapshot_id}.png`
4. Set `screenshot_path` on the Snapshot record
5. Wrap entire block in try/except — failure logs a warning, leaves `screenshot_path` null, does not fail the check

## Dependency

- `playwright` as optional extra: `[project.optional-dependencies] browser = ["playwright"]`
- After install: `playwright install chromium`
- Pipeline uses a try-import pattern — no screenshot capability if Playwright isn't installed, no error

## Dashboard Changes

**Watch detail page** — new section immediately after the name/URL header, before config fields:

- If latest snapshot has `screenshot_path` and file exists: render `<img>` thumbnail (max-width ~400px, aspect-ratio preserved), linked to full-size image route
- If no screenshot: section omitted entirely (no placeholder, no "no screenshot available" message)

**New route:** `GET /watches/{watch_id}/screenshot` — serves the latest snapshot's screenshot PNG from disk. Optional `snapshot_id` query param to serve a specific snapshot's screenshot.

## Migration

One Alembic migration: add nullable `screenshot_path varchar` column to `snapshots`.

## Out of Scope

- Screenshot for non-HTML content types (PDF, CSV)
- Screenshot diffing / visual regression
- Configurable viewport size (hardcode 1280x800)
- Screenshot on-demand / re-generation
- Text-based content preview
