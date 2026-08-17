-- Separate the migration role from the application role (#259).
--
-- READ THIS BEFORE RUNNING IT. It executes against the live `watcher`
-- database while the service is up, and a wrong grant here locks the running
-- service out of its own schema.
--
--   sudo -u postgres WATCHER_APP_PASSWORD='<generated>' \
--     psql -d watcher -f scripts/setup-db-roles.sql
--
-- The password never appears in this file: it is read from the environment by
-- \getenv below. Run order, the /etc/watcher/.env edit that follows, the
-- verification commands, and the rollback are in
-- docs/DEPLOYMENT.md -> "Migration role and application role".
--
--
-- WHAT IT DOES
--
--   watcher      (unchanged)  owns the schema, keeps DDL rights, gains
--                             CREATEDB. This is the *migration* role; alembic
--                             connects with it via
--                             WATCHER_MIGRATION_DATABASE_URL.
--   watcher_app  (new)        LOGIN, no attributes, SELECT/INSERT/UPDATE/
--                             DELETE on existing tables plus sequence usage.
--                             No DDL. This is what the service connects with
--                             via DATABASE_URL.
--
-- WHY THE INCUMBENT ROLE IS THE MIGRATION ROLE, and not a new `watcher_migrate`
-- as #259 proposed: `watcher` already owns the database, all 17 public tables,
-- their sequences, and the procrastinate routines. Introducing a *new* owner
-- means reassigning every one of those on a live database — for no additional
-- guarantee, because the role being constrained is the application's. Keeping
-- the incumbent makes this script purely additive: it creates one role, grants
-- it strictly less than exists today, and touches nothing the running service
-- depends on. Rollback is an env-file edit, not a repair.
--
-- IDEMPOTENT. Re-running is a no-op that also re-asserts the role's attributes
-- and rotates its password to whatever WATCHER_APP_PASSWORD currently holds.
--
-- NOT A MIGRATION. Deliberately outside the alembic chain: grants are host
-- state, not schema state, and a migration that granted them would run with
-- whatever role happened to be connected.

\set ON_ERROR_STOP on

-- Role names are psql variables so a rehearsal against a scratch cluster runs
-- these exact bytes with -v app_role=... -v migrate_role=... . Defaults apply
-- when nothing was passed, so the operator invocation above needs neither.
\if :{?app_role}
\else
  \set app_role watcher_app
\endif

\if :{?migrate_role}
\else
  \set migrate_role watcher
\endif

-- The password is never in this file. A missing one aborts through a real
-- error, not \quit: \quit exits 0, so "no password supplied" would be
-- indistinguishable from "roles configured" to anyone checking $?.
\getenv app_password WATCHER_APP_PASSWORD
\if :{?app_password}
\else
  \warn 'WATCHER_APP_PASSWORD is not set — refusing to create a role without a password.'
  DO $abort$ BEGIN
    RAISE EXCEPTION 'WATCHER_APP_PASSWORD is not set';
  END $abort$;
\endif

-- Unquoted: psql does not interpolate variables inside single quotes, so the
-- quoted form would print the literal `:app_role` and mislead a rehearsal.
\echo granting to application role :app_role ; migration role :migrate_role

BEGIN;

-- 1. The application role. Created only when absent; \gexec runs the rows the
--    SELECT returns, and it returns none when the role already exists.
SELECT format('CREATE ROLE %I LOGIN', :'app_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role');
\gexec

-- Re-asserted every run, so a role that acquired an attribute by hand loses it
-- again. NOINHERIT matters more than it looks: without it, any future
-- membership granted to this role would take effect silently.
ALTER ROLE :"app_role" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD :'app_password';

-- 2. The migration role gains CREATEDB. This is the friction #259 was filed
--    over: without it `alembic revision --autogenerate` has no scratch
--    database to diff a migrated schema against, so migrations get hand-written
--    (9c1d4b7ea822, f4a8b26c9d31) and per-agent test databases need a
--    superuser. Nothing else about the role changes.
ALTER ROLE :"migrate_role" WITH CREATEDB;

-- 3. Reach the database and the schema. Both are already granted to PUBLIC by
--    default on this cluster; stated explicitly so a later REVOKE ... FROM
--    PUBLIC hardening pass does not silently take the service down.
--    CREATE on the schema is deliberately absent — that is the DDL right the
--    application must not hold.
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_role');
\gexec

GRANT USAGE ON SCHEMA public TO :"app_role";

-- 4. Existing objects.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"app_role";

-- procrastinate_jobs/_events/_workers/_periodic_defers are bigserial. Without
-- USAGE here every enqueue fails while reads keep working.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_role";

-- procrastinate installs ~18 routines (defer, fetch, trigger functions) that
-- the worker calls on every job. EXECUTE is granted to PUBLIC by default; the
-- explicit grant is again insurance against a REVOKE FROM PUBLIC pass.
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO :"app_role";

-- 5. Future objects. Without this, the first migration that adds a table ships
--    a runtime failure: the table exists, the app cannot read it, and nothing
--    fails at migrate time. Keyed to the creating role, so it applies to
--    exactly the objects `alembic` creates while connected as :migrate_role —
--    a migration run as any other role (postgres, say) is NOT covered, and
--    needs a matching GRANT by hand.
ALTER DEFAULT PRIVILEGES FOR ROLE :"migrate_role" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_role";

ALTER DEFAULT PRIVILEGES FOR ROLE :"migrate_role" IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"app_role";

ALTER DEFAULT PRIVILEGES FOR ROLE :"migrate_role" IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO :"app_role";

-- 6. One exception to the blanket table grant: alembic_version records which
--    migrations have run, and the application has no business writing it. A
--    stray write there would make the next `alembic upgrade head` run against a
--    schema it has mis-stamped. SELECT is left in place so an operator query
--    through the app's credential still works. Guarded because a freshly
--    bootstrapped database has no such table yet.
SELECT format('REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM %I', :'app_role')
WHERE EXISTS (
  SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'alembic_version'
);
\gexec

COMMIT;

-- 7. Report. Read-only; this is what to eyeball before editing
--    /etc/watcher/.env. The application role must show no attributes, and
--    `has_schema_privilege(..., 'CREATE')` must be false.
SELECT
  r.rolname,
  r.rolsuper AS superuser,
  r.rolcreatedb AS createdb,
  r.rolcreaterole AS createrole,
  has_database_privilege(r.rolname, current_database(), 'CONNECT') AS can_connect,
  has_schema_privilege(r.rolname, 'public', 'USAGE') AS schema_usage,
  has_schema_privilege(r.rolname, 'public', 'CREATE') AS schema_create
FROM pg_roles r
WHERE r.rolname IN (:'app_role', :'migrate_role')
ORDER BY r.rolname;

SELECT
  c.relname AS table_name,
  has_table_privilege(:'app_role', c.oid, 'SELECT') AS sel,
  has_table_privilege(:'app_role', c.oid, 'INSERT') AS ins,
  has_table_privilege(:'app_role', c.oid, 'UPDATE') AS upd,
  has_table_privilege(:'app_role', c.oid, 'DELETE') AS del
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY c.relname;
