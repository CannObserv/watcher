# watcher

Monitors cannabis industry activity: licenses, regulatory filings, compliance events, and entity relationships.

## Architecture

The **`WatchedItem` is the single monitored entity** (#191): one row = one URL = one
fingerprint = one change signal. It owns its `effective_url`, `source_specs`, schedule
(`default_schedule_config` + an optional 1:1 `TemporalProfile`), `domain_name` /
`domain_suspended`, health/timestamps, and its notification surface. A periodic
`schedule_tick` enqueues `check_watched_item` for each due item; the pipeline extracts,
fingerprints, writes a `ChangeRevision`, and dispatches `CHANGE_DETECTED` once per item.

Canonical content provenance (InfoItem / InfoSource / SourceRevision) lives in the sibling
**Archiver service** (separate repo, port 8020), consumed via the `archiver-client`
SDK. `archiver_info_item_id` on a WatchedItem is an optional cross-schema reference; URL-only
WatchedItems leave it null. SourceRevisions are POSTed to Archiver on every detected change,
with a local `pending_archiver_sync` outbox + drain worker guaranteeing delivery during
Archiver outages. Notifications dispatch through the sibling **Notifier service** via the
`NotifierClient` SDK.

Operate WatchedItems at `/api/v1/watched-items` (API) and `/watched-items` (dashboard).
Full conventions: [AGENTS.md](AGENTS.md). Operator install: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
Collapse design: [docs/plans/2026-06-16-collapse-watcheditem-watch-design.md](docs/plans/2026-06-16-collapse-watcheditem-watch-design.md).

## Setup

```bash
git submodule update --init --recursive
uv sync
```

## Development

```bash
# Dev server on port 8001 against a non-production database (#233).
# Port 8000 belongs to the systemd service — never bind it by hand.
bash scripts/dev_server.sh
```

## Testing

```bash
uv run pytest
```

See [docs/COMMANDS.md](docs/COMMANDS.md) for full command reference.
