# Changelog

## v2.0.0 — Phase 5 cutover

**Breaking** — Watcher refactored to produce SourceRevisions in Archiver.

- Watch table reshaped: `info_item_id` → `info_source_id`.
- Content persistence dropped: no more `Snapshot`/`Change`/`differ.py`/chunk diffs.
- Notification trigger moved from `info.changes` consumption to inline POST-success sites; outbox drain re-fires deferred notifications.
- New `pending_source_revisions` outbox guarantees delivery to Archiver.
- Scratch cache at `WATCHER_CACHE_DIR` with sweeper + outbox interlock.
- SDK pin: `archiver-client>=2.2.0,<3`.

See [docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md](docs/plans/2026-05-13-phase-5-watcher-v2-cutover.md).

---
