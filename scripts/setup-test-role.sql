-- The test suite's own database role (#388).
--
-- READ THIS BEFORE RUNNING IT. It runs as the superuser on the cluster that
-- production lives on. It only creates: one role, and its two databases when
-- they are absent.
--
--   WATCHER_TESTER_PASSWORD="$TESTER_PW" sudo --preserve-env=WATCHER_TESTER_PASSWORD \
--     -u postgres psql -d postgres < scripts/setup-test-role.sql
--
-- Redirected, not `-f`: the `postgres` OS user cannot read anything under
-- /home/exedev. Connected to `postgres`, not to a database it creates.
--
-- The password never appears in this file: \getenv reads it. Where it comes
-- from, the one-time drop of the old databases that must come first, the
-- REVOKE on production, the .env edit, and the read-back:
-- docs/MIGRATIONS.md -> "The test role".
--
--
-- WHAT IT DOES
--
--   watcher_tester  LOGIN CREATEDB, nothing else. Owns watcher_test and
--                   watcher_dev, so pytest's create_all/drop_all and
--                   dev_server.sh's public-schema reset run as it; CREATEDB
--                   because the backup rehearsal creates its scratch
--                   *_test databases as whoever TEST_DATABASE_URL logs in as
--                   (#383). No CREATEROLE, no membership, no superuser and
--                   no pg_signal_backend: #383's drop falls back to a plain
--                   DROP without it, and that role is cluster-wide.
--
-- WHY A DATABASE HELD BY ANOTHER ROLE STOPS IT. A watcher_test owned by
-- `watcher` holds `watcher`'s tables, which the new owner could not drop. It is
-- never adopted with REASSIGN OWNED: that command also moves every *database*
-- the old role owns — production's included, whichever database it runs in.
-- Drop the old one by hand (the runbook), then re-run this.
--
-- IDEMPOTENT. Re-running re-asserts the role's attributes and sets its
-- password to whatever WATCHER_TESTER_PASSWORD currently holds.
--
-- NOT A MIGRATION, like scripts/setup-db-roles.sql: roles are cluster state.

\set ON_ERROR_STOP on

-- psql variables, so a rehearsal against a scratch cluster runs these exact
-- bytes with -v tester_role=... -v test_db=... -v dev_db=... .
\if :{?tester_role}
\else
  \set tester_role watcher_tester
\endif

\if :{?test_db}
\else
  \set test_db watcher_test
\endif

\if :{?dev_db}
\else
  \set dev_db watcher_dev
\endif

-- A missing password aborts through a real error, not \quit, which exits 0.
\getenv tester_password WATCHER_TESTER_PASSWORD
\if :{?tester_password}
\else
  \warn 'WATCHER_TESTER_PASSWORD is not set — refusing to create a role without a password.'
  DO $abort$ BEGIN
    RAISE EXCEPTION 'WATCHER_TESTER_PASSWORD is not set';
  END $abort$;
\endif

\echo test role :tester_role ; databases :test_db :dev_db

BEGIN;

SELECT format('CREATE ROLE %I LOGIN', :'tester_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'tester_role');
\gexec

-- Re-asserted every run, so a role that acquired an attribute by hand loses it
-- again. NOINHERIT: no membership granted later takes effect silently.
ALTER ROLE :"tester_role" WITH LOGIN NOSUPERUSER CREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD :'tester_password';

-- Refuse, inside the transaction so the role change rolls back with it, when
-- either database exists under another owner. \gexec runs the DO block only
-- for a row the SELECT returns.
SELECT format(
  'DO $refuse$ BEGIN RAISE EXCEPTION %L; END $refuse$',
  format('%s exists, owned by %s, not %s: see docs/MIGRATIONS.md -> "The test role"',
         d.datname, pg_get_userbyid(d.datdba), :'tester_role'))
FROM pg_database d
WHERE d.datname IN (:'test_db', :'dev_db')
  AND d.datdba <> (SELECT oid FROM pg_roles WHERE rolname = :'tester_role');
\gexec

COMMIT;

-- CREATE DATABASE cannot run inside a transaction block. A fresh database's
-- `public` schema belongs to pg_database_owner, so the owner gets it too.
SELECT format('CREATE DATABASE %I OWNER %I', name, :'tester_role')
FROM unnest(ARRAY[:'test_db', :'dev_db']) AS name
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = name);
\gexec

-- Report. Read-only. The role row must read t for login and createdb, f for
-- every other column. The second query lists every database the role can
-- connect to or acts as the owner of: only the *_test and *_dev ones and the
-- maintenance set (postgres, template0/1) may appear. `watcher` appears
-- until PUBLIC's CONNECT on it is revoked (the runbook's next step).
SELECT
  r.rolname,
  r.rolcanlogin AS login,
  r.rolsuper AS superuser,
  r.rolcreatedb AS createdb,
  r.rolcreaterole AS createrole,
  r.rolinherit AS inherit,
  r.rolreplication AS replication,
  r.rolbypassrls AS bypassrls,
  (SELECT count(*) FROM pg_auth_members m WHERE m.member = r.oid) AS memberships
FROM pg_roles r
WHERE r.rolname = :'tester_role';

SELECT
  d.datname,
  pg_get_userbyid(d.datdba) AS owner,
  has_database_privilege(:'tester_role', d.oid, 'CONNECT') AS can_connect,
  pg_has_role(:'tester_role', d.datdba, 'USAGE') AS acts_as_owner
FROM pg_database d
WHERE has_database_privilege(:'tester_role', d.oid, 'CONNECT')
   OR pg_has_role(:'tester_role', d.datdba, 'USAGE')
ORDER BY d.datname;
