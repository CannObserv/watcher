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
| **No database credential** | The unit runs as root (to read its 0400 key) under a sandbox; `pg_dump` and a `psql` for the schema version drop to the `postgres` OS user with `setpriv` and connect over the local socket by peer auth. `setpriv`, not `runuser` — runuser goes through PAM, which cannot open a session under `ProtectSystem=strict`. |
| **Verified before it ships** | `pg_restore --list` must read the archive and find the data sections of `public.alembic_version` and `public.watched_items`. A readable dump of the wrong database is refused. |
| **Named by its own time** | `<host>/<YYYYMMDDTHHMMSSZ>.dump` — the start of `pg_dump`, which is when it took its snapshot. A listing is a timeline. |
| **Create, never overwrite or delete** | `if_generation_match=0` in code; `objectCreator` + `objectViewer` at IAM, no `delete`. A 412 is `unchanged` only if the object's recorded sha256 matches — two dumps are never the same bytes, so anything else is a name collision and a failure. |
| **Retention is the bucket's** | Lifecycle deletes at 30 days, soft-delete left on. A compromised host cannot erase its own history. |
| **Failure is loud, silence too** | A failed run is a failed unit *and* an `alert` check-in to notifier; a good one checks in `ok`. The dead-man monitor alarms when neither arrives (D9). |
| **Metadata travels with the object** | `dumped_at`, `sha256`, `size_bytes`, `alembic_head`, `server_version`, `pg_dump_version`, `toc_entries`, `source_host` — what a restore checks without trusting the file. |

**RPO is 24 hours** — one dump a night. The dump is small (27.6 MB measured
before the #296 D7 prune, which removes 95 % of the rows), so going hourly is a
one-line timer change if that ever needs tightening.

## The sandbox, and the one capability trap

`deploy/watcher-backup.service` is `ProtectSystem=strict`, `ProtectHome=read-only`,
`PrivateTmp`, `NoNewPrivileges`, the kernel/namespace/personality protections, and
`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`. Its bounding set is exactly
`CAP_DAC_READ_SEARCH` (the venv under the 0750 home) plus `CAP_SETUID` and
`CAP_SETGID` (for `setpriv`).

**Those two are also `AmbientCapabilities`, and must stay so.** Under systemd
255, any seccomp-backed protection beside `NoNewPrivileges=yes` strips
`CAP_SETUID` from the effective set before the exec (`CapEff` `0xc4` → `0x44`),
and `setpriv` then fails `setresuid` with EPERM. Bisected on the VM; ambient
keeps it through the exec, and the `postgres` child holds no capabilities at all
(`CapPrm`/`CapEff`/`CapAmb` all zero). `tests/deploy/test_backup_units.py` pins
the shape.

**Nor any of the unit's environment.** `setpriv --reset-env` hands the child
only its passwd entry's `HOME`/`SHELL`/`USER`/`LOGNAME` and a default `PATH`.
Without it the check-in key rode into `pg_dump`'s environment, readable by any
`postgres`-uid process through `/proc/<pid>/environ` — and a key that forges
`ok` is the one thing the dead-man switch cannot survive. Checked in this
sandbox (2026-09-12): a marker variable reached the child without the flag, not
with it, and `psql` still connected by peer auth.

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

On the host:

```bash
sudo install -m 0400 -o root -g root co-watcher-backup.json /etc/watcher/co-watcher-backup.json
sudo install -m 0644 -o root -g root /dev/null /etc/watcher/backup.env
sudo tee /etc/watcher/backup.env >/dev/null <<'EOF'
WATCHER_BACKUP_BUCKET=co-gcs-watcher-backup
GOOGLE_APPLICATION_CREDENTIALS=/etc/watcher/co-watcher-backup.json
EOF
```

**The dead-man monitor** is notifier's (notifier#56): a monitor on the watcher
tenant, `interval_seconds` 86400 plus a grace for the timer's jitter and a slow
run, and a check-in key. Then:

```bash
sudo install -m 0400 -o root -g root /dev/null /etc/watcher/backup-notifier.env
sudo tee /etc/watcher/backup-notifier.env >/dev/null <<'EOF'
WATCHER_BACKUP_NOTIFIER_BASE_URL=http://notifier:9000
WATCHER_BACKUP_MONITOR_ID=<monitor id>
WATCHER_BACKUP_NOTIFIER_API_KEY=<production-marked key>
EOF
```

All three or none: half a configuration is logged as an ERROR and checks in
nothing. Two traps, both from broker#3. **A monitor left `enabled: false` still
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

```bash
sudo cp deploy/watcher-backup.service deploy/watcher-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start watcher-backup.service        # one run, by hand
sudo journalctl -u watcher-backup.service -n 20    # "Backup uploaded: gs://…"
sudo systemctl enable --now watcher-backup.timer   # only once the run above succeeded
```

Two runs never share a name — it is the second the dump began — so `unchanged`
appears only for a retried upload of the same file. **Prove the grant is
create-only by observation**, on an object of the probe's own: create it, then
try to overwrite it and to delete it. Under `objectCreator` + `objectViewer`
both must answer **403**; a success means the identity holds
`storage.objects.delete` and could erase its own history. Only the probe object
is ever at risk. (A create-only upload over an existing name proves nothing
here: GCS answers `if_generation_match=0` with 412 whatever the grant, which is
what broker#4 observed and what the job's own collision path relies on.)

```bash
sudo bash -c "set -a; . /etc/watcher/backup.env; set +a; .venv/bin/python -" <<'PY'
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

```bash
cd /home/exedev/watcher
ENV='set -a; . /etc/watcher/backup.env; set +a'
SRC=watcher                                   # the host that shipped the dump

# What is there — every host's dumps, or one host's with --prefix
sudo bash -c "$ENV; .venv/bin/python -m src.ops.restore --list"

# Fetch and verify only — sha256 against the recorded digest, then pg_restore --list.
# The whole database, as root: a 0600 file in a 0700 directory, and a directory
# that exists but is not root's own and private is refused. Never under /tmp.
sudo bash -c "$ENV; .venv/bin/python -m src.ops.restore --latest --prefix $SRC --download-only /root/watcher-restore"
```

Into a database — **an existing, empty one**; the restore is one transaction, so
a failure leaves it empty rather than half-loaded:

```bash
sudo -u postgres createdb -O watcher watcher          # or a *_dev name to rehearse
sudo bash -c "$ENV; .venv/bin/python -m src.ops.restore --latest --prefix $SRC --into watcher --run-as postgres"

# The dump carries the table grants and default privileges, but not the
# database-level GRANT CONNECT (pg_dump without --create has nowhere to put it).
# Re-run the roles script, as MIGRATIONS.md does — redirected, not -f:
sudo -u postgres WATCHER_APP_PASSWORD="$APP_PW" \
  psql -d watcher < scripts/setup-db-roles.sql
```

`--object <key>` restores a specific dump instead of the newest.

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
  production on the shared VM under the unit's exact confinement — 27.6 MB, 178
  TOC entries, alembic `2f8bb8f7100a`, 17 tables with data; discarded.
- **Against a real object**: pending the provisioning above — the first run
  and a restore of its object, recorded here when done.
