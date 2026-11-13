# Database Migrations

Migrations are **not** run by the systemd unit or the app lifespan — they are a
manual step. After a deploy that changes DB models:

```bash
source scripts/load-env.sh
uv run alembic upgrade head
sudo systemctl restart watcher
```

A fresh host bootstraps the full schema the same way (`alembic upgrade head`
against an empty database). The chain is self-contained — it references no
Archiver-owned schema — and is smoke-checked in CI (`migrations` job, #234).

Alembic connects with `WATCHER_MIGRATION_DATABASE_URL` when it is set and
`DATABASE_URL` otherwise (#259), so the command above is unchanged either way.
`alembic.ini` carries **no** URL: it used to default to the production database
with a guessable password, which made "migrate production" the behaviour of a
shell that forgot `source scripts/load-env.sh`. With neither variable set the
run now fails instead.

## Migration role and application role — one-time (#259)

Splits the single `watcher` role in two: `watcher` keeps owning and migrating
the schema, and a new `watcher_app` serves it with `SELECT/INSERT/UPDATE/DELETE`
and no DDL.

> **Done on co-watcher** — both roles exist and `DATABASE_URL` already names
> `watcher_app`, with `WATCHER_MIGRATION_DATABASE_URL` on `watcher`. What
> follows is for a **rebuilt or restored cluster**, where it is required rather
> than optional: grants are not schema state, so no migration recreates them,
> and step 1 rotates a live password if run against a working database.

A third role on the cluster, `watcher_backup`, is neither of these: the nightly
backup's read-only login (`pg_read_all_data`, peer auth only), created by
`scripts/setup-backup-role.sql` — see [RECOVERY.md](RECOVERY.md) (#297).

`scripts/setup-db-roles.sql` is additive for the roles: it creates one role,
grants it strictly less than exists today, and reassigns no ownership. Its one
subtraction is PUBLIC's `CONNECT` on the database (#388), after both roles hold
their own. `watcher` remains
the owner, which is why rollback is an env-file edit rather than a repair. It
also gives `watcher` `CREATEDB` — the missing attribute that forced
`9c1d4b7ea822` and `f4a8b26c9d31` to be hand-written and made per-agent test
databases a `sudo -u postgres` job.

**1. Apply the grants.** Read the script first; it runs against the live
database while the service is up.

```bash
# A password only this file and /etc/watcher/.env will ever hold.
APP_PW="$(openssl rand -base64 24 | tr -d '/+=')"

# Redirected, not `-f`: the postgres OS user cannot read under /home/exedev,
# so `-f scripts/...` fails with "Permission denied". The shell does the read.
# The password rides the environment: `sudo -u postgres VAR=…` puts it in `ps`.
WATCHER_APP_PASSWORD="$APP_PW" sudo --preserve-env=WATCHER_APP_PASSWORD -u postgres \
  psql -d watcher < scripts/setup-db-roles.sql
```

It prints two tables when it finishes. Check them before going further:
`watcher_app` must show `createdb=f`, `schema_usage=t`, **`schema_create=f`**,
and `sel/ins/upd/del = t` on all 17 tables except `alembic_version`, which is
`t/f/f/f`. Re-running the script is safe — it re-asserts the attributes and
rotates the password.

**2. Prove the new role can serve, and cannot DDL.** Still before touching the
env file, so a failure here costs nothing:

```bash
APP_URL="postgresql://watcher_app:${APP_PW}@localhost:5432/watcher"

psql "$APP_URL" -c "SELECT count(*) FROM watched_items"          # succeeds
psql "$APP_URL" -c "CREATE TABLE ddl_probe (x int)"              # permission denied for schema public
psql "$APP_URL" -c "DROP TABLE watched_items"                    # must be owner of table watched_items
psql "$APP_URL" -c "INSERT INTO alembic_version VALUES ('x')"    # permission denied for table alembic_version
psql "$APP_URL" -c "SELECT nextval('procrastinate_jobs_id_seq')" # succeeds — enqueue needs this
```

The three refusals are the whole point. If `CREATE TABLE` succeeds, the app role
holds `CREATE` on `public` and the split has bought nothing — stop and fix the
grant rather than proceeding.

**3. Point the two URLs at the two roles** in `/etc/watcher/.env`. The
migration credential is the value `DATABASE_URL` holds *right now*:

```ini
DATABASE_URL=postgresql+asyncpg://watcher_app:<APP_PW>@localhost:5432/watcher
WATCHER_MIGRATION_DATABASE_URL=postgresql+asyncpg://watcher:<existing>@localhost:5432/watcher
```

Both live in the same file, so every process that sources it holds the
migration credential — the weaker of the two options considered in #259,
accepted for deploy friction. The service is the exception:
`deploy/watcher.service` carries
`UnsetEnvironment=WATCHER_MIGRATION_DATABASE_URL` (#270), the only *unit*
directive that can remove a variable an `EnvironmentFile=` sets — the comment
on that line explains why `Environment=WATCHER_MIGRATION_DATABASE_URL=` cannot.
`alembic` run from a shell still resolves it. The guarantee underneath is
unchanged and is the one that matters: the *connection the app actually opens*
cannot execute DDL, whatever reaches it.

**4. Restart and confirm.**

```bash
sudo systemctl restart watcher
source scripts/load-env.sh                      # pick up the edited file
curl -s localhost:8000/ready                    # {"status":"ready","db":true,...}
sudo journalctl -u watcher -n 50 | grep -i 'permission denied' || echo "no permission errors"

# #270: the service must not hold the migration credential. Expect 0.
sudo tr '\0' '\n' < /proc/$(systemctl show watcher -p MainPID --value)/environ \
  | grep -c WATCHER_MIGRATION_DATABASE_URL
```

A non-zero count means `UnsetEnvironment=` is not in force — most often an
installed unit that was never `daemon-reload`ed, which
`tests/deploy/test_installed_unit_matches_repo.py` also checks.

Then confirm the queue still turns over — the readiness probe only proves
`SELECT 1`, while `procrastinate` needs sequences and functions:

```bash
psql "${DATABASE_URL/+asyncpg/}" -c \
  "SELECT status, count(*) FROM procrastinate_jobs GROUP BY status"
```

A `succeeded` count that grows over a few minutes means the embedded worker is
enqueuing and running under the new role. `permission denied for sequence` in
the journal is the signature of a missed sequence grant.

**5. Migrate as usual.** `uv run alembic upgrade head` now connects as `watcher`
through the new variable. Nothing about the command changes.

**Rollback.** Nothing in step 1 removes anything, so recovery is one line:
comment out the new `DATABASE_URL`, restore the previous `watcher` value, and
`sudo systemctl restart watcher`. The service is back on the owning role within
a restart, and the grants can stay in place while the failure is diagnosed.
Dropping the role entirely (`DROP OWNED BY watcher_app; DROP ROLE watcher_app;`)
is only needed to undo it permanently.

**What the split does not cover.** Grants are not schema state, so they live
outside the Alembic chain and no migration recreates them — a rebuilt or
restored database needs this script run again. Objects created by any role
*other* than `watcher` are not covered by the default privileges either: a
migration run as `postgres` leaves tables the app cannot read, with no error at
migrate time. And procrastinate's own schema changes are DDL, so they need the
migration credential too.

**A procrastinate upgrade migrates by hand.** Nothing applies its schema at
startup (#341: a missing one raises). `procrastinate schema --apply` is the
fresh-install schema and fails on an existing one. An upgrade applies the
release's versioned files from `$(uv run procrastinate schema --migrations-path)`,
named `<xx.yy.zz>_<nn>_<pre|post>_*.sql`: `pre` before the new code starts,
`post` after ([procrastinate: *Migrate the schema*](https://procrastinate.readthedocs.io/en/stable/howto/production/migrations.html)).
Before landing a bump, list the files newer than the installed version; none
means no DDL. 3.7.2 → 3.10.0 (#426) shipped none: the newest file is still
`03.04.00_50_post_add_retry_failed_job_procedure.sql`.

## The test role — one-time (#388)

`TEST_DATABASE_URL` logs in as `watcher_tester`, not `watcher`, so the
credential every agent shell loads cannot reach production. `watcher_tester`
has `LOGIN CREATEDB` and nothing else. `CREATEDB` is for the backup
rehearsal's scratch `*_test` databases (#383). It owns `watcher_test` and
`watcher_dev`. `watcher` owns only production, and its password lives only in
`/etc/watcher/.env`. What the test role owns, and the guard that checks it:
[COMMANDS.md](COMMANDS.md) → *The test database*.

> **Never `REASSIGN OWNED BY watcher`.** Besides the objects in the database
> it runs in, it moves every *database* `watcher` owns, production's included
> (measured on 16.15 in a rolled-back transaction, #388). The test databases
> are dropped and recreated instead. Both are disposable: pytest and
> `dev_server.sh` rebuild them.

Run it clear of the nightly backup (03:17 UTC plus its randomised delay) and
of the Monday patch window. Steps 2–4 are superuser steps over peer.

```bash
# 0. Nothing on the test databases: no pytest, no dev server, no leaked
#    rehearsal database. Both queries must print nothing.
sudo -u postgres psql -Atc "SELECT datname, usename FROM pg_stat_activity
  WHERE datname IN ('watcher_test','watcher_dev') OR datname LIKE 'watcher\_rh%'"
sudo -u postgres psql -Atc "SELECT datname FROM pg_database WHERE datname LIKE 'watcher\_rh%'"

# 1. The password, generated into this shell only. Hex, so the URL needs no
#    escaping. Never echo it.
TESTER_PW="$(openssl rand -hex 24)"

# 2. The old databases, owned by watcher.
sudo -u postgres psql -v ON_ERROR_STOP=1 -c "DROP DATABASE watcher_test" -c "DROP DATABASE watcher_dev"

# 3. The role and its databases. Redirected, not -f (postgres cannot read
#    /home/exedev). The password rides the environment, never argv.
WATCHER_TESTER_PASSWORD="$TESTER_PW" sudo --preserve-env=WATCHER_TESTER_PASSWORD \
  -u postgres psql -d postgres < scripts/setup-test-role.sql

# 4. PUBLIC's CONNECT on production. watcher_app and watcher_backup hold
#    their own; the owner always may. Not a re-run of setup-db-roles.sql,
#    which would also reset watcher_app's password.
sudo -u postgres psql -v ON_ERROR_STOP=1 -d watcher \
  -c "REVOKE CONNECT ON DATABASE watcher FROM PUBLIC"

# 5. The repo .env: TEST_DATABASE_URL now names the new role. Rewritten in
#    place, never opened in an editor (an editor's snapshots kept the last
#    leaked password, #388). Prints the user, host and database only.
TESTER_PW="$TESTER_PW" python3 - <<'PY'
import os, pathlib, urllib.parse as u
env = pathlib.Path(".env"); lines = env.read_text().splitlines(keepends=True)
for i, line in enumerate(lines):
    if line.startswith("TEST_DATABASE_URL="):
        p = u.urlsplit(line.split("=", 1)[1].strip())
        host = p.netloc.rsplit("@", 1)[1]
        new = p._replace(netloc=f"watcher_tester:{os.environ['TESTER_PW']}@{host}")
        lines[i] = f"TEST_DATABASE_URL={u.urlunsplit(new)}\n"
        print("TEST_DATABASE_URL -> watcher_tester@" + host + p.path)
env.write_text("".join(lines))
PY
unset TESTER_PW
```

**Read-back.**

```bash
source scripts/load-env.sh
DATABASE_URL="$TEST_DATABASE_URL" uv run python -m scripts.db_containment && echo contained
grep -c '://watcher:' .env                       # 0: the owner's URL is gone
sudo -u postgres psql -Atc "SELECT r, has_database_privilege(r, 'watcher', 'CONNECT')
  FROM unnest(ARRAY['watcher','watcher_app','watcher_backup','watcher_tester']) r"
#   t, t, t, f
sudo -u postgres psql -c '\du'
uv run pytest -m integration -q --no-cov         # green, no skips
systemctl show watcher -p NRestarts              # unchanged; no WARNING+ since
```

Then the next nightly backup: `systemctl show watcher-backup.service -p Result`
reads `success`.

**Rollback.** Run `GRANT CONNECT ON DATABASE watcher TO PUBLIC` as `postgres`.
The test role can stay while that's diagnosed: without the revoke it can
connect to production, but it owns nothing there. Undoing the role itself
means dropping its two databases and the role, then recreating both databases
`OWNER watcher`, and `TEST_DATABASE_URL` holds the owner's password again.
That leak is the one #388 closed.

## One-time orderings — applied

Some releases cannot use the standard order. Each such release adds a section
here, named by its revision, with the commands to run. Once that release is
applied on co-watcher, the section moves verbatim to the history file. The
shapes so far:

- **Restart before migrate:** the migration drops something the previous
  release still maps (SQLAlchemy names every mapped column in its SELECTs).
- **No safe order:** a new NOT NULL column that only the new release fills.
  Run both steps back to back.
- **Stop, migrate, start:** both of the above at once.

Every one of them is applied on co-watcher. They are recorded, with what each
dropped and why, in [MIGRATIONS-HISTORY.md](MIGRATIONS-HISTORY.md).
