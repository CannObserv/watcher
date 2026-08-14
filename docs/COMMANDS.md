# Common Commands

## Setup

```bash
# Install dependencies (creates .venv automatically)
uv sync
```

## Environment

Two env files, loaded in order:

```bash
# Production secrets (DATABASE_URL) — persistent, survives repo resets
/etc/watcher/.env

# Dev/agent secrets (GH_TOKEN, TEST_DATABASE_URL) — repo root, git-ignored
.env

# Load both for shell commands
source scripts/load-env.sh
```

The systemd service loads both automatically (see `deploy/watcher.service`).

`scripts/load-env.sh` is **sourced, not executed** — the exports have to land in your
shell. It parses each file rather than sourcing it, so a secrets file is never run, and
skips a malformed line instead of aborting. It replaced
`export $(cat … | xargs)`, which printed the whole environment (secrets included) when
both files were absent, died under `set -e` on any comment line, and word-split values
containing spaces. Paths are overridable via `WATCHER_SYSTEM_ENV_FILE` /
`WATCHER_PROJECT_ENV_FILE`; guarded by `tests/scripts/test_load_env.py`.

## Shipping

```bash
# Full ship gate: ruff check, ruff format --check, pytest (non-integration)
bash scripts/pre-ship.sh
```

This is watcher's thin wrapper — it loads the env files above, then delegates to the
vendored gate in `shipping-work-python-fastapi`. Run it from the repo root; it exits
non-zero on any failure and 2 on tooling/infra problems (including an uninitialized
`skills-vendor/` submodule). See [SKILLS.md](SKILLS.md) for why the gate itself is not
forked.

## Service Management

The watcher service runs via systemd. **Always use systemctl** — never start uvicorn manually on port 8000.

```bash
# Restart after code changes (migrations are NOT auto-run)
sudo systemctl restart watcher

# Check status
sudo systemctl status watcher

# Follow logs
sudo journalctl -u watcher -f

# Reload systemd after editing deploy/watcher.service
sudo systemctl daemon-reload && sudo systemctl restart watcher
```

## Development

```bash
# Dev server (port 8001) — the ONLY sanctioned launch path (#233).
# Targets TEST_DATABASE_URL (or WATCHER_DEV_DATABASE_URL), migrates it, and
# refuses any DB whose name lacks a _test/_dev suffix. Never hand-run uvicorn
# with the prod env loaded: /etc/watcher/.env points DATABASE_URL at
# production, and the embedded worker would consume the prod task queue.
bash scripts/dev_server.sh

# Knobs: WATCHER_DEV_PORT (default 8001; 8000 refused — it belongs to
# systemd), WATCHER_DEV_DATABASE_URL, WATCHER_DEV_SKIP_MIGRATE=1
```

Never launch uvicorn by hand with the prod env loaded — `/etc/watcher/.env`
points `DATABASE_URL` at production, and a hand-run "dev" server would share
the prod DB, run a second Procrastinate worker on the prod queue, and split
the rate-limiter budget (#233). The script targets `TEST_DATABASE_URL` (or
`WATCHER_DEV_DATABASE_URL`), migrates it, and refuses anything whose DB name
lacks a `_test`/`_dev` suffix. The same rule is enforced in-app by
`src/core/db_safety.py`; only `deploy/watcher.service` opts into prod via
`WATCHER_ALLOW_PRODUCTION_DB=1` (in the unit, never an env file).

Both sanctioned launch paths (`scripts/dev_server.sh` and the systemd
`ExecStart`) pass `--log-config src/core/log_config.json` so uvicorn's own
access/error lines are JSON like the app's (#244). Any ad-hoc uvicorn command
needs the same flag, or it emits plain text alongside the JSON records.

### Testing code changes against the live site

After committing to main, restart the service to pick up changes:

```bash
sudo systemctl restart watcher
# Verify
curl -s http://localhost:8000/health | python3 -m json.tool
```

### Worktree testing

Run a worktree build on a different port to avoid conflicting with the service:

```bash
cd .worktrees/<branch>
bash scripts/dev_server.sh   # same guard rails as the repo-root dev server
```

## Testing

```bash
# Run all tests (excludes integration)
uv run pytest

# Run with coverage
uv run pytest --cov

# Run a specific file
uv run pytest tests/path/to/test_file.py --no-cov

# Run integration tests (hits live external services)
uv run pytest -m integration
```

## Linting

```bash
# Check
uv run ruff check .

# Fix auto-fixable issues
uv run ruff check --fix .
```

## Database

```bash
# PostgreSQL setup (first time)
sudo apt-get install -y postgresql postgresql-client
sudo systemctl start postgresql
sudo -u postgres psql -c "CREATE USER watcher WITH PASSWORD 'watcher';"
sudo -u postgres psql -c "CREATE DATABASE watcher OWNER watcher;"
sudo -u postgres psql -c "CREATE DATABASE watcher_test OWNER watcher;"

# Watcher migrations (requires DATABASE_URL in env)
source scripts/load-env.sh
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "description of change"
uv run alembic current

# Archiver service migrations live in the sibling Archiver repo.
# See its docs/COMMANDS.md.
```

## Task Queue (Procrastinate)

```bash
# Apply procrastinate schema (first time, after DB setup)
source scripts/load-env.sh
uv run procrastinate --app=src.workers.app schema --apply

# Run worker standalone (alternative to embedded mode in FastAPI)
uv run procrastinate --app=src.workers.app worker

# The worker also runs embedded in FastAPI via lifespan — no separate process needed for dev
```

## Tailwind CSS

```bash
# One-time VM setup (tailwindcss CLI — global npm)
sudo npm install -g @tailwindcss/cli

# Build Tailwind CSS
bash scripts/build-css.sh

# Watch mode (auto-rebuild on changes)
bash scripts/build-css.sh --watch
```

## Git Submodules

```bash
# Init after cloning
git submodule update --init --recursive

# Force-refresh vendor skills
git submodule update --remote --merge skills-vendor/gregoryfoster-skills skills-vendor/obra-superpowers
```

## Tests require the Archiver sibling repo

Watcher's `tests/conftest.py` provisions the cross-schema `information.*`
test tables by subprocess-invoking Archiver's own alembic against
`TEST_DATABASE_URL`. Without the sibling repo on disk, tests fail at
session start with "Archiver repo not found at /home/exedev/archiver".

Setup:

```bash
# Default location:
git clone <archiver-repo> /home/exedev/archiver
cd /home/exedev/archiver && uv sync
```

Override via `ARCHIVER_REPO_PATH=/some/other/path` if you keep the sibling repo
elsewhere. Since #254 that is the *only* thing pointing at the sibling checkout:
the `archiver-client` path dependency that used to be pinned separately in
`[tool.uv.sources]` — and ignored the variable, so relocating meant editing two
places or getting passing tests over a broken `uv sync` — went with the SDK.

The `information` schema persists between pytest sessions to enable
the cache-check (#150); per-test row isolation is still handled by
`db_session`'s savepoint rollback. Tests that bypass `db_session` and
write directly via `engine.connect()` will leak rows into subsequent
sessions — don't do that.

**pytest-xdist is unsupported.** The fixture writes to a single
`TEST_DATABASE_URL`; multiple xdist workers would race on `alembic
upgrade head` and on watcher-table teardown. Tracked in #150 —
worker-id-suffixed databases + a coordination lock are the rework when
xdist is actually adopted.

## CI (#220)

GitHub Actions (`.github/workflows/ci.yml`) runs on push/PR to `main`: a
**lint** job (`ruff check` + `ruff format --check`), a **test** job
(`pytest -m "not integration"` against a `postgres:16` service), and a
**migrations** job (independent migration-chain smoke-check, #234 — `alembic
upgrade head` from an empty `postgres:16` then `alembic check` for drift). Only
the **test** job checks out the sibling `archiver` repo (public; for
`ARCHIVER_REPO_PATH`, whose alembic builds the `information` schema conftest's
factories write to) — `lint` and `migrations` stopped needing it when #254
removed the `archiver-client` path dep. All three jobs rewrite
the `notifier-client` SSH source to HTTPS, authenticate to GCS **keyless via
WIF** (`vars.GCP_WIF_PROVIDER` → `co-pypi-reader` SA), and sync the wheelhouse
before `uv sync`. Only the test job also syncs archiver's wheelhouse (its `uv
run alembic` subprocess needs co-core); the migrations job does **not** — the
#234 squash collapsed the pre-existing chain into a self-contained genesis
baseline (`2addddea0b03`) that references no `information` schema, so `upgrade
head` from empty is fully standalone (no archiver seeding, no cross-service
ordering). **Squash cutover:** already-migrated DBs need a one-time `alembic
stamp 2addddea0b03 --purge` before their next upgrade — see `docs/DEPLOYMENT.md`
→ "Migration baseline (squash)". Integration tests hit live external services
and are excluded in CI. **One-time GCP grant** (operator, for WIF) — bind watcher's repo
to the read-only SA; the org-scoped `github-ci` provider needs no change:
```bash
gcloud iam service-accounts add-iam-policy-binding \
  co-pypi-reader@co-gcs.iam.gserviceaccount.com --project=co-gcs \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/912903030445/locations/global/workloadIdentityPools/github/attribute.repository/CannObserv/watcher"
```
