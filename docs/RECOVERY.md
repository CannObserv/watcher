# Recovery

The nightly database backup, the restore, and the runbook around both (#296
D8/D9). Built on CannObserv/broker#4's pattern — its `docs/RECOVERY.md` is the
sibling to this one.

## What was exposed

Until #296 the production database had **never been backed up**: no timer, no
dump directory, one copy on one disk. The VM move (#296) makes the host
disposable by design, and a disposable host holding the only copy of the data is
the case this closes.

## Design

| Property | How |
|---|---|
| **No database credential, no privilege** | The unit runs as its own dynamic user, `watcher_backup`, with no capabilities (#297). `pg_dump` and a `psql` for the schema version connect as that user over the local socket, by peer auth, to the role of the same name — `pg_read_all_data` and nothing else. Its two keys arrive as systemd credentials, never in a process environment. |
| **Verified before it ships** | `pg_restore --list` must read the archive and find the data sections of `public.alembic_version` and `public.watched_items` — a readable dump of the wrong database is refused — and `pg_restore --file=/dev/null` must then read every data block through. `--list` alone passes a truncated archive: the table of contents precedes the data. |
| **Named by its own time** | `<host>/<YYYYMMDDTHHMMSSZ>.dump` — the start of `pg_dump`, which is when it took its snapshot. A listing is a timeline. |
| **Create, never overwrite or delete** | `if_generation_match=0` in code; `objectCreator` + `objectViewer` at IAM, no `delete`. A 412 is `unchanged` only if the object's recorded sha256 matches — two dumps are never the same bytes, so anything else is a name collision and a failure. |
| **Retention is the bucket's** | Lifecycle deletes at 30 days, soft-delete left on. A compromised host cannot erase its own history. |
| **Failure is loud, silence too** | A failed run is a failed unit *and* an `alert` check-in to notifier; a good one checks in `ok`. The dead-man monitor alarms when neither arrives (D9). |
| **Metadata travels with the object** | `dumped_at`, `sha256`, `size_bytes`, `alembic_head`, `server_version`, `pg_dump_version`, `toc_entries`, `source_host` — what a restore checks without trusting the file. |

**RPO is 24 hours** — one dump a night. The dump is small — 27.6 MB before the
#296 D7 prune, 1.7 MB after it (both measured) — so going hourly is a one-line
timer change if that ever needs tightening.

## The sandbox, and why it is this shape

`deploy/watcher-backup.service` runs code from the checkout, nightly and
unattended, so what that code can reach is the whole question (#297). Until
#297 the answer was root: `User=root` to read a `0400 root:root` key, holding
`CAP_DAC_READ_SEARCH` (every file on the host, `notifier.env` included) and
`CAP_SETUID`/`CAP_SETGID` (to become `postgres`, the cluster superuser) — so
anything that could write the tree had its code run as uid 0. Now:

| | How |
|---|---|
| **Its own user** | `DynamicUser=yes`, `User=watcher_backup`: systemd allocates the uid when the run starts and releases it when it ends. Nothing to provision on a new host, no account to log in to, and while no run is active no process can be `watcher_backup` at all. |
| **No capabilities** | `CapabilityBoundingSet=` is empty and nothing is ambient. Observed in the unit: `CapPrm`/`CapEff`/`CapBnd`/`CapAmb` all zero, `NoNewPrivs` 1, seccomp on — in the job and in a child it spawns. |
| **A read-only database role** | `scripts/setup-backup-role.sql` creates `watcher_backup`: `LOGIN`, `INHERIT`, a member of `pg_read_all_data` (SELECT on every table and sequence, USAGE on every schema), `PASSWORD NULL`. pg_hba's `local all all peer` admits it from the OS user of the same name, and no password rule ever can. The job no longer reaches superuser at all. |
| **Keys as credentials** | `LoadCredential=gcs:/etc/watcher/co-watcher-backup.json` and `notifier-key:/etc/watcher/backup-notifier.key`: systemd reads each root-only file and hands the run a private copy under `$CREDENTIALS_DIRECTORY` (`/run/credentials/watcher-backup.service/`, `0440 root` with an ACL for the run's uid). The GCS SDK gets the copy's path, `GOOGLE_APPLICATION_CREDENTIALS=%d/gcs`; the check-in reads its file. Neither key is in any process environment, so neither is in a child's or in `/proc/<pid>/environ`. |
| **An empty home** | `ProtectHome=tmpfs` with `BindReadOnlyPaths=/home/exedev/watcher`: `/home` holds the checkout and nothing else — no `~/.ssh`, no other checkout — and the checkout's `.env` (`0640 exedev`) is unreadable to the run's uid. This is what replaced `CAP_DAC_READ_SEARCH`: the venv needed traversing a `0750` home, and now the home is not there. |

Plus what was already there: `ProtectSystem=strict`, `PrivateTmp`,
`NoNewPrivileges`, the kernel/namespace/personality protections, and
`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`. `tests/deploy/test_backup_units.py`
pins every line of it. `systemd-analyze security --offline` scores the unit 3.9
(OK), from 5.1 (MEDIUM) as root.

**Two systemd 255 facts decide the key files.** A `LoadCredential=` whose source
file is missing fails the start, `243/CREDENTIALS`, before any of the job runs;
and an empty `SetCredential=` — the documented fallback — is ignored, so it
cannot make one optional. Both files therefore **must exist**, and until the
dead-man monitor does, `backup-notifier.key` is an **empty** file: the job reads
an empty key as unset, and warns each night that a stopped backup will not be
noticed. A missing GCS key is the same `243`: a failed unit and no check-in at
all, so the monitor's silence alarm is what reports it.

**The interpreter must be outside `/home`**, or inside the bound checkout. The
venv's `python` links to `/usr/bin/python3.12` on these hosts; a uv-managed one
under `~/.local/share/uv` would be hidden by the empty home, and the unit would
fail to exec (`203/EXEC`). The unit test resolves the link on any host that has
the venv and fails if so.

**What a `pg_read_all_data` dump cannot read fails it, loudly.** A table under
row security (the role has no `BYPASSRLS`) and a large object (the predefined
role covers tables, views and sequences) are each a `pg_dump` error, never a
quietly thinner dump; only a logical-replication subscription is a warning, and
it is not data. Production has none of the three (checked 2026-09-12), and the
role script's report counts the first two, beside any relation the role cannot
read, every time it runs.

**Superseded: the capability trap.** The root unit needed `CAP_SETUID` for
`setpriv`, and under systemd 255 any seccomp-backed protection beside
`NoNewPrivileges=yes` stripped it from the effective set before the exec
(`CapEff` `0xc4` → `0x44`), so it had to be ambient as well. With no privilege
drop left, there is nothing for the sandbox to strip. The restore still drops
to `postgres` with `setpriv --reset-env`, but it is run by hand, outside any
unit.

## Provisioning — needs the GCP project owner

The node has no `gcloud` and no credential that can create any of this. Mirrors
broker#4 with `broker` → `watcher`:

```bash
PROJECT=co-gcs
BUCKET=co-gcs-watcher-backup
SA=co-watcher-backup

gcloud storage buckets create "gs://$BUCKET" --project="$PROJECT" --location=<same as co-gcs-blobs> \
    --uniform-bucket-level-access --public-access-prevention
printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":30}}]}\n' > /tmp/lifecycle.json
gcloud storage buckets update "gs://$BUCKET" --lifecycle-file=/tmp/lifecycle.json

gcloud iam service-accounts create "$SA" --project="$PROJECT" --display-name="co-watcher DB backup writer"
for role in roles/storage.objectCreator roles/storage.objectViewer; do
    gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
        --member="serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com" --role="$role"
done
gcloud iam service-accounts keys create co-watcher-backup.json \
    --iam-account="$SA@$PROJECT.iam.gserviceaccount.com"
```

On the host — three files. The two keys are `0400 root:root` and read only by
systemd, which hands the run its own copies; `backup.env` holds no secret. The
check-in key file is created **empty** now, because the unit will not start
without it (see *The sandbox* above), and filled in when the monitor exists.

```bash
sudo install -m 0400 -o root -g root co-watcher-backup.json /etc/watcher/co-watcher-backup.json
sudo install -m 0400 -o root -g root /dev/null /etc/watcher/backup-notifier.key
sudo install -m 0644 -o root -g root /dev/null /etc/watcher/backup.env
sudo tee /etc/watcher/backup.env >/dev/null <<'EOF'
WATCHER_BACKUP_BUCKET=co-gcs-watcher-backup
EOF
```

**No `GOOGLE_APPLICATION_CREDENTIALS` in `backup.env`** — broker#4's host
steps put one there; this unit sets its own. It points at the run's private copy
(`%d/gcs`), and an env file's value would win, aiming the job at the root-only
original. Left to the SDK that fails `Permission denied` on the key, which reads
as a reason to loosen its mode — so the job refuses first: exit 2 and an `alert`
naming this file, before any client is built.

**The dead-man monitor** is notifier's (notifier#56): a monitor on the watcher
tenant, `interval_seconds` 86400 plus a grace for the timer's jitter and a slow
run, and a check-in key. Then the key into its file — `tee`, so the existing
`0400 root:root` file keeps its mode — and the other two into `backup.env`:

```bash
printf '%s' '<production-marked key>' | sudo tee /etc/watcher/backup-notifier.key >/dev/null
sudo tee -a /etc/watcher/backup.env >/dev/null <<'EOF'
WATCHER_BACKUP_NOTIFIER_BASE_URL=http://notifier:9000
WATCHER_BACKUP_MONITOR_ID=<monitor id>
EOF
```

**What a check-in carries**, for the monitor's alert template: an `alert` sends
`source_host`, `outcome` (`failed`) and `error`; an `ok` sends the run's
summary — `outcome` (`uploaded` or `unchanged`), `object`, `dumped_at`,
`size_bytes`, `sha256`, `alembic_head`, `source_host`.

All three or none — the base URL, the monitor id, and a non-empty key: half a
configuration is logged as an ERROR naming what is missing, and checks in
nothing. The key is never read from the environment; the
`WATCHER_BACKUP_NOTIFIER_API_KEY` it once was is ignored.

Two traps, both from broker#3. **A monitor left `enabled: false` still
delivers `alert` check-ins but alarms on nothing when they stop** — findings
reach a person, silence does not, and it looks completely wired. And a
`tenant_id` in place of the monitor id is a 404: both are ULIDs, both appear in
the monitor's JSON.

**The base URL is configuration — a departure from broker#3**, which made it a
constant so a port typo could not aim a dead-man's switch at notifier_dev
(`:9001`, whose `/health` is byte-identical). This repo's rule is that a
notifier URL in `src/` is configuration, a literal being the defect whatever
host it names (`tests/test_notifier_isolation.py`, #280). The typo is not silent
anyway: keys live per database, so a production-marked key on `:9001` is a
**401** (notifier#56) — nothing lands, and the monitor alarms. Inverting it
takes the wrong port *and* a development-marked key. Use the production key.

## Install and first run

**The role first**, once per cluster: a role is cluster state, which no dump
carries, so a host restored from a dump needs it too. Its report must read `t`
for `login`, `inherit`, `no_password`, `reads_all_data` and `can_connect`, `f`
for every other column, and three zeros.

```bash
sudo -u postgres psql -d watcher < scripts/setup-backup-role.sql
sudo cp deploy/watcher-backup.service deploy/watcher-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start watcher-backup.service        # one run, by hand
sudo journalctl -u watcher-backup.service -n 20    # "Backup uploaded: gs://…"
sudo systemctl enable --now watcher-backup.timer   # only once the run above succeeded
```

A start that fails `243/CREDENTIALS` is a key file missing — both must exist,
the check-in key empty if need be; `203/EXEC` is an interpreter the empty
`/home` hides; a `FATAL: role "watcher_backup" does not exist` in the journal
is the role script not yet run on this cluster.

Two runs never share a name — it is the second the dump began — so `unchanged`
appears only for a retried upload of the same file. **Prove the grant is
create-only by observation**, on an object of the probe's own: create it, then
try to overwrite it and to delete it. Under `objectCreator` + `objectViewer`
both must answer **403**; a success means the identity holds
`storage.objects.delete` and could erase its own history. Only the probe object
is ever at risk. (A create-only upload over an existing name proves nothing
here: GCS answers `if_generation_match=0` with 412 whatever the grant, which is
what broker#4 observed and what the job's own collision path relies on.)

By hand as root, the one identity that can read the key file itself:

```bash
sudo bash -c "GOOGLE_APPLICATION_CREDENTIALS=/etc/watcher/co-watcher-backup.json .venv/bin/python -" <<'PY'
from datetime import UTC, datetime
from google.api_core.exceptions import Forbidden
from google.cloud import storage
bucket = storage.Client().bucket("co-gcs-watcher-backup")
blob = bucket.blob(f"probe/{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
blob.upload_from_string(b"probe", if_generation_match=0)  # the create: must succeed
for attempt, act in (("overwrite", lambda: blob.upload_from_string(b"again")),
                     ("delete", blob.delete)):
    try:
        act()
        print(f"{attempt}: ALLOWED — the grant is too wide")
    except Forbidden:
        print(f"{attempt}: 403 — create-only holds")
PY
```

The probe object has no `.dump` suffix, so no listing shows it, and the
lifecycle rule removes it with everything else.

## Restore

**Name the source host.** A restore runs on a different host from the one that
shipped the dump — co-watcher at the cutover, a replacement in an incident — and
dumps live under the shipping host's name (`watcher/…` from the shared VM). So
`--latest` requires `--prefix HOST` and never defaults to the restoring host:
once that host's own timer has run, its newest dump is a real, verifiable dump
of the wrong database.

The restore is run by hand, as root — it reads the key file itself, where the
backup unit is handed a copy — so it names the key explicitly: `backup.env`
does not.

```bash
cd /home/exedev/watcher
ENV='set -a; . /etc/watcher/backup.env; set +a; export GOOGLE_APPLICATION_CREDENTIALS=/etc/watcher/co-watcher-backup.json'
SRC=watcher                                   # the host that shipped the dump

# What is there — every host's dumps, or one host's with --prefix
sudo bash -c "$ENV; .venv/bin/python -m src.ops.restore --list"

# Fetch and verify only — sha256 against the recorded digest, then pg_restore --list.
# The whole database, as root: a 0600 file in a 0700 directory, and a directory
# that exists but is not root's own and private is refused. Never under /tmp.
sudo bash -c "$ENV; .venv/bin/python -m src.ops.restore --latest --prefix $SRC --download-only /root/watcher-restore"
```

Into a database — **an existing, empty one**; the restore is one transaction, so
a failure leaves it empty rather than half-loaded.

**The roles come first.** The dump names `watcher` (the owner, and the
migration role) and grants to `watcher_app`, but carries neither role — so on a
fresh cluster, which co-watcher and every replacement host is, `createdb -O
watcher` fails and the restore aborts at its first grant. And both passwords are
the ones `/etc/watcher/.env` **already holds**: `setup-db-roles.sql` sets
`watcher_app`'s to whatever it is handed, so a new one breaks `DATABASE_URL`.

```bash
# The passwords /etc/watcher/.env already carries — never new ones.
MIGRATE_PW='<the password in WATCHER_MIGRATION_DATABASE_URL>'
APP_PW='<the password in DATABASE_URL>'

# 1. Fresh cluster only: the owner role (an existing cluster has it). \getenv,
#    so the password is never in argv.
sudo -u postgres WATCHER_MIGRATE_PASSWORD="$MIGRATE_PW" psql -v ON_ERROR_STOP=1 <<'SQL'
\getenv pw WATCHER_MIGRATE_PASSWORD
CREATE ROLE watcher LOGIN PASSWORD :'pw';
SQL

# 2. The database, owned by it.
sudo -u postgres createdb -O watcher watcher          # or a *_dev name to rehearse

# 3. watcher_app and its CONNECT, before the restore whose grants name it. Safe
#    on an empty database. Redirected, not -f (postgres cannot read /home/exedev).
sudo -u postgres WATCHER_APP_PASSWORD="$APP_PW" psql -d watcher < scripts/setup-db-roles.sql

# 4. The restore.
sudo bash -c "$ENV; .venv/bin/python -m src.ops.restore --latest --prefix $SRC --into watcher --run-as postgres"

# 5. The roles script again — idempotent; it re-asserts every grant against the
#    restored tables, which is what the gates below then check.
sudo -u postgres WATCHER_APP_PASSWORD="$APP_PW" psql -d watcher < scripts/setup-db-roles.sql

# 6. The backup's role, so this host's own backups can run (Install and first run).
sudo -u postgres psql -d watcher < scripts/setup-backup-role.sql
```

The dump carries the table grants and default privileges; what it cannot carry
is the database-level `GRANT CONNECT` (`pg_dump` without `--create` has nowhere
to put it) — step 3 supplies that.

`--object <key>` restores a specific dump instead of the newest.

**A name is the writer's claim; `created` is the bucket's.** An honest dump is
named when `pg_dump` starts and created when the upload lands, so `--latest`
passes over — and `--list` marks `SUSPECT` — any dump named more than ten
minutes after the bucket created it: a skewed clock, or a compromised writer
planting a future-dated name to own `--latest` until the lifecycle rule takes it.

### Go / no-go gates

Before anything connects to a restored database:

```sql
-- the schema version matches the object's alembic_head metadata
SELECT version_num FROM alembic_version;
-- the two-role model survived (#259): DML yes, DDL no, alembic_version read-only
SELECT has_table_privilege('watcher_app', 'watched_items', 'INSERT')   AS dml,      -- t
       has_schema_privilege('watcher_app', 'public', 'CREATE')         AS ddl,      -- f
       has_table_privilege('watcher_app', 'alembic_version', 'INSERT') AS head_rw,  -- f
       has_database_privilege('watcher_app', current_database(), 'CONNECT') AS conn; -- t
-- future tables stay covered
SELECT count(*) FROM pg_default_acl WHERE array_to_string(defaclacl, ',') LIKE '%watcher_app=%';  -- > 0
```

When the source is still alive and **quiesced** (the #296 cutover), also compare
row counts on every table outside `procrastinate_*` between source and target;
on a live source a nightly dump's counts drift by design.

## Rehearsals

- **Every integration run**: `tests/ops/test_backup_restore_rehearsal.py` seeds a
  scratch database with the two-role grants, ships it through the real
  `pg_dump` and the SDK-faithful bucket fake, restores it with the real
  `pg_restore`, and asserts the gates above; a second test proves a failed
  restore leaves the target untouched.
- **Inside the real sandbox, by hand (2026-09-11)**: `take_dump` against
  production on the shared VM under the root unit's exact confinement —
  27.6 MB, 178 TOC entries, alembic `2f8bb8f7100a`, 17 tables with data;
  discarded.
- **The #297 shape, first in a throwaway unit (2026-09-12)**: on systemd 255 a
  missing `LoadCredential=` source failed `243/CREDENTIALS`, an empty
  `SetCredential=` fallback was ignored, and an empty key file loaded as an
  empty credential — which is why both key files must exist.
- **Then inside the installed unit (2026-09-13)**, a runtime drop-in replacing
  only `ExecStart`, the env file and the two key sources (stand-ins for the
  unprovisioned ones; every identity and sandbox line the unit's own):
  `watcher_backup`, `CapPrm`/`CapEff`/`CapBnd`/`CapAmb` 0 in the job and a
  child; no key in the environment, `GOOGLE_APPLICATION_CREDENTIALS` the
  credential copy's path; `/home` holding only the checkout, and `.env`,
  `/etc/watcher/.env`, `notifier.env` and the service's GCS key all
  unreadable. `psql` connected as `watcher_backup`, not a superuser, and
  `take_dump` of production read 1.8 MB, 178 TOC entries, alembic
  `2f8bb8f7100a`, 17 tables with data — its table of contents **byte-identical**
  to a superuser `pg_dump`'s taken beside it (sha256 of the entries, `8c7318ee…`).
  A GCS preflight listed through the sandbox on the stand-in key, and the
  check-in, handed the empty key, warned and posted nothing. Discarded with the
  run's private `/tmp`.
- **Against a real object**: pending the provisioning above — the first run
  and a restore of its object, recorded here when done.
