# Environment

Every file and variable that configures a Watcher process: what loads what,
in which order, and which values a non-production launch path must never
inherit.

Split out of [DEPLOYMENT.md](DEPLOYMENT.md) — where install, systemd, timers
and the wheelhouse still live — when the two sections below reached 54% of a
document already over its budget.

## Environment Files

The service loads env files in this order (later values override earlier):

| File | Purpose | Required |
|---|---|---|
| `/run/watcher/build-id` | `BUILD_ID` (auto-generated from git SHA) | optional |
| `/etc/watcher/.env` | Production secrets (`DATABASE_URL`) | **yes** |
| `.env` (repo root) | Dev/agent overrides (`GH_TOKEN`, `TEST_DATABASE_URL`) | optional |
| `/etc/watcher/notifier.env` | The production notifier credential, unit-only (#278) | **yes** |

`/etc/watcher/.env` is owned by `root:exedev` (mode 640) and survives repo resets, worktree switches, and redeployments.

`/etc/watcher/notifier.env` is `600 root:root` and holds
`WATCHER_NOTIFIER_BASE_URL` + `WATCHER_NOTIFIER_API_KEY` and nothing else.
`scripts/load-env.sh` does not know the path, so no shell, suite or REPL
inherits the pair; systemd parses `EnvironmentFile=` as root before `User=`
takes effect, so the `exedev` account never needs to read it.

**Why it is separate.** #277 made the notifier credential unusable outside the
unit (`WATCHER_NOTIFIER_ENABLED`); it could not make it *unheld*. Every agent
shell on this VM sources `/etc/watcher/.env`, and notifier's audit
([CannObserv/notifier#22](https://github.com/CannObserv/notifier/issues/22),
watcher#278) found ~1289 watcher fixture notifications already delivered on that
key to the production Slack and Mailgun channels. A flag stops an accident; it
does not stop a deliberate `export`, and it does not stop the key being read out
of a process environment. Moving the file is what makes the credential
genuinely unavailable to anything but the service.

It is loaded **last** (env files apply in order, so nothing may override it) and
**required** — no `-` prefix, so an absent file fails the start rather than
leaving the service up and quietly un-notifying.
`src/core/notifier_client.assert_environment_notifier_allowed` makes the same
combination fatal in-app, which covers what systemd cannot see: a file present
but empty, truncated, or with the assignment renamed.

**Do not** re-add either variable to `/etc/watcher/.env` or to the repo `.env`.
Non-production runs point at notifier's *development* tenant instead — see
`WATCHER_DEV_NOTIFIER_BASE_URL` below.

For shell commands that need secrets:

```bash
source scripts/load-env.sh
```

## Environment Variables

**Naming rule for new variables.** Anything naming a shared external resource
takes a **service-prefixed** name with a separate dev key — Archiver's
`ARCHIVER_REDIS_URL` / `ARCHIVER_DEV_REDIS_URL` split is the pattern. A bare
unprefixed name (`REDIS_URL`) is silently inherited from `/etc/watcher/.env` by
anything that sources it, which is exactly how a dev process ends up pointed at
a production resource (the #233 hazard, in env-var form). Watcher's own
`WATCHER_BUS_REDIS_URL` / `WATCHER_DEV_BUS_REDIS_URL` split (#245) follows the
pattern — see **Redis and the bus**.

| Variable | Location | Required | Purpose |
|---|---|---|---|
| `DATABASE_URL` | `/etc/watcher/.env` | **yes** | PostgreSQL connection string the **application** connects with. Once the role split is applied (below) this names `watcher_app`, which holds DML and no DDL |
| `WATCHER_MIGRATION_DATABASE_URL` | `/etc/watcher/.env` | no | Connection string **Alembic** connects with — the schema owner, `watcher` (#259). Unset → falls back to `DATABASE_URL`, which is the pre-split behaviour and what every host uses until `scripts/setup-db-roles.sql` has run. Set-but-empty counts as unset — which is what the shell launchers rely on; the service has it removed outright. `scripts/dev_server.sh` overwrites it with the dev database, and `tests/conftest.py` pins it to `TEST_DATABASE_URL`: it is the one variable that can drop tables, so it is never inherited by a non-production launch path. `deploy/watcher.service` drops it from the service process with `UnsetEnvironment=` (#270) — only `alembic/env.py` ever reads it |
| `PROCRASTINATE_DATABASE_URL` | `/etc/watcher/.env` | no | libpq-style DSN for procrastinate; falls back to `DATABASE_URL` with driver prefix stripped |
| `GH_TOKEN` | `.env` | no | GitHub personal access token |
| `TEST_DATABASE_URL` | `.env` | no | PostgreSQL connection string for test database |
| `BUILD_ID` | env | no | Git SHA for static asset cache-busting (default `"dev"`) |
| `WATCHER_NOTIFIER_BASE_URL` | `/etc/watcher/notifier.env` | **yes** | Base URL of the notifier service (e.g. `http://localhost:9000`). **Not sufficient on its own since #277** — see `WATCHER_NOTIFIER_ENABLED`. Moved out of `/etc/watcher/.env` under #278: that file is exported into every agent shell, and this pair must be held by the service alone |
| `WATCHER_NOTIFIER_API_KEY` | `/etc/watcher/notifier.env` | **yes** | Watcher tenant API key issued by `scripts/seed_tenant.py` in the notifier repo, marked `production` there. This is the credential that makes a stray dispatch *deliverable*, which is why the pair is gated — and, since #278, why it lives in a file no shell sources |
| `WATCHER_NOTIFIER_ENABLED` | `deploy/watcher.service` **only** | prod only | `=1` opts this process into building a notifier client at all (`src/core/notifier_client/client.py`, #277). Without it `get_notifier_client()` raises `NotifierNotEnabled` — **and a URL held without it aborts startup**, so a unit that lost the line fails loudly instead of going quiet on notifications. Must live in the systemd unit, never an env file, for the same reason as the two flags above and with the largest blast radius of the three: a stray database row is recoverable and a stray bus frame is inert, but a stray notification is delivered to real subscribers, cannot be recalled, and *succeeds* — leaving no error behind to notice. `scripts/dev_server.sh` sets it for itself when `WATCHER_DEV_NOTIFIER_BASE_URL` names a scratch notifier. Since #278 the **flag without a URL** aborts startup too: only the unit sets it, and the credential it goes with is in the unit's own env file, so that combination means the file did not load |
| `WATCHER_ALLOW_PRODUCTION_DB` | `deploy/watcher.service` **only** | prod only | `=1` opts into serving a database whose name lacks a `_test`/`_dev` suffix (`src/core/db_safety.py`, #233). Must live in the systemd unit, never an env file — env files are sourced by hand-run dev servers, which are exactly what the guard stops |
| `WATCHER_DEV_DATABASE_URL` | `.env` | no | Persistent dev database for `scripts/dev_server.sh`; wins over `TEST_DATABASE_URL` |
| `WATCHER_BUS_REDIS_URL` | `/etc/watcher/.env` | prod | Redis URL of the Archiver-operated broker (`redis://localhost:6379/0`) for the `content.fetch-policy` and `info.watch-status` producers (#245, #264). Unset → both periodic publish tasks skip with an ERROR log: Replicator paces every host at its own conservative default, and Archiver's watched-item panel / drift detector go stale. **Not sufficient on its own since #262** — see `WATCHER_BUS_ENABLED` |
| `WATCHER_BUS_ENABLED` | `deploy/watcher.service` **only** | prod only | `=1` opts this process into building a bus client at all (`src/core/bus.py`, #262). Without it `bus_client_from_env()` returns `None`, so nothing publishes and neither consumer starts — **and a URL held without it aborts startup** with `BusNotEnabled`, so a unit that lost the line fails loudly instead of going quiet. Must live in the systemd unit, never an env file: `WATCHER_BUS_REDIS_URL` does live in one, so every process that sources `/etc/watcher/.env` inherits the production broker address, and the flag is the only thing separating the service from an agent shell or a REPL. `scripts/dev_server.sh` sets it for itself when `WATCHER_DEV_BUS_REDIS_URL` names a scratch bus |
| `WATCHER_WATCH_STATUS_REPUBLISH_CRON` | env | no | Cron expression for the `info.watch-status` full-set republish (default `*/5 * * * *`, #264). The period is the recovery bound for a dropped frame — this stream has no outbox by design; loss is corrected by the next full set. A malformed value falls back to the default with an ERROR log |
| `WATCHER_WATCH_STATUS_STREAM_MAXLEN` | env | no | Producer-enforced retention cap for `info.watch-status` (`XADD MAXLEN ~`, default `50000`). The full set republishes forever, so an untrimmed stream grows without bound; invalid/non-positive values fall back to the default with a warning — never to unbounded |
| `WATCHER_FETCH_POLICY_STREAM_MAXLEN` | env | no | Same producer-enforced retention cap for `content.fetch-policy` (default `50000`) |
| `WATCHER_FETCH_COMMAND_TIMEOUT_SECONDS` | env | no | Reaper timeout for an in-flight command with no fact (default `1800` — deliberately generous; Replicator's reclaim cadence is an operator knob, and a tight value re-issues under live retries) |
| `WATCHER_CONDITIONAL_GET_ENABLED` | env | no | Which items may replay stored conditional-GET validators (#269). Unset/falsey → off for everything and every command is byte-identical to the pre-#269 one; `true` → the whole fleet; anything else is read as a comma-separated list of WatchedItem ids, which is the canary position. Safe only while replicator#17 and #249 part 1 are both deployed — a validator sent to a Replicator that classifies 304 as a plain fetch failure marks a healthy item ERROR and notifies a user about it on every no-change check |
| `WATCHER_VALIDATOR_MAX_AGE_HOURS` | env | no | How long a stored validator pair may be replayed before one unconditional re-fetch is forced (default `168`). The residual net under the deterministic invalidation rules — an origin whose ETag tracks a template rather than the watched region would otherwise hold a fingerprint inherited indefinitely. An unparseable value falls back to the default rather than raising, and a zero or negative value disables replay entirely (logged at INFO): the knob must not be able to wedge the issue path |
| `GCS_BLOB_CREDENTIALS` | env | for `gs://` blobs | Key file for the `co-gcs-blob-reader` SA (`/etc/watcher/co-gcs-blob-reader.json`), read by the `gs://` blob arm (#275). Singular `BLOB` — easy to typo as `BLOBS`. Deliberately **not** `GOOGLE_APPLICATION_CREDENTIALS`, which is the wheelhouse identity: reading fetched content and reading the private package index are different jobs and must not share a principal. Unset or unusable (missing file, malformed key) → every `gs://` blob fails permanently (`blob_unreadable`, no re-issues) until fixed; a rotated key at the same path needs a restart |
| `WATCHER_FETCH_MAX_REISSUES` | env | no | Re-issues per fetch intent before it fails with ERROR health (default `3`). Caps the *lineage*, not one path: both the reaper's stall sweep and the blob-unreadable apply (#275) read the same `reissue_count` |
| `WATCHER_DEV_BUS_REDIS_URL` | `.env` | no | Scratch-bus opt-in for `scripts/dev_server.sh`; without it the dev server **clears** an inherited `WATCHER_BUS_REDIS_URL` (and `WATCHER_BUS_ENABLED`) so it cannot publish policy onto the production stream. With it, the script exports both, since the flag is otherwise unit-only |
| `WATCHER_DEV_NOTIFIER_BASE_URL` | `.env` | no | Scratch-notifier opt-in for `scripts/dev_server.sh`; without it the dev server **clears** `WATCHER_NOTIFIER_BASE_URL`, `WATCHER_NOTIFIER_API_KEY` and `WATCHER_NOTIFIER_ENABLED` so it cannot notify the production tenant (#277) — kept after #278 moved the pair out of the sourced env files, because it also catches a shell that exported them by hand. Requires `WATCHER_DEV_NOTIFIER_API_KEY` beside it — a URL without its key refuses to start rather than fall back to whatever key is in the environment |
| `WATCHER_DEV_NOTIFIER_API_KEY` | `.env` | no | The scratch notifier's tenant key. Required whenever `WATCHER_DEV_NOTIFIER_BASE_URL` is set; meaningless without it. Use a key notifier marks **`development`** (notifier#22), so a dev server pointed at a production notifier is refused instead of delivering |

**The dev notifier tenant (#278, provisioned 2026-08-25).** Watcher's
development credential is valid against the notifier deployment on
**`http://localhost:9001`**, a separate instance from production's `:9000` with
its own database. Two consequences, both verified rather than assumed:

* The dev key **authenticates on `:9001`** — which is itself the proof that
  deployment is not classified production, since notifier refuses a
  `development` key wherever `serving_production()` is true.
* Presented to production `:9000` it comes back **401 `Invalid API key`, not the
  403** notifier#22 describes. The 403 branch compares a key's `environment`
  against the deployment it reached, which only arises when one database holds
  both kinds. Here the two deployments have separate databases, so the dev key
  does not exist in production at all — refused by non-existence, which is the
  stronger of the two failures. Do not rely on reading a 403 to detect a
  misdirected dev server; the refusal is what matters, not its code.

The dev tenant currently has **no channels and no templates**, so a dev server
can authenticate and construct a client but has nothing to dispatch *to*:
`dispatch_event_notifications` skips a candidate with no `remote_channel_id`
and records it as a failed audit result. That is fine for the isolation #278
asked for, and not yet enough to exercise the notification path end to end —
sink channels are requested on that issue.

**Retired variables.** Removed from `/etc/watcher/.env` under #277 after each
was confirmed to have no reader in `src/`, `scripts/` or `tests/`. Historical
plan documents under `docs/plans/` still describe them and are left as written
— they are a record of what was true then, not instructions:

| Variable | Retired because |
|---|---|
| `USE_REMOTE_NOTIFY` | The local Apprise path it switched between is gone; the remote path is the only path. `docs/plans/2026-05-02-notifier-adapter.md` still names it as a rollback lever — it is not one, and flipping it does nothing |
| `ARCHIVER_BASE_URL` | Watcher makes no HTTP calls to Archiver since #254; the SDK went with them |
| `ARCHIVER_API_KEY` | Same. Removing it from this file does **not** revoke it — the key is presumably still valid on Archiver's side, where revocation is tracked as [CannObserv/archiver#186](https://github.com/CannObserv/archiver/issues/186). Until that closes, treat the credential as live |
| `APPRISE_SECRET_KEY` | The Fernet key for encrypted Apprise URLs. Verified no ciphertext column survives in the schema before deletion |

The notifier pair was **renamed** rather than retired in the same pass:
`NOTIFIER_BASE_URL` → `WATCHER_NOTIFIER_BASE_URL`, `NOTIFIER_API_KEY` →
`WATCHER_NOTIFIER_API_KEY`, bringing them under the service-prefix rule that
`WATCHER_BUS_REDIS_URL` already followed. A bare `NOTIFIER_*` name in a shell
today resolves to nothing.

**Watcher's Redis use.** Archiver operates `redis-server` and owns the broker
(archiver#109). Watcher publishes `content.fetch-policy` (#245) and — Phase 4,
#241 — publishes `content.fetch` commands and consumes `content.blobs` facts
via its own consumer group (`watcher`, started in the lifespan when
`WATCHER_BUS_REDIS_URL` is set **and** `WATCHER_BUS_ENABLED=1`, #262). Since
#254 it also consumes `info.registry` **grouplessly**, replayed from `0-0` at
boot; without a usable bus neither consumer starts and the registry cannot
converge. Since #264 it publishes
`info.watch-status` — the return leg of the registry channel: applied
generation, scheduler state, and observation freshness per InfoItem, full-set
republished on `WATCHER_WATCH_STATUS_REPUBLISH_CRON` (default every 5
minutes) including tombstones from `revoked_info_items`. **Health primitive:
last-entry age** — `redis-cli XREVRANGE info.watch-status + - COUNT 1` should
never be older than the republish period while the service is up; an aging
stream with a live service means the publish task is failing (check
Procrastinate job errors), and Archiver's panel renders drift from exactly
this staleness. All queued work stays on Procrastinate over
Postgres. See [ARCHITECTURE.md](ARCHITECTURE.md) § *Redis and the bus* for the ownership split.

