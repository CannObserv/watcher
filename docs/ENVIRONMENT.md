# Environment

Every file and variable that configures a Watcher process: what loads what,
in which order, and which values a non-production launch path must never
inherit.

Split out of [DEPLOYMENT.md](DEPLOYMENT.md) — where install, systemd and timers
still live — when the two sections below reached 54% of a
document already over its budget.

## Environment Files

The service loads env files in this order (later values override earlier):

| File | Purpose | Required |
|---|---|---|
| `/run/watcher/build-id` | `BUILD_ID` (auto-generated from git SHA) | optional |
| `/etc/watcher/.env` | Production secrets (`DATABASE_URL`) | **yes** |

`/etc/watcher/.env` is owned by `root:exedev` (mode 640) and survives repo resets, worktree switches, and redeployments.

**The service does not load the repo-root `.env` (#296 D5).** That file is the
agent workspace's: `GH_TOKEN_*`, `ANTHROPIC_API_KEY`, `TEST_DATABASE_URL`, the
`WATCHER_DEV_*` opt-ins. Every one of them is marked *no* under **Prod** below,
and until #296 the unit loaded the file anyway, so the process serving the
dashboard held every token in it. `scripts/load-env.sh` still exports it into
shells, which is what it is for. A production setting belongs in
`/etc/watcher/.env`; a production credential belongs in a unit-only file.
`tests/deploy/test_installed_unit_matches_repo.py` fails a unit that loads any
env file from the checkout.

**The backup unit loads one env file of its own, and neither of the service's** (#296 D8/D9, #297): `/etc/watcher/backup.env` — the bucket, and the check-in's base URL and monitor id, no secret. Its two keys are not environment at all: `LoadCredential=` reads `/etc/watcher/co-watcher-backup.json` and `/etc/watcher/backup-checkin.key` (both `0400 root:root`, both required; the second is a co-status key for the `co-watcher` tenant (#330), empty until the monitor exists) and hands the run private copies under `$CREDENTIALS_DIRECTORY`. `watcher-backup.service` never loads `/etc/watcher/.env` — it holds no database credential and must not hold the bus URL. See [RECOVERY.md](RECOVERY.md); installing them: [RECOVERY-PROVISIONING.md](RECOVERY-PROVISIONING.md).

For shell commands that need secrets:

```bash
source scripts/load-env.sh
```

It parses each file and never runs it. A runbook line that needs one file
names it, and `/dev/null` keeps the repo `.env`'s tokens out — `backup.env`
under `sudo` is the case in point:

```bash
WATCHER_SYSTEM_ENV_FILE=<file> WATCHER_PROJECT_ENV_FILE=/dev/null . scripts/load-env.sh
```

Never source a file under `set -a`: `tests/scripts/test_dev_server.py` fails
that across `scripts/` and `docs/` (#407).

## Environment Variables

**Naming rule for new variables.** Anything naming a shared external resource
takes a **service-prefixed** name with a separate dev key — Archiver's
`ARCHIVER_REDIS_URL` / `ARCHIVER_DEV_REDIS_URL` split is the pattern. A bare
unprefixed name (`REDIS_URL`) is silently inherited from `/etc/watcher/.env` by
anything that sources it, which is exactly how a dev process ends up pointed at
a production resource (the #233 hazard, in env-var form). Watcher's own
`WATCHER_BUS_REDIS_URL` / `WATCHER_DEV_BUS_REDIS_URL` split (#245) follows the
pattern — see [BUS.md](BUS.md).

| Variable | Location | Required | Purpose |
|---|---|---|---|
| `DATABASE_URL` | `/etc/watcher/.env` | **yes** | PostgreSQL connection string the **application** connects with. Once the role split is applied (below) this names `watcher_app`, which holds DML and no DDL |
| `WATCHER_MIGRATION_DATABASE_URL` | `/etc/watcher/.env` | no | Connection string **Alembic** connects with — the schema owner, `watcher` (#259). Unset → falls back to `DATABASE_URL`, which is the pre-split behaviour and what every host uses until `scripts/setup-db-roles.sql` has run. Set-but-empty counts as unset — which is what the shell launchers rely on; the service has it removed outright. `scripts/dev_server.sh` overwrites it with the dev database, and `tests/conftest.py` pins it to `TEST_DATABASE_URL`: it is the one variable that can drop tables, so it is never inherited by a non-production launch path. `deploy/watcher.service` drops it from the service process with `UnsetEnvironment=` (#270) — only `alembic/env.py` ever reads it |
| `PROCRASTINATE_DATABASE_URL` | `/etc/watcher/.env` | no | libpq-style DSN for procrastinate; falls back to `DATABASE_URL` with driver prefix stripped |
| `GH_TOKEN` | `.env` | no | GitHub personal access token |
| `TEST_DATABASE_URL` | `.env` | no | PostgreSQL connection string for test database |
| `BUILD_ID` | env | no | Git SHA for static asset cache-busting (default `"dev"`) |
| `WATCHER_BACKUP_BUCKET` | `/etc/watcher/backup.env` | backup unit | Bucket the nightly dump is created in (`co-gcs-watcher-backup`); no default — unset fails the run and checks in `alert` (#296 D8) |
| `WATCHER_BACKUP_PREFIX` | `/etc/watcher/backup.env` | no | Object prefix the backup writes under; defaults to the hostname, so each host's dumps list as their own timeline. Unset here, and this host is `co-watcher`. The restore never reads it — a restore runs on another host, so `--prefix` names the source. **The bucket holds two timelines:** `co-watcher/` is live, `watcher/` is the retired VM's and its newest object post-dates the #296 cutover, so `--prefix watcher` restores 2026-09-15 |
| `WATCHER_BACKUP_CHECKIN_BASE_URL` | `/etc/watcher/backup.env` | backup unit | Base URL of the dead-man check-in (#296 D9): `http://status:9000`, co-status since #330. Named for no service (#332); the old `WATCHER_BACKUP_NOTIFIER_BASE_URL` is not read: set alone it is an ERROR naming the rename, beside the new one a WARNING to remove it. Configuration, not a constant — see RECOVERY-PROVISIONING.md for why a port typo still alarms rather than inverts |
| `WATCHER_BACKUP_MONITOR_ID` | `/etc/watcher/backup.env` | backup unit | The co-status monitor (`co-watcher-backup`, id unchanged from notifier) the backup checks in to on every run. All three or none: this, the base URL, and the check-in key — a systemd credential, not a variable (#297); `WATCHER_BACKUP_NOTIFIER_API_KEY` is no longer read |
| `WATCHER_ALLOW_PRODUCTION_DB` | `deploy/watcher.service` **only** | prod only | `=1` opts into serving a database whose name lacks a `_test`/`_dev` suffix (`src/core/db_safety.py`, #233). Must live in the systemd unit, never an env file — env files are loaded by hand-run dev servers, which are exactly what the guard stops |
| `WATCHER_DEV_DATABASE_URL` | `.env` | no | Persistent dev database for `scripts/dev_server.sh`; wins over `TEST_DATABASE_URL` |
| `WATCHER_BUS_REDIS_URL` | `/etc/watcher/.env` | prod | Redis URL of the broker, over the tailnet (`redis://watcher:<password>@broker:6379/0`) for the `info.watch-status` producer (#264; `content.fetch-policy` too until F1, #374). Unset → the periodic publish task skips with an ERROR log: Archiver's watched-item panel / drift detector go stale. **Not sufficient on its own since #262** — see `WATCHER_BUS_ENABLED` |
| `WATCHER_BUS_ENABLED` | `deploy/watcher.service` **only** | prod only | `=1` opts this process into building a bus client at all (`src/core/bus.py`, #262). Without it `bus_client_from_env()` returns `None`, so nothing publishes and no consumer starts — **and a URL held without it aborts startup** with `BusNotEnabled`, so a unit that lost the line fails loudly instead of going quiet. Must live in the systemd unit, never an env file: `WATCHER_BUS_REDIS_URL` does live in one, so every process that sources `/etc/watcher/.env` inherits the production broker address, and the flag is the only thing separating the service from an agent shell or a REPL. `scripts/dev_server.sh` sets it for itself when `WATCHER_DEV_BUS_REDIS_URL` names a scratch bus |
| `WATCHER_WATCH_STATUS_REPUBLISH_CRON` | env | no | Cron expression for the `info.watch-status` full-set republish (default `*/5 * * * *`, #264). The period is the recovery bound for a dropped frame — this stream has no outbox by design; loss is corrected by the next full set. A malformed value falls back to the default with an ERROR log |
| `WATCHER_WATCH_STATUS_STREAM_MAXLEN` | env | no | Producer-enforced retention cap for `info.watch-status` (`XADD MAXLEN ~`, default `500`). The full set republishes forever, so an untrimmed stream grows without bound; invalid/non-positive values fall back to the default with a warning — never to unbounded. **Floored at 10 full sets** of whatever batch is being published, so a corpus larger than the cap raises it rather than being trimmed below one full set (#292) — which is why a value *below* the floor is silently raised too |
| `WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS` | env | no | Reaper timeout for an in-flight command with no fact (default `1800` — deliberately generous; Replicator's reclaim cadence is an operator knob, and a tight value re-issues under live retries) |
| `WATCHER_CONDITIONAL_GET_ENABLED` | env | no | Which items may replay stored conditional-GET validators (#269). Unset/falsey → off for everything and every command is byte-identical to the pre-#269 one; `true` → the whole fleet; anything else is read as a comma-separated list of WatchedItem ids, which is the canary position. Safe only while replicator#17 and #249 part 1 are both deployed — a validator sent to a Replicator that classifies 304 as a plain fetch failure marks a healthy item ERROR and notifies a user about it on every no-change check |
| `WATCHER_VALIDATOR_MAX_AGE_HOURS` | env | no | How long a stored validator pair may be replayed before one unconditional re-fetch is forced (default `168`). The residual net under the deterministic invalidation rules — an origin whose ETag tracks a template rather than the watched region would otherwise hold a fingerprint inherited indefinitely. Not the blob-renewal bound: replay also stops at half the fact's `blob_expires_at` horizon whatever this is set to (#339, [CONDITIONAL-GET.md](CONDITIONAL-GET.md)). An unparseable value falls back to the default rather than raising, and a zero or negative value disables replay entirely (logged at INFO): the knob must not be able to wedge the issue path |
| `GOOGLE_APPLICATION_CREDENTIALS` | `/etc/watcher/.env` | for `uv lock` | The `co-pypi-reader` key; only `scripts/co-pypi.sh` reads it (#358). |
| `UV_INDEX_CO_PYPI_*` | one `uv` command **only** | for `uv lock` | co-pypi auth, set by `scripts/co-pypi.sh` or CI; **never** in an env file ([DEPENDENCIES.md](DEPENDENCIES.md)). |
| `WATCHER_FETCH_MAX_REISSUES` | env | no | Re-issues per fetch intent before it fails with ERROR health (default `3`). Caps the *lineage*, not one path: the reaper's stall sweep, the process reaper and the processor's `input_unreadable` (#275) all read it, one counter spanning both legs (#326) |
| `WATCHER_PROCESS_COMMAND_TIMEOUT_SECONDS` | env | no | How long an in-flight process command may go without a fact before the reaper may re-issue it (default `1800`) — **only once the processor has read past it** (answered a command published after it); otherwise it is held. Unparseable → the default |
| `WATCHER_PROCESS_COMMAND_HARD_LIMIT_SECONDS` | env | no | How long a process command may stay in flight (or unpublished) at all before its lineage ends, consuming or not (default `86400`): the check fails `processing_timeout` and the item goes ERROR (#326). The command whose failure fact was refused never gets one (processor#17) |
| `WATCHER_DEV_BUS_REDIS_URL` | `.env` | no | Scratch-bus opt-in for `scripts/dev_server.sh`; without it the dev server **clears** an inherited `WATCHER_BUS_REDIS_URL` (and `WATCHER_BUS_ENABLED`) so it cannot publish policy onto the production stream. With it, the script exports both, since the flag is otherwise unit-only |

**Retired variables.** Removed from `/etc/watcher/.env` (under #277, #376 and
#379) after each was confirmed to have no reader in `src/` or `scripts/`
(`tests/test_notification_subsystem_retired.py` pins #376's and #379's). Historical
plan documents under `docs/plans/` still describe them and are left as written
— they are a record of what was true then, not instructions:

| Variable | Retired because |
|---|---|
| `USE_REMOTE_NOTIFY` | The local Apprise path it switched between is gone; the remote path is the only path. `docs/plans/2026-05-02-notifier-adapter.md` still names it as a rollback lever — it is not one, and flipping it does nothing |
| `ARCHIVER_BASE_URL` | Watcher makes no HTTP calls to Archiver since #254; the SDK went with them |
| `ARCHIVER_API_KEY` | Same. Removing it from this file does **not** revoke it — the key is presumably still valid on Archiver's side, where revocation is tracked as [CannObserv/archiver#186](https://github.com/CannObserv/archiver/issues/186). Until that closes, treat the credential as live |
| `APPRISE_SECRET_KEY` | The Fernet key for encrypted Apprise URLs. Verified no ciphertext column survives in the schema before deletion |
| `WATCHER_ERROR_RENOTIFY_INTERVAL` | #71's reminder gap. Watcher sends no reminder since F3 (#376): Archiver owns the cadence. Never set in production, so nothing to remove |
| `WATCHER_NOTIFY_CUTOVER_AT` | F3's gate (#376), set to `2026-11-06T21:00:00Z` at the flip. Part 2 deleted the gate with the dispatch path, so nothing reads it; the operator removes the line **after** part 2 is live (before, a restart without it double-sent): [MIGRATIONS.md](MIGRATIONS.md) → *Restart-before-migrate — one-time, `e2eab993d615`* |
| `WATCHER_NOTIFIER_BASE_URL`, `WATCHER_NOTIFIER_API_KEY` | The production notifier credential (renamed from bare `NOTIFIER_*` under #277). #278 moved it out of this file into `/etc/watcher/notifier.env`, `600 root:root`, loaded by the unit alone and last, because every agent shell exports this one and notifier's audit (notifier#22) found ~1289 watcher fixture notifications delivered on it. #379 deleted the client and dropped the file from the unit; the operator shreds it, with no copy, once the new unit is live, and CannObserv/notifier#118 revokes the key |
| `WATCHER_NOTIFIER_ENABLED` | The unit-only opt-in for the notifier client (#277); with #278 a URL without it, or it without a URL, aborted startup. Went from `deploy/watcher.service` with the client (#379) |
| `WATCHER_DEV_NOTIFIER_BASE_URL`, `WATCHER_DEV_NOTIFIER_API_KEY` | `scripts/dev_server.sh`'s scratch notifier (notifier's development tenant on `:9001`, #278). The operator removes them from the repo `.env` after #379 is live |
| `WATCHER_PUBLIC_BASE_URL`, `WATCHER_DEV_PUBLIC_BASE_URL` | The dashboard base notification links were built from (#296 D6). No link is built any more (#379); the operator removes the production line after #379 is live |
| `WATCHER_DIFF_MAX_INPUT_BYTES` | The change diff's input cap (#222). The diff went with the notification renderer (#379). Never set in production |
| `GCS_BLOB_CREDENTIALS` | The `co-gcs-blob-reader` key (`/etc/watcher/co-gcs-blob-reader.json`) the change diff read the processor's stored texts with (#275, #350). `src/core/blobs.py` went with the diff (#379); the operator removes the line and the key file after #379 is live |

**Watcher's Redis use.** The broker is its own VM, operated from
CannObserv/broker (archiver#193 D6; archiver#109 before that). Watcher published
`content.fetch-policy` (#245) until F1 (#374) and — Phase 4, #241 — publishes `content.fetch`
commands and consumes `content.blobs` facts via its own consumer group
(`watcher.blobs`, started in the lifespan when `WATCHER_BUS_REDIS_URL` is set
**and** `WATCHER_BUS_ENABLED=1`, #262). Since #254 it also consumes
`info.registry` **grouplessly**, replayed from `0-0` at boot; without a usable
bus no consumer starts and the registry cannot converge. Since #264 it
publishes `info.watch-status` — the return leg of the registry channel: applied
generation, scheduler state, and observation freshness per InfoItem, full-set
republished on `WATCHER_WATCH_STATUS_REPUBLISH_CRON` (default every 5 minutes)
including tombstones from `revoked_info_items`. **Health primitive: last-entry
age** — `XINFO STREAM info.watch-status` (its `last-entry`) should never be
older than the republish period while the service is up; `XREVRANGE` is not in
watcher's ACL, so use `XINFO STREAM` or `XRANGE` (broker#77). An aging stream with a
live service means the publish task is failing (check Procrastinate job errors),
and Archiver's panel renders drift from exactly this staleness. All queued work
stays on Procrastinate over Postgres. See [BUS.md](BUS.md) for the ownership
split.

