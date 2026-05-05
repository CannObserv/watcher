# watcher

Monitors cannabis industry activity: licenses, regulatory filings, compliance events, and entity relationships.

## Architecture

Watcher is **InfoItem-native** as of Phase 2c (issue #138). A `Watch` references an `info_item_id` managed by the sibling **Information service** (`src/information/`, port 8020), which owns the canonical URL + extraction config in a versioned `InfoSpec` document. Watch detection lookups, screenshot captures, and notification events all resolve the URL via the `InformationClient` SDK at request time.

Detected changes flow onto a Redis Stream (`info.changes`, envelope `schema_version: 2`, partitioned by `info_item_id`) for downstream consumers (Archive in Phase 3+). The drain task runs every minute via Procrastinate's `@bp.periodic` decorator.

Authoring tools live on the Information service at `/api/v1/tools/*` (Phase 3a) — `validate_info_spec`, `find_info_item`, `fetch_and_render`, `preview_extraction`, `propose_selectors`, plus an atomic `create_info_item` that takes an optional `initial_info_spec`. Smoke: `bash scripts/smoke_phase3a.sh`.

Full design: [docs/plans/2026-05-04-watcher-phase2c-cutover-plan.md](docs/plans/2026-05-04-watcher-phase2c-cutover-plan.md). Operator install (incl. `INFORMATION_API_KEY`): [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Setup

```bash
git submodule update --init --recursive
uv sync
```

## Development

```bash
uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

## Testing

```bash
uv run pytest
```

See [docs/COMMANDS.md](docs/COMMANDS.md) for full command reference.
