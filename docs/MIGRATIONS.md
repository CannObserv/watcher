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
and no DDL. **The code half is already deployed and is a no-op until this runs**
— the fallback above means a single-role database behaves exactly as before.

`scripts/setup-db-roles.sql` is purely additive: it creates one role, grants it
strictly less than exists today, and reassigns no ownership. `watcher` remains
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
sudo -u postgres WATCHER_APP_PASSWORD="$APP_PW" \
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
migrate time. And `procrastinate schema --apply` on a procrastinate upgrade is
DDL, so it needs the migration credential too.

## Restart-before-migrate — one-time, `d5a71c93e0f2` (#251)

`d5a71c93e0f2` makes `watched_items.archiver_info_item_id` and
`archiver_info_source_id` NOT NULL. The same release deletes the two code paths
that could produce a row without them (the dashboard create form and the API's
URL-only branch), so for **this one migration** the order above is reversed:

```bash
sudo systemctl restart watcher      # new code first — no path can write a bare row
uv run alembic upgrade head         # then the constraint
```

Migrating first leaves the old code briefly serving `/watched-items/new`, whose
insert then violates the new constraint and 500s. Production held zero bare rows
at the time of writing, so the migration itself is a metadata-only lock on four
rows; the ordering is about the window, not the data. Subsequent deploys use the
standard order above.

## Restart-before-migrate — one-time, `f4a8b26c9d31` (#261)

`f4a8b26c9d31` **drops** three columns the previous release still maps:
`pending_archiver_sync.content_cache_uri` / `content_cache_expires_at` and
`change_revisions.archiver_revision_id`. SQLAlchemy names every mapped column in
its SELECTs, so the order above is reversed for this one:

```bash
sudo systemctl restart watcher      # new code first — it no longer maps them
uv run alembic upgrade head         # then drop the columns
```

Migrating first would drop columns out from under the running process, and every
query against those two tables — the pipeline's last-fingerprint lookup and
`GET /watched-items/{id}/revisions` among them — would fail with
`UndefinedColumn` until the restart landed.

Restart-first has no equivalent window: the new code never references the three,
both cache columns were already released to nullable by `32140463c26c`, and
`archiver_revision_id` has always been nullable, so nothing the new code writes
needs them. `pending_archiver_sync` held zero rows at the time of writing and
`change_revisions` held 32, of which 23 carried an `archiver_revision_id` — those
values are destroyed by design (#261): Archiver identifies a SourceRevision by
`(info_source_id, content_fingerprint)`, so the mapping is re-derivable and the
local copy was redundant rather than unique. Subsequent deploys use the standard
order above.

## Restart-before-migrate — one-time, `10783d8a2405` (#272)

`10783d8a2405` **drops** the four inert `domains` columns the retired
in-process rate limiter (#241 step 5) left behind: `max_concurrency`,
`decay_window`, `current_interval`, `last_request_at`. Same reasoning as
`f4a8b26c9d31` above — the previous release still maps all four, so the order
is reversed:

```bash
sudo systemctl restart watcher      # new code first — it no longer maps them
uv run alembic upgrade head         # then drop the columns
```

Restart-first has no window: the new code never references them, and all four
carry genesis server defaults or are nullable, so the old schema accepts the
new code's INSERTs until the migration lands. The values are destroyed by
design — nothing has read them for behavior since #241 step 5 (`current_interval`
froze at the Phase-4 cutover; `last_request_at` had no writer at all).
Subsequent deploys use the standard order above.

## No safe order — one-time, `e7c4b2a91f60` (#252)

`e7c4b2a91f60` adds `fetch_commands.info_source_id` NOT NULL, and the release
that needs it is the same release that populates it. Unlike `d5a71c93e0f2`
above, **neither order avoids a window**:

- Migrate first → the still-running old code's `create_fetch_command` omits the
  column, and every INSERT raises `NotNullViolation`.
- Restart first → the new code names a column the database does not have yet.

Run the two back-to-back and accept the seconds in between:

```bash
source scripts/load-env.sh
uv run alembic upgrade head && sudo systemctl restart watcher
```

What fails in that window is bounded and self-healing, and it is only the two
paths that **INSERT** a command row: `check_watched_item` on each
`schedule_tick`, and the reaper's re-issues. Both are periodic — the next tick
after the restart succeeds, and no WatchedItem is left in a bad state. (The
pending-publish sweep and the fact consumer only UPDATE existing rows, so
neither is affected; a command already in flight rides the window out and its
row is backfilled.) In the journal it looks like

```
null value in column "info_source_id" of relation "fetch_commands"
```

on a handful of procrastinate jobs, then silence. That is the expected shape of
this deploy, not a symptom of something worse.

**This deploy also has a cross-service prerequisite:** Replicator must be
publishing `info_source_id` on its facts (CannObserv/replicator#28) *before*
watcher restarts onto co-core 0.8.0, or the fact consumer cannot decode them.
See [CONTENT-PIPELINE.md](CONTENT-PIPELINE.md) → "`info_source_id` on the wire".

## Migration baseline (squash) — one-time stamp

The pre-#234 migration chain was squashed into a single genesis baseline
(`2addddea0b03`, #234). This removed a transitional cross-schema FK into the
Archiver `information` schema that made `alembic upgrade head` fail from a clean
database.

Because the old version files were removed, an **already-migrated** database
(production, or any long-lived dev DB) has an `alembic_version` pointing at a
revision that no longer exists — a plain `upgrade head` there fails with
`Can't locate revision …`. **Once**, at the deploy that first lands the squash,
stamp the baseline instead of upgrading.

**First, confirm the database is exactly at the pre-squash HEAD** (`c5d6e7f8a9b0`,
the #218 audit-log-indexes migration). `stamp --purge` asserts the baseline
*regardless of where the DB actually is* — if the DB is behind `c5d6e7f8a9b0`,
stamping would silently mark the missing migrations as applied and corrupt the
schema. So this is a **halt-on-mismatch** gate, not a formality:

```bash
source scripts/load-env.sh
# psql needs a driverless URL — strip the SQLAlchemy "+asyncpg" dialect suffix.
psql "${DATABASE_URL/+asyncpg/}" -c "SELECT version_num FROM alembic_version"
```

- If it prints **`c5d6e7f8a9b0`** → proceed to the stamp below.
- **Any other value → STOP.** The DB is not at the pre-squash HEAD; do not stamp.
  Reconcile it first (upgrade it to `c5d6e7f8a9b0` using the pre-squash version
  files from git history, or investigate why it diverged) before squashing.

> Production is expected to already be at `c5d6e7f8a9b0` — this gate should pass
> on the first read. A mismatch means something unusual happened to the DB; do
> not improvise the stamp, reconcile as above.

```bash
# Only after confirming the version is c5d6e7f8a9b0:
uv run alembic stamp 2addddea0b03 --purge   # re-point bookkeeping; schema unchanged
uv run alembic upgrade head                 # subsequent upgrades work normally
```

`stamp` only rewrites the `alembic_version` bookkeeping row; it makes no schema
changes. Fresh databases created after the squash need no stamp — they run the
genesis migration normally.

> The squash intentionally dropped two pieces of dead cruft from the *baseline*
> (an orphaned `trg_fn_watches_last_changed_at()` function and the vestigial
> `notification_event_types` catalog table). Existing databases still carry them
> harmlessly after the stamp; an optional cleanup migration can drop them later.

## Drop the dead `information` schema — applied 2026-08-18 (#271)

**Done; nothing to run.** Unlike the one-time sections above, this one cannot
come back: they describe orderings a replayed chain re-enters, whereas the
genesis baseline (#234) creates no `information` schema, so no database built or
restored after it has one. Kept as the record of what was removed and why.

Production's `watcher` database *had* carried an empty `information` schema
(`info_items`, `info_sources`, both owned by `watcher`), left behind when #234
stopped mirroring Archiver's registry locally. Nothing in `src/` read or wrote
it, and #259's grants cover `schema public` only — so it sat outside the grant
model entirely, neither granted to `watcher_app` nor explicitly excluded, which
is the whole reason to remove it rather than leave it for whoever next audits
privileges. Zero rows: tidying, not risk.

**Operator action on production only — deliberately not a migration.**
`tests/conftest.py` builds a real `information` schema in `TEST_DATABASE_URL` by
invoking Archiver's own alembic and keeps it alive between sessions for the #150
cache-check (see [COMMANDS.md](COMMANDS.md) → "Tests require the Archiver sibling
repo"). A migration dropping the schema would therefore break every local test
run and CI's `test` job. `tests/test_migration_chain.py` fails the suite if a
version file ever creates or drops the schema — in raw SQL or through
SQLAlchemy's `CreateSchema`/`DropSchema` constructs.

What was run, and what it proved. `DROP SCHEMA` is DDL, so it took the
migration credential — `DATABASE_URL` is `watcher_app` since #259 and cannot
execute it. Re-running step 1 today reports `relation "information.info_items"
does not exist`; that is the schema being gone, not a broken instruction:

```bash
source scripts/load-env.sh
# psql needs a driverless URL — strip the SQLAlchemy "+asyncpg" dialect suffix.
MIG="${WATCHER_MIGRATION_DATABASE_URL/+asyncpg/}"

# 1. Confirmed dead: both counts 0, exactly those two tables, and `\d` on each
#    showed no `Referenced by:` — so CASCADE could only reach the two of them.
psql "$MIG" -c "SELECT count(*) FROM information.info_items"      # 0
psql "$MIG" -c "SELECT count(*) FROM information.info_sources"    # 0
psql "$MIG" -c "\dt information.*"

# 2. Dropped — "NOTICE: drop cascades to 2 other objects", both those tables.
psql "$MIG" -c "DROP SCHEMA information CASCADE"

# 3. Verified.
psql "$MIG" -c "\dn"              # `public` only
curl -s localhost:8000/ready       # {"status":"ready","db":true,"queue":true}
uv run alembic check               # No new upgrade operations detected
```

No restart was needed and nothing had to be re-granted: no connection the
service opens ever referenced the schema. Rollback would not be a restore — the
tables held no rows, so recreating them would mean re-running Archiver's alembic
against this database, which is precisely the mirror #234 removed.

**Archiver still owns `information`** — in *its own* database, where it is live
and canonical. Watcher's only remaining mention is a docstring in
`src/core/models/watched_item.py` naming Archiver's `information.info_items` as
the referent of `archiver_info_item_id`; that documents a foreign system and is
not a dependency. `alembic/env.py`'s autogenerate filter also drops every
non-`public` schema, so the copy tests build in `TEST_DATABASE_URL` can never
turn into a Watcher migration. Whether any *other* sibling's database carries a
leftover of its own is that repo's business, not this one's.
