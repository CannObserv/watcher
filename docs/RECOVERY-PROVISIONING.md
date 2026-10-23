# Recovery — provisioning

The one-time setup behind the nightly backup (#296 D8/D9): what the GCP project
owner creates, the host's three files, and the co-status dead-man monitor. The
backup's design, its sandbox, install and restore: [RECOVERY.md](RECOVERY.md).

## GCP resources

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

## The host's files

On the host — three files. The two keys are `0400 root:root` and read only by
systemd, which hands the run its own copies; `backup.env` holds no secret. The
check-in key file is created **empty** now, because the unit will not start
without it (see [*The sandbox*](RECOVERY.md#the-sandbox-and-why-it-is-this-shape)), and filled in when the monitor exists.

```bash
sudo install -m 0400 -o root -g root co-watcher-backup.json /etc/watcher/co-watcher-backup.json
sudo install -m 0400 -o root -g root /dev/null /etc/watcher/backup-checkin.key
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

## The dead-man monitor

**The dead-man monitor is co-status's** (#330, CannObserv/status#2):
`co-watcher-backup` in tenant `co-watcher`, at `http://status:9000`. It began
on notifier (notifier#56) on the watcher tenant and was imported with its id,
fields and two channels; co-status keeps notifier's check-in contract exactly —
path, body, 202 — so the move was configuration only: the base URL in
`backup.env` and the key in `backup-checkin.key`. Its check-in key is a
co-status key for that tenant, minted on the co-status host; the notifier key
it replaced (a second production key on notifier's watcher tenant, notifier#62)
retires with the handover. A monitor for a new host is created on co-status,
not here. The monitor:

| Field | Value | Why |
|---|---|---|
| `interval_seconds` | `86400` | one run a night |
| `grace_seconds` | `7200` | the timer's 10-minute jitter, plus the unit's one-hour timeout — a run killed by it cannot check in |
| `renotify_seconds` | `86400` | spaces repeat reports, missing or `alert`; every failed night still reports, and the next `ok` sends *"has cleared"* (status#28) |
| `channel_ids` | the watcher tenant's default Mailgun and Slack channels | |
| `title_template` / `body_template` | `watcher backup {{ outcome }} on {{ source_host }}` / `{{ error }}` | rendered for an `alert` only |
| `enabled` | `true`, stated | the first trap below |

Then the key into its file — read at a prompt, so it reaches neither shell
history nor argv, and through `tee`, so the file keeps its `0400 root:root` —
and the other two into `backup.env`:

```bash
read -rsp 'check-in key: ' KEY; echo
printf '%s' "$KEY" | sudo tee /etc/watcher/backup-checkin.key >/dev/null; unset KEY
sudo tee -a /etc/watcher/backup.env >/dev/null <<'EOF'
WATCHER_BACKUP_CHECKIN_BASE_URL=http://status:9000
WATCHER_BACKUP_MONITOR_ID=<monitor id>
EOF
sudo systemctl start watcher-backup.service
```

A check-in that lands shows in the journal only as httpx's own line —
`POST …/monitors/<id>/checkin "HTTP/1.1 202 Accepted"` — so the proof is on
the monitor's side: a fresh last check-in with the run's summary as its
variables. Then **see the alarm fire** before relying on it — on notifier that
was a `PATCH` to `interval_seconds` 60 and `grace_seconds` 0, waiting for *"has
stopped reporting"*, checking in for *"has recovered"*, and `PATCH`ing it back;
on co-status, arrange it on status#2.

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
service URL in `src/` is configuration, a literal being the defect whatever
host it names (`tests/test_notifier_isolation.py`, #280). The typo is not silent
anyway. On notifier, keys lived per database, so a production-marked key on
`:9001` was a **401** (notifier#56). On co-status (#330), `status:9001` does
not answer from co-watcher at all (checked 2026-09-28): the check-in times out.
Either way nothing lands, and the monitor alarms.
