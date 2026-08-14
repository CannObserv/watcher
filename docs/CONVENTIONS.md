# Conventions

The long-form conventions. `AGENTS.md` keeps the commit format, the logger
import, the four-key floor, and the general code rules; the reasoning behind them
is here.

## Log-record key contract

Every record serializes as JSON with **at least** `timestamp` / `level` / `logger` /
`message`, plus `exc_info` and whatever extras the emitting library attaches. Those four
are a floor, not an exhaustive list, and are pinned by `tests/core/test_logging.py` —
don't rename or drop a key without updating both.

`timestamp` is ISO 8601 UTC, and `exc_info` appears when logging an exception.
Extras come from the emitting library — procrastinate adds `action`/`job`/…, and
uvicorn's own lines carry extras of their own. The four-key set matches structlog's
defaults, so a future structlog/OTel migration won't churn log consumers. The pin is
`tests/core/test_logging.py` (#238).

## uvicorn's own loggers need `--log-config`

uvicorn's own loggers need `--log-config src/core/log_config.json` (both sanctioned launch
paths already pass it) plus the `strip_color_message` filter; `ExecStartPre` output is
plain text by design, so a log pipeline must tolerate non-JSON journald lines.

Why each half is needed (#244): `uvicorn`, `uvicorn.access`,
and `uvicorn.error` ship with `propagate=False` and their own plain-text handlers,
so `configure_logging()` — which touches only the **root** logger — never reaches
them; without the flag journald gets mixed formats (plain access lines interleaved
with JSON app records). Every uvicorn invocation therefore passes
`--log-config src/core/log_config.json` (already wired into
`deploy/watcher.service` and `scripts/dev_server.sh` — the only two sanctioned
launch paths). That dictConfig file carries **no** copy of the format string: its
`"()"` key calls `build_json_formatter()` in `src/core/logging.py`, the single
formatter definition shared with `configure_logging()`. Both facts are pinned by
`tests/core/test_logging.py`.

Each of the three uvicorn loggers also lists the `strip_color_message` **filter**
(`ColorMessageFilter`, `src/core/logging.py`) — uvicorn attaches an ANSI-coloured
duplicate of every lifecycle line as `extra={"color_message": ...}`, and extras
reach the JSON payload (#246). It sits on the *loggers*, not the stdout handler
and not the formatter's `reserved_attrs`: those clean one sink only, so a handler
that serializes `record.__dict__` directly (OTel's `LoggingHandler`, whose
reserved list omits `color_message`) would silently resurrect the field. Listing
it on all three is load-bearing — propagation walks ancestors' *handlers*, never
their filters, so a filter on the parent `uvicorn` alone never sees a
`uvicorn.error` record.

## `ExecStartPre` output is plain text

**One exception — `ExecStartPre` output is plain text (#247).** The JSON claim
above scopes to the *application's* records. `deploy/watcher.service` runs the
wheelhouse sync as a non-fatal `ExecStartPre`, so journald also gets a plain-text
line on every service start (`wheelhouse in sync: N downloaded, M already present
-> …`, or `error: could not sync gs://…` on the failure path — the one that
appears exactly when something is already wrong). That is by design, not drift:
the step runs under `uv run --no-project` *before* `uv sync`, cannot import the
project, and so cannot share `build_json_formatter()`; emitting JSON would mean a
second hand-maintained copy of the key schema. The unit's other two `ExecStartPre`
lines are silent on success but plain-text on failure the same way (`git rev-parse`
writes `fatal: not a git repository` to stderr; only stdout is redirected to the
build-id file). A log pipeline that `json.loads` every journald `MESSAGE` must
tolerate all of them (reading the entry's native fields — `_SYSTEMD_UNIT`,
`SYSLOG_IDENTIFIER`, `MESSAGE` — is unaffected); the `jq` follow-recipe in
`docs/DEPLOYMENT.md` is written to survive them — bare `jq` aborts on the first
plain line and hides every record after it.

## ULID format errors

**ULID format errors:** Treatment depends on whether the ULID is a path parameter or a filter query parameter.

- **Path parameters** (e.g. `/watched-items/{id}`) → 404. Use `parse_ulid` from [src/api/routes/helpers.py](../src/api/routes/helpers.py), which raises `HTTPException(404)`. The dashboard helper (`get_watched_item_detail` in [src/dashboard/context.py](../src/dashboard/context.py)) returns `None` and the route renders a 404 page.
- **Filter query parameters** on list endpoints (e.g. `?watch_id=<value>`) → 400. Use `parse_filter_ulid` from [src/api/routes/helpers.py](../src/api/routes/helpers.py), which raises `HTTPException(400, "Invalid <field> format")`. Pass the parameter name as `field` (e.g. `parse_filter_ulid(watch_id, "watch_id")`).

Do not use `parse_ulid` for filter query params — the endpoint itself exists; an unparseable filter value is a bad request, not a missing resource.

## DB triggers

**DB Triggers (gotcha):**
- Triggers live in Alembic migrations (`CREATE OR REPLACE FUNCTION` + `CREATE OR REPLACE TRIGGER`; downgrade with `DROP TRIGGER IF EXISTS … ON table; DROP FUNCTION IF EXISTS …`).
- Integration tests use `Base.metadata.create_all` (not migrations), so triggers are NOT applied automatically. Any trigger added in a migration must also be recreated in `tests/conftest.py` inside the `test_engine` fixture, after `create_all`.
- Current triggers: none. `trg_changes_update_last_changed_at` removed in Phase 5 (#156) when the `changes` table was dropped.
