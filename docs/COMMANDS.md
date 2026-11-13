# Common Commands

## Setup

```bash
# Install dependencies (creates .venv; co-core needs the co-pypi token, #358)
source scripts/load-env.sh && bash scripts/co-pypi.sh uv sync

# Install the git pre-commit hook — once per clone; worktrees share .git/hooks
uv run pre-commit install
```

The hook (`.pre-commit-config.yaml`, #352) runs `ruff check` and `ruff format
--check` (Python and Markdown). Its CSS check went with the dashboard (#377).
Nothing installs the hook for you: `.git/hooks/` is per clone, which is why CI
also runs every check (*CI* below).

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

The systemd service loads **only** `/etc/watcher/.env` — never the repo `.env`, whose agent tokens are no production
configuration (#296 D5; `deploy/watcher.service`, [ENVIRONMENT.md](ENVIRONMENT.md)).
A production setting put in the repo `.env` is one the service never sees.

`scripts/load-env.sh` is **sourced, not executed** — the exports have to land in your
shell. It parses each file rather than sourcing it, so a secrets file is never run, and
skips a malformed line instead of aborting. It replaced
`export $(cat … | xargs)`, which printed the whole environment (secrets included) when
both files were absent, died under `set -e` on any comment line, and word-split values
containing spaces. Paths are overridable via `WATCHER_SYSTEM_ENV_FILE` /
`WATCHER_PROJECT_ENV_FILE`; guarded by `tests/scripts/test_load_env.py`.
In a linked worktree, which has no `.env` of its own, it falls back to the primary
checkout's, and when neither file is readable it prints a `WARN` naming the paths
tried, never a value (#404). It leaves the names it set, unexported, in
`WATCHER_LOADED_ENV_NAMES` for the ship gate's stamp.

## Shipping

```bash
# Full ship gate: ruff check, ruff format --check, pytest (default), pytest -m integration
bash scripts/pre-ship.sh
```

This is watcher's thin wrapper — it loads the env files above, delegates to the
vendored gate in `shipping-work`, and once that passes runs
`pytest -m integration` (~20 s against the local test DB), failing on a skip or xfail as well
as a failure (#353). The vendored gate deselects the mark; watcher runs it here
because merging to `main` locally and restarting means CI reports only after the code
is live. Run it from the repo root; it exits non-zero on any failure and 2 on
tooling/infra problems (including an uninitialized `skills-vendor/` submodule). See [SKILLS.md](SKILLS.md) for why the gate itself is not
forked.

The vendored gate's default suite runs with CI's no-server `TEST_DATABASE_URL`, so the
local default run matches CI's `test` job: a DB test missing its `integration` marker
fails here too (#406).

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
# Targets TEST_DATABASE_URL (or WATCHER_DEV_DATABASE_URL), migrates it,
# applies the procrastinate schema (#341), and refuses any DB whose name lacks
# a _test/_dev suffix. Never hand-run uvicorn with the prod env loaded:
# /etc/watcher/.env points DATABASE_URL at production, and the embedded worker
# would consume the prod task queue. Its env files load through
# scripts/load-env.sh — parsed, never sourced; a linked worktree with no .env
# gets the primary checkout's (#407, #404).
bash scripts/dev_server.sh

# Knobs: WATCHER_DEV_PORT (default 8001; 8000 refused — it belongs to
# systemd), WATCHER_DEV_DATABASE_URL, WATCHER_DEV_SKIP_MIGRATE=1,
# WATCHER_DEV_BUS_REDIS_URL
```

Never launch uvicorn by hand with the prod env loaded — `/etc/watcher/.env`
points `DATABASE_URL` at production, and a hand-run "dev" server would share
the prod DB, run a second Procrastinate worker on the prod queue, and split
the rate-limiter budget (#233). The script targets `TEST_DATABASE_URL` (or
`WATCHER_DEV_DATABASE_URL`), migrates it, and refuses anything whose DB name
lacks a `_test`/`_dev` suffix. The same rule is enforced in-app by
`src/core/db_safety.py`; only `deploy/watcher.service` opts into prod via
`WATCHER_ALLOW_PRODUCTION_DB=1` (in the unit, never an env file).

Before its first `uv run` the script runs the unit's start check, `uv sync
--locked --offline` (#401, #358; [DEPENDENCIES.md](DEPENDENCIES.md) →
*The start check*), and refuses to start on a miss, naming the sync that fixes
it. From the deploy checkout every `uv run` syncs `watcher.service`'s own
`.venv`. No bypass knob, as in the unit. A fresh worktree fails here unless
uv's cache holds every locked file: `bash scripts/co-pypi.sh uv sync`.

No migration creates procrastinate's tables, so the script applies them too
(#341): always after the `TEST_DATABASE_URL` branch's public-schema reset, and
on a persistent `WATCHER_DEV_DATABASE_URL` only when `procrastinate_jobs` is
missing — `schema --apply` is not idempotent. Without them the embedded
worker's `register_worker` fails at boot.

The database is not the only production resource an env file hands out. The
script clears an inherited `WATCHER_BUS_REDIS_URL` (#262) unless a scratch
replacement is named, because a dev server runs the embedded worker against a
real check pipeline. The bus has a unit-only opt-in flag; a URL held without it
aborts startup rather than going quiet. See
[ENVIRONMENT.md](ENVIRONMENT.md) → *Environment Variables*.

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
# Run the default suite (excludes integration and live)
uv run pytest

# Run with coverage
uv run pytest --cov

# Run a specific file
uv run pytest tests/path/to/test_file.py --no-cov

# Run integration tests (need TEST_DATABASE_URL, nothing external)
uv run pytest -m integration

# Run live tests (a real external service; excluded from every gate)
uv run pytest -m live
```

**What the marks mean (#353).** `integration` = connects to the test database and
needs nothing else: run with only `TEST_DATABASE_URL` set and outbound network
denied, every one passed. CI runs them in the `integration` job and
`scripts/pre-ship.sh` runs them locally; both fail on a skip or an xfail
(`scripts/check_no_skips.py`), so a test that skips itself for want of a tool or
credential cannot pass as "ran". `live` = needs a real external service or
credential; no gate selects it. A new test that reaches outside the test database
goes under `live`, never `integration`. Both are excluded from the default run
(`addopts`). The default suite needs `TEST_DATABASE_URL` set, never a server: CI's
`test` job runs it with no database, so an unmarked test that connects fails there.
`tests/test_integration_gate.py` pins the marks and the CI jobs.

**Nothing external is enforced, not just measured.** An autouse fixture
(`network_guard`, `tests/conftest.py` → `tests/network_guard.py`) refuses every
`connect` except loopback, unix sockets and `TEST_DATABASE_URL`'s host, with
`ENETUNREACH` — what the `unshare -n` run saw — and fails the test at teardown even
if the code swallowed the refusal. `live` tests are exempt. A test that provokes a
refusal on purpose clears `network_guard.attempts`.

## Linting

```bash
# Check
uv run ruff check .

# Fix auto-fixable issues
uv run ruff check --fix .

# Format (CI runs the --check form)
uv run ruff format --check .
uv run ruff format .
```

`ruff format` also formats the `python` fences in Markdown (ruff ≥0.16, #315):
every Markdown file is in scope except `docs/plans/` and `skills/` — the
reasons sit beside `extend-exclude` in `pyproject.toml`.

## Database

```bash
# PostgreSQL setup (first time)
sudo apt-get install -y postgresql postgresql-client
sudo systemctl start postgresql
sudo -u postgres psql -c "CREATE USER watcher WITH PASSWORD 'watcher';"
sudo -u postgres psql -c "CREATE DATABASE watcher OWNER watcher;"
# Then split the roles: scripts/setup-db-roles.sql (#259) — see
# docs/MIGRATIONS.md -> "Migration role and application role". Grants are not
# schema state, so no migration recreates them on a fresh host.
# The test role and its databases, never owned by watcher (#388):
# scripts/setup-test-role.sql — see "The test database" below.

# Watcher migrations. Alembic connects with WATCHER_MIGRATION_DATABASE_URL when
# set, else DATABASE_URL (#259). alembic.ini carries no URL, so a shell that
# skipped load-env.sh fails instead of reaching for a default.
source scripts/load-env.sh
uv run alembic upgrade head
uv run alembic current

# Archiver service migrations live in the sibling Archiver repo.
# See its docs/COMMANDS.md.
```

### The test database

`TEST_DATABASE_URL` logs in as **`watcher_tester`** (#388), never as `watcher`,
the production owner. The test role has `LOGIN CREATEDB` and nothing else: no
`CREATEROLE`, no superuser, no membership. It owns `watcher_test`,
`watcher_dev`, and the scratch `watcher_rh*_test` databases the backup
rehearsal creates. PUBLIC has no `CONNECT` on `watcher`, so the test role
can't open a session there at all. `scripts/setup-test-role.sql` creates the
role and its databases, and the one-time switch is in
[MIGRATIONS.md](MIGRATIONS.md) → *The test role*.

**The guard checks the credential as well as the name.** `db_safety` refuses a
database whose name lacks `_test`/`_dev`. `scripts/db_containment.py` refuses a
**role** that can connect to, or acts as the owner of, any database without
that suffix. `postgres`, `template0` and `template1` are exempt by name.

- **Where it runs.** Every `integration` test (`contained_test_role` in
  `tests/conftest.py`), and `scripts/dev_server.sh` before its reset, so
  `WATCHER_DEV_DATABASE_URL` is held to the same rule.
- **CI passes it** as the superuser: its container holds only `watcher_test`
  and the exempt three.
- **A refusal** names the role and the databases it reaches.

```bash
DATABASE_URL="$TEST_DATABASE_URL" uv run python -m scripts.db_containment && echo contained
```

**Autogenerate wants a scratch database, not production.** `--autogenerate`
diffs the models against whatever database it connects to, so pointing it at
`DATABASE_URL` means diffing production. Build a throwaway instead — which is
what `CREATEDB` on the migration role (#259) is for:

```bash
source scripts/load-env.sh
MIG="${WATCHER_MIGRATION_DATABASE_URL:-$DATABASE_URL}"   # the owning role
SCRATCH="${MIG%/*}/watcher_autogen_dev"                  # same credentials, new database

psql "${MIG/+asyncpg/}" -c "CREATE DATABASE watcher_autogen_dev"
WATCHER_MIGRATION_DATABASE_URL="$SCRATCH" uv run alembic upgrade head
WATCHER_MIGRATION_DATABASE_URL="$SCRATCH" uv run alembic revision \
  --autogenerate -m "description of change"
WATCHER_MIGRATION_DATABASE_URL="$SCRATCH" uv run alembic check   # drift, locally
psql "${MIG/+asyncpg/}" -c "DROP DATABASE watcher_autogen_dev"
```

`watcher_test` is not a substitute: pytest builds it with
`Base.metadata.create_all`, so its `alembic_version` never matches its tables
and `upgrade head` fails part-way through the chain. Alembic warns on stderr
when the two URLs name different databases — that line is expected here, and is
the signal to take seriously anywhere else.

## Task Queue (Procrastinate)

```bash
# Apply procrastinate schema (first time, after DB setup; dev_server.sh does
# this itself for the dev database, #341)
source scripts/load-env.sh
uv run procrastinate --app=src.workers.app schema --apply

# Run worker standalone (alternative to embedded mode in FastAPI)
uv run procrastinate --app=src.workers.app worker

# The worker also runs embedded in FastAPI via lifespan — no separate process needed for dev
```

## Backup, restore and job history (#296)

Operator entry points in `src/ops/`, each with its runbook. A `uv run` below syncs
the service's `.venv` without the start check (#401,
[DEPENDENCIES.md](DEPENDENCIES.md) → *Accepted limits*); `.venv/bin/python` doesn't sync:

```bash
# The backup's read-only database role — once per cluster, before its first run
# (docs/RECOVERY.md → Install and first run). Idempotent; its report is the check:
sudo -u postgres psql -d watcher < scripts/setup-backup-role.sql

# Nightly dump to GCS — the timer runs it; one run by hand (docs/RECOVERY.md):
sudo systemctl start watcher-backup.service

# Restore — always name the host that shipped the dump (docs/RECOVERY.md → Restore).
# This host ships under `co-watcher/`; `watcher/` is the retired VM's timeline, whose
# newest object post-dates the #296 cutover — `--latest --prefix watcher` is the wrong DB:
sudo bash -c "WATCHER_SYSTEM_ENV_FILE=/etc/watcher/backup.env WATCHER_PROJECT_ENV_FILE=/dev/null . scripts/load-env.sh; export GOOGLE_APPLICATION_CREDENTIALS=/etc/watcher/co-watcher-backup.json; .venv/bin/python -m src.ops.restore --list"

# One-off job-history backlog prune; the hourly task holds it after
# (docs/DEPLOYMENT.md → Job history). The opt-in covers the dry run too:
source scripts/load-env.sh
WATCHER_ALLOW_PRODUCTION_DB=1 uv run python -m src.ops.prune_job_history --dry-run

# Break-glass: stop checking every item on one host (#372), Archiver up or down.
# Database only — no bus, no Archiver. Audited `source: "cli"`; exit 0 on a change
# or a no-op (it says which), 1 for an unknown host, 2 if it could not run
# (config, usage, database). The service acts on it from its next tick; since F1
# (#374) nothing downstream sees it. The venv's interpreter, so a break-glass run
# never syncs the service's venv (#401), the form suggested for archiver#372's runbook:
WATCHER_ALLOW_PRODUCTION_DB=1 .venv/bin/python -m src.ops.suspend_domain HOST
WATCHER_ALLOW_PRODUCTION_DB=1 .venv/bin/python -m src.ops.suspend_domain HOST --resume

# The patching gate (#382; docs/DEPLOYMENT.md → OS patching): Procrastinate jobs
# in `doing`, read-only, stdout the count alone; 1 database, 2 config or usage.
# The venv's interpreter, as the knob runs it — `uv run` syncs the service's venv:
WATCHER_ALLOW_PRODUCTION_DB=1 .venv/bin/python -m src.ops.inflight --env-file /etc/watcher/.env
```

## F3: the notification cutover (#376) — done

Flipped at T = `2026-11-06T21:00:00Z`: Archiver live with T at 20:16:50Z,
Watcher at 20:18:00Z, drained by 22:13Z, exports shredded. Part 2 deleted
Watcher's dispatch path; Archiver sends every notification, and #379 deleted
the rest of Watcher's notification code. The record is on #376 and #368; the
one-time deploy order is [MIGRATIONS-HISTORY.md](MIGRATIONS-HISTORY.md) →
*Restart-before-migrate — one-time, `e2eab993d615`*.

## Git Submodules

```bash
# Init after cloning
git submodule update --init --recursive

# Force-refresh vendor skills
git submodule update --remote --merge skills-vendor/gregoryfoster-skills skills-vendor/obra-superpowers
```

## The test database

`tests/conftest.py` builds watcher's tables in `TEST_DATABASE_URL` with
`Base.metadata.create_all` and drops them at session end; per-test isolation is
`db_session`'s savepoint rollback. Tests that bypass `db_session` and write
directly via `engine.connect()` leak rows into subsequent sessions — don't do
that.

**No sibling checkout (#311).** The suite used to subprocess-run the Archiver
checkout's alembic, located by `ARCHIVER_REPO_PATH`, to build an `information`
schema its factories wrote rows into. Those rows only minted two ULIDs:
production has had no `information` schema since #271 (see `docs/MIGRATIONS-HISTORY.md`
→ "Drop the dead `information` schema"), and the WatchedItem links carry no FK.
`make_watched_item` now mints both links, `test_engine` drops a leftover
`information` schema from a pre-#311 session, and
`tests/test_archiver_isolation.py` fails if the test database carries one
again. No migration may create or drop it either — `tests/test_migration_chain.py`.

**pytest-xdist is unsupported.** The fixture writes to a single
`TEST_DATABASE_URL`; multiple xdist workers would race on creating and
dropping the watcher tables. Tracked in #150 — worker-id-suffixed databases
are the rework when xdist is actually adopted.

## CI (#220)

GitHub Actions (`.github/workflows/ci.yml`) runs on push/PR to `main`: a
**lint** job (`ruff check` + `ruff format --check`), a **test** job
(`pytest -m "not integration and not live"` with **no database** — its
`TEST_DATABASE_URL` points at port 1, so an unmarked test that connects fails there),
an
**integration** job (`pytest -m integration` against its own `postgres:16`, then
`scripts/check_no_skips.py` on the JUnit report — #353), and a
**migrations** job (independent migration-chain smoke-check, #234 — `alembic
upgrade head` from an empty `postgres:16` then `alembic check` for drift). No
job checks out a sibling repo — #254 removed the `archiver-client` path dep
that `lint` and `migrations` needed, and #311 the `test` job's archiver alembic
run. The four Python jobs authenticate **keyless via WIF**
(`vars.GCP_WIF_PROVIDER` → `co-pypi-reader` SA), whose access token `uv sync`
alone takes for the co-pypi index (#358). The migrations job needs
nothing from archiver either — the #234 squash collapsed the pre-existing
chain into a self-contained genesis baseline (`2addddea0b03`) that references
no `information` schema, so `upgrade head` from empty is fully standalone (no
archiver seeding, no cross-service ordering).
Both database jobs pull `postgres:16` as `mirror.gcr.io/library/postgres:16`:
GitHub's shared runners exhaust Docker Hub's anonymous pull limit, and the mirror
needs no credential (`tests/ci/test_service_images.py`).
**Squash cutover:** already-migrated DBs need a one-time `alembic
stamp 2addddea0b03 --purge` before their next upgrade — see `docs/MIGRATIONS-HISTORY.md`
→ "Migration baseline (squash)". Only `live` tests (real GCS) run in no CI
job. **One-time GCP grant** (operator, for WIF) — bind watcher's repo
to the read-only SA; the org-scoped `github-ci` provider needs no change:
```bash
gcloud iam service-accounts add-iam-policy-binding \
  co-pypi-reader@co-gcs.iam.gserviceaccount.com --project=co-gcs \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/912903030445/locations/global/workloadIdentityPools/github/attribute.repository/CannObserv/watcher"
```

**The runner is pinned to the host's release (#365).** Every `ci.yml` job and
`audit.yml`'s run `ubuntu-24.04`, never `ubuntu-latest`: the backup rehearsal runs
the image's own `pg_dump`/`pg_restore`, and 26.04's PostgreSQL 18 client writes
an archive the `postgres:16` service rejects. The 24.04 image's client is the
host's major. `tests/ci/test_workflow_pins.py` holds one image for both
workflows, and on the host it fails until that image is `ubuntu-$VERSION_ID`,
so moving the host is what moves the pin. The rehearsal fixture skips a client
of any other major, naming the skew, and the `integration` job's no-skip check
turns that skip red. `context-cadence.yml` stays on `ubuntu-latest`: it is
generated, and it is outside the gate.

**The lock audit is not CI's gate (#397).** `.github/workflows/audit.yml`'s one
job, `uv audit (report-only)`, runs `scripts/audit_lock.sh` weekly, on demand, and
on a lock-touching push or PR. It always exits 0 — a finding is a warning and the
job summary — so it never decides a merge; the gate is `ci.yml`'s jobs. No
secret, no WIF, no index token: `uv audit --frozen` reads `uv.lock` alone. What to
do with a finding: `docs/DEPENDENCIES.md` → *Advisory floors*.

### Dependabot PRs (#283)

**Moved to [DEPENDENCIES.md](DEPENDENCIES.md) → *Dependabot PRs*.**
