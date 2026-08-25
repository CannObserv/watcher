#!/usr/bin/env bash
# Launch the Watcher dev server (port 8001) against a NON-PRODUCTION database.
#
# Why this script exists (#233; ported from archiver's 2026-07-18 incident):
#
#   AGENTS.md and docs/COMMANDS.md used to document a raw uvicorn recipe for
#   the dev server that began by sourcing /etc/watcher/.env. That file sets
#   DATABASE_URL to *production*, so the dev server on 8001 and the systemd
#   service on 8000 shared one database. Worse than the archiver case: the
#   lifespan starts the embedded Procrastinate worker, so the "dev" server was
#   also a second worker consuming the production task queue and a second
#   DomainRateLimiter splitting every domain's politeness budget. In archiver
#   the same recipe wrote verification rows into the production registry;
#   nothing in the loop was wrong except the recipe.
#
#   tests/conftest.py refuses to let pytest point at production and pins
#   DATABASE_URL to the test database. That guard does nothing for a hand-run
#   server. This script is the same guard for the other way into the database.
#
# Resolution order for the dev database:
#   1. WATCHER_DEV_DATABASE_URL — a persistent dev DB, if you keep one.
#   2. TEST_DATABASE_URL        — the default.
#
# Note on (2): pytest creates and drops watcher tables in TEST_DATABASE_URL.
# Running the suite while a dev server is up on the same database wipes your
# dev data mid-session. That is a survivable annoyance and strictly better
# than writing to production, but if it bites, create a dedicated database and
# set WATCHER_DEV_DATABASE_URL.
#
# Env knobs:
#   WATCHER_DEV_DATABASE_URL             persistent dev DB; wins over TEST_DATABASE_URL
#   WATCHER_DEV_PORT                     default 8001 (8000 is systemd's, refused)
#   WATCHER_DEV_SKIP_MIGRATE=1           skip the alembic upgrade
#   WATCHER_DEV_SERVER_DRY_RUN=1         print resolution, do not exec uvicorn
#   WATCHER_DEV_NOTIFIER_BASE_URL        scratch notifier; opts in, needs the key below
#   WATCHER_DEV_NOTIFIER_API_KEY         scratch notifier key; required with the URL
#   WATCHER_DEV_SERVER_SKIP_ENV_FILES=1  skip sourcing env files (tests)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${WATCHER_DEV_SERVER_SKIP_ENV_FILES:-}" != "1" ]]; then
  set -a
  # shellcheck disable=SC1091
  [ -f /etc/watcher/.env ] && . /etc/watcher/.env
  # shellcheck disable=SC1091
  [ -f "$REPO_ROOT/.env" ] && . "$REPO_ROOT/.env"
  set +a
fi

PORT="${WATCHER_DEV_PORT:-8001}"

# Port 8000 belongs to systemd (watcher.service). Binding it from here either
# fails on a port clash or, worse, shadows the live service.
if [[ "$PORT" == "8000" ]]; then
  echo "dev_server: refusing to bind port 8000 — that port belongs to systemd" >&2
  echo "  (watcher.service). Use the default 8001, or set WATCHER_DEV_PORT." >&2
  exit 1
fi

DEV_URL="${WATCHER_DEV_DATABASE_URL:-${TEST_DATABASE_URL:-}}"

if [[ -z "$DEV_URL" ]]; then
  echo "dev_server: no non-production database URL available." >&2
  echo "  Set TEST_DATABASE_URL (or WATCHER_DEV_DATABASE_URL) in .env." >&2
  echo "  Refusing to start rather than fall back to DATABASE_URL." >&2
  exit 1
fi

# db_name <url> — the database name, i.e. the path segment. Strips
# scheme://credentials@host:port and the query string so neither an escaped
# slash in a password nor an '@' in the query can be mistaken for the path.
# The query goes FIRST: on a credential-less URL, '@' inside the query would
# otherwise be read as a credentials terminator and the tail returned as the
# name (CR finding 1 — bash accepted, Python refused). Anything without '://'
# is not a connection URL at all; return nothing so the caller fails closed,
# matching database_name() in src/core/db_safety.py.
db_name() {
  local url="$1"
  case "$url" in
    *://*) ;;
    *) return ;;
  esac
  url="${url#*://}"          # drop scheme
  url="${url%%\?*}"          # drop query string
  url="${url#*@}"            # drop credentials, if any
  url="${url#*/}"            # drop host:port, leaving the database name
  printf '%s' "$url"
}

DEV_DB_NAME="$(db_name "$DEV_URL")"

# Positive assertion, not a comparison against known production URLs. String
# equality is defeated by cosmetic differences — postgresql://…/watcher and
# postgresql+asyncpg://…/watcher name the same database, as do localhost and
# 127.0.0.1. The database NAME is the boundary that actually holds. Mirrors
# src/core/db_safety.py, which enforces the same rule inside the application.
case "$DEV_DB_NAME" in
  *_test | *_dev) ;;
  *)
    echo "dev_server: refusing to start against database '${DEV_DB_NAME:-<unparseable>}'." >&2
    echo "  The dev database name must end in '_test' or '_dev'; anything else" >&2
    echo "  is treated as production (see #233 / archiver 2026-07-18 incident," >&2
    echo "  where a dev server wrote into the production registry)." >&2
    echo "  Point TEST_DATABASE_URL or WATCHER_DEV_DATABASE_URL at a" >&2
    echo "  dedicated database, e.g. watcher_test." >&2
    exit 1
    ;;
esac

# Force the dev URL onto the child, and clear the worker override that
# src/workers/__init__.py consults before DATABASE_URL — leaving a production
# value there would run the embedded worker against the production task queue.
export DATABASE_URL="$DEV_URL"
unset PROCRASTINATE_DATABASE_URL

# Same treatment for the migration credential (#259), and it is the sharpest of
# the three: alembic reads WATCHER_MIGRATION_DATABASE_URL first, that variable
# lives in /etc/watcher/.env which this script sources, and the `alembic
# upgrade head` below runs with whatever it finds. Inheriting it would point
# the *dev* launch path at production holding DDL rights — including the
# public-schema DROP in the TEST_DATABASE_URL branch. Overwritten rather than
# unset: unset falls back to DATABASE_URL, which is the same value today but
# would silently follow any future change to that fallback.
export WATCHER_MIGRATION_DATABASE_URL="$DEV_URL"

# Same guard for the bus (#245): /etc/watcher/.env carries the production
# WATCHER_BUS_REDIS_URL, and a dev server inheriting it would publish
# fetch-policy frames onto the stream the live Replicator paces real origins
# from. Publish only when a dev bus is explicitly configured.
#
# WATCHER_BUS_ENABLED is the opt-in src/core/bus.py requires beside the URL
# (#262) — normally unit-only, exactly like WATCHER_ALLOW_PRODUCTION_DB. This
# script is the other sanctioned launch path, so it sets the flag itself in the
# branch that points at a scratch broker, and clears it in the branch that has
# no bus at all: the app must never see a URL without the flag, and never a
# flag inherited from an env file that has no business carrying one.
if [[ -n "${WATCHER_DEV_BUS_REDIS_URL:-}" ]]; then
  export WATCHER_BUS_REDIS_URL="$WATCHER_DEV_BUS_REDIS_URL"
  export WATCHER_BUS_ENABLED=1
  BUS_REPORT="$WATCHER_BUS_REDIS_URL"
  BUS_ENABLED_REPORT="1"
else
  unset WATCHER_BUS_REDIS_URL
  unset WATCHER_BUS_ENABLED
  BUS_REPORT="(cleared)"
  BUS_ENABLED_REPORT="(cleared)"
fi

# Same guard again for the notifier (#277), and this is the one whose stray
# output cannot be recalled: /etc/watcher/.env carries WATCHER_NOTIFIER_BASE_URL and
# WATCHER_NOTIFIER_API_KEY, and this server runs the embedded worker against a real
# check pipeline — so an inherited key delivers real notifications to real
# subscribers as the production tenant, and *succeeds*, leaving no error to
# notice. WATCHER_NOTIFIER_ENABLED is the unit-only opt-in the app now requires beside
# the URL; as with the bus, this script sets it in the branch that points at a
# scratch notifier and clears it in the branch that has none.
#
# Both halves or neither: a dev base URL falling back to the inherited
# production key would be the exact hazard this clears, so a URL without its
# key refuses rather than guesses.
if [[ -n "${WATCHER_DEV_NOTIFIER_BASE_URL:-}" ]]; then
  if [[ -z "${WATCHER_DEV_NOTIFIER_API_KEY:-}" ]]; then
    echo "dev_server: WATCHER_DEV_NOTIFIER_BASE_URL is set without WATCHER_DEV_NOTIFIER_API_KEY." >&2
    echo "  Refusing to fall back to the inherited WATCHER_NOTIFIER_API_KEY — that is the" >&2
    echo "  production tenant's credential (#277). Set both in .env, or neither." >&2
    exit 1
  fi
  export WATCHER_NOTIFIER_BASE_URL="$WATCHER_DEV_NOTIFIER_BASE_URL"
  export WATCHER_NOTIFIER_API_KEY="$WATCHER_DEV_NOTIFIER_API_KEY"
  export WATCHER_NOTIFIER_ENABLED=1
  NOTIFIER_REPORT="$WATCHER_NOTIFIER_BASE_URL"
  NOTIFIER_ENABLED_REPORT="1"
else
  unset WATCHER_NOTIFIER_BASE_URL
  unset WATCHER_NOTIFIER_API_KEY
  unset WATCHER_NOTIFIER_ENABLED
  NOTIFIER_REPORT="(cleared)"
  NOTIFIER_ENABLED_REPORT="(cleared)"
fi

# pytest builds watcher_test with Base.metadata.create_all, not alembic, so
# its alembic_version (if any) never matches the actual tables and a plain
# `upgrade head` fails mid-history. The test DB is disposable by definition,
# so when resolution fell back to TEST_DATABASE_URL the public schema is
# dropped and rebuilt from migrations — a deterministic state every launch.
# (The `information` schema — Archiver's, persisted between pytest sessions —
# lives outside `public` and is untouched.) An explicit
# WATCHER_DEV_DATABASE_URL is the persistent alternative: assumed
# alembic-managed, migrated in place, never reset.
#
# Migrating here keeps the safe path usable; an operator who finds it broken
# tends to reach for the old recipe that pointed at production.
#
# Decided once, so the dry-run report (which tests assert on) and the executed
# path cannot drift apart.
if [[ "${WATCHER_DEV_SKIP_MIGRATE:-}" == "1" ]]; then
  DO_RESET=0
  DO_MIGRATE=0
  MIGRATE_REPORT="(skipped)"
  RESET_REPORT="(none)"
elif [[ -n "${WATCHER_DEV_DATABASE_URL:-}" ]]; then
  DO_RESET=0
  DO_MIGRATE=1
  MIGRATE_REPORT="$DATABASE_URL"
  RESET_REPORT="(none)"
else
  DO_RESET=1
  DO_MIGRATE=1
  MIGRATE_REPORT="$DATABASE_URL"
  RESET_REPORT="public-schema"
fi

if [[ "${WATCHER_DEV_SERVER_DRY_RUN:-}" == "1" ]]; then
  echo "DATABASE_URL=$DATABASE_URL"
  echo "WATCHER_MIGRATION_DATABASE_URL=$WATCHER_MIGRATION_DATABASE_URL"
  echo "PROCRASTINATE_DATABASE_URL=(cleared)"
  echo "WATCHER_BUS_REDIS_URL=$BUS_REPORT"
  echo "WATCHER_BUS_ENABLED=$BUS_ENABLED_REPORT"
  echo "WATCHER_NOTIFIER_BASE_URL=$NOTIFIER_REPORT"
  echo "WATCHER_NOTIFIER_ENABLED=$NOTIFIER_ENABLED_REPORT"
  echo "PORT=$PORT"
  echo "MIGRATE=$MIGRATE_REPORT"
  echo "RESET=$RESET_REPORT"
  exit 0
fi

cd "$REPO_ROOT"

if [[ "$DO_RESET" == "1" ]]; then
  echo "dev_server: resetting public schema of $DATABASE_URL"
  psql "${DATABASE_URL/+asyncpg/}" -q -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
fi

if [[ "$DO_MIGRATE" == "1" ]]; then
  echo "dev_server: alembic upgrade head → $DATABASE_URL"
  uv run alembic upgrade head
fi

echo "dev_server: port $PORT → $DATABASE_URL"
# --log-config keeps uvicorn's own loggers on the app's JSON formatter, same as
# deploy/watcher.service (#244). Relative path: we cd'd to REPO_ROOT above.
exec uv run uvicorn src.api.main:app --host 0.0.0.0 --port "$PORT" --reload \
  --log-config src/core/log_config.json
