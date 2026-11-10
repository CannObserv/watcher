# Database migrations — history

The one-time deploy orderings, each **applied on co-watcher** (head
`c8e1d4f7a2b6` when they moved here, 2026-11-10), and two operator actions that were never
migrations. Moved verbatim out of [MIGRATIONS.md](MIGRATIONS.md) (#381), so
"the standard order above" and "#259 above" mean that file's opening block and
its *Migration role and application role* section. Each ordering governs only
a deploy that moves running code across that revision. A database already at
head needs none of them.

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

## Restart-before-migrate — one-time, `89e0c7725367` (#350)

Drops the five shadow-leg columns on `process_commands` the previous release
still maps (and adds #345's partial index) — #261's shape, #261's order:
restart, then `alembic upgrade head`. The new code never names the columns
(all nullable) and is correct without the index.

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

## Restart-before-migrate — one-time, `7f3d3491e921` (#374)

`7f3d3491e921` **drops** the `fetch_policy_tombstones` table, which carried a
deleted host's `revoked=True` through every `content.fetch-policy` full set
(#245). F1 handed that stream to Archiver, and part 2 deletes the producer and
the `FetchPolicyTombstone` model. The order is reversed for the same reason as
`10783d8a2405` above: the previous release still maps the table and writes it
on every domain create and delete, so dropping first fails those paths until
the restart lands.

```bash
sudo systemctl restart watcher      # new code first — it never names the table
uv run alembic upgrade head         # then drop it
```

Restart-first has no window: the new code never names the table, and nothing
has read it since F1 stopped Watcher's producer (2026-10-26). The rows are
dead; the nightly backup holds them, and the downgrade recreates the table
empty. No grants step: `watcher` owns it (#259 above). Subsequent deploys use
the standard order above.

The same release unregisters the `publish_fetch_policy` task, which part 1
kept only so a queued job drained as a no-op. Before the restart, confirm none
is left: `SELECT status, count(*) FROM procrastinate_jobs WHERE task_name =
'publish_fetch_policy' AND status IN ('todo', 'doing') GROUP BY status;` should
return no rows. A job left over fails as an unknown task rather than draining.

## Restart-before-migrate — one-time, `e2eab993d615` (#376)

`e2eab993d615` **drops** `watched_items.last_error_notified_at`, #71's
reminder clock. F3 handed notifications to Archiver (T =
`2026-11-06T21:00:00Z`), and part 2 deletes Watcher's dispatch path with the
reminder. The previous release still maps the column, and SQLAlchemy names
every mapped column in its SELECTs, so the order is reversed — `f4a8b26c9d31`'s
shape:

```bash
sudo systemctl restart watcher      # new code first — it never names the column
uv run alembic upgrade head         # then drop it
```

Restart-first has no window: the new code never names the column, and it is
nullable, so the old schema accepts the new code's INSERTs until the migration
lands. The stamps are dead; the nightly backup holds them, and the downgrade
recreates the column empty.

**Then, and only then, remove T.** The same release deletes the gate that read
`WATCHER_NOTIFY_CUTOVER_AT`. Until it is live, that line is all that stops the
old code dispatching beside Archiver, so it leaves `/etc/watcher/.env` **after**
the restart above — never before, and no backup copy:

```bash
sudo sed -i '/^WATCHER_NOTIFY_CUTOVER_AT=/d' /etc/watcher/.env
sudo grep -c '^WATCHER_NOTIFY_CUTOVER_AT' /etc/watcher/.env   # expect 0
```

No restart is needed for it: nothing reads the line any more. Subsequent
deploys use the standard order above.

## Stop-migrate-start — one-time, `c8e1d4f7a2b6` (#380)

`c8e1d4f7a2b6` **drops** the jump's retired schema: four tables
(`notification_templates`, `temporal_profiles`, `api_keys`, `app_users`), eight
`watched_items` columns and four `domains` columns (the list is in the
revision). Neither order above is safe. Migrate-first fails every WatchedItem
and Domain load the old release makes, because it still maps the columns.
Restart-first fails every reconcile create, because `watched_items.name` is
NOT NULL with **no server default**. (`domains.min_interval` is NOT NULL too,
but its default covers it.) So stop across it: `info.registry` replays from
`0-0` at boot and the groups resume, so there is no window, just seconds of
downtime.

```bash
source scripts/load-env.sh
sudo systemctl stop watcher
uv run alembic upgrade head          # c8e1d4f7a2b6
sudo systemctl start watcher
```

Before running it, confirm the latest nightly backup succeeded: it holds the
dropped values. Keep the stop clear of the backup timer. Production held no
archived or `probing` row on 2026-11-10, so the drop lifts no pause. The
downgrade recreates everything empty, with `name` **nullable** (NOT NULL fails
on a table with rows). No grants step: `watcher` owns every table (#259).
Subsequent deploys use the standard order.

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

## Migration baseline (squash) — one-time stamp — **history; nothing to run**

> co-watcher's database was created after the squash (it restored from a
> post-squash dump and reads `2f8bb8f7100a`), so the gate below **cannot** pass
> here and is not meant to: a "mismatch" on this host is the healthy answer.
> This section applies only to a database that predates #234.

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

> On a pre-#234 database this gate passes on the first read, and a mismatch
> means something unusual happened to it: do not improvise the stamp, reconcile
> as above. On co-watcher it simply does not apply — see the note above.

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
At the time, `tests/conftest.py` built a real `information` schema in
`TEST_DATABASE_URL` by invoking Archiver's own alembic and kept it alive between
sessions for the #150 cache-check, so a migration dropping the schema would have
broken every local test run and CI's `test` job. #311 has since removed that
schema from the test database too (see [COMMANDS.md](COMMANDS.md) → "The test
database"). `tests/test_migration_chain.py` fails the suite if a
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
