# Recovery rehearsals

An annex of [RECOVERY.md](RECOVERY.md): each rehearsal done by hand, dated,
with what it proved. The suite's own rehearsal, which every integration run
repeats, is listed there. The entries use that doc's terms: "above" and the
gates mean its *Restore* section and go/no-go gates; "the roles script" is
`scripts/setup-db-roles.sql`; "the root unit" is the backup unit before #297,
and "the #297 shape" its sandbox since, both in *The sandbox*; the create-only
probe is the one in *Install and first run*.

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
  `take_dump` of production read 1,805,866 bytes, 178 TOC entries, alembic
  `2f8bb8f7100a`, 17 tables with data — its table of contents **byte-identical**
  to a superuser `pg_dump`'s taken beside it (sha256 of the entries, `8c7318ee…`).
  A GCS preflight listed through the sandbox on the stand-in key, and the
  check-in, handed the empty key, warned and posted nothing. Discarded with the
  run's private `/tmp`.
- **Against a real object (2026-09-13)**, on the shared VM once the bucket and
  writer key existed. The first hand-started run of the installed unit shipped
  `watcher/20260913T193654Z.dump` — 1,810,347 bytes, alembic `2f8bb8f7100a`,
  `source_host` `watcher` — and, with no monitor yet, the check-in warned and
  posted nothing. The create-only probe created its object and got **403** on
  both the overwrite and the delete. `--list` showed the dump, and a
  `--download-only` fetch matched its recorded sha256 and read through; the
  copy was then deleted. The timer was enabled after that run, and its first
  firing (03:20 UTC the next night) shipped on its own. That object restored
  into a scratch `_test` database on the same cluster in 1.6 s, fetch
  included: every go/no-go gate passed, the structure matched production's
  exactly (17 tables, 4 sequences, 18 functions, 42 indexes, 7 triggers), and
  row counts differed only in `audit_log` and `fetch_commands`, append-only
  logs that had grown since the dump. The roles already existed, so the roles
  script was not re-run — it would have reset `watcher_app`'s password
  cluster-wide.
- **The dead-man monitor (2026-09-14)**, `watcher-backup` on the watcher
  tenant, alerting to its global Slack and Mailgun channels. A hand-started run
  checked in (`202`; `pending` → `ok`, the run's summary as `last_variables`).
  A labelled test `alert` rendered the templates and was delivered to both
  channels. With the window cut to 60 s, the sweep marked it `missing` 32 s
  past the deadline and alerted; the next real run's check-in recovered it, and
  the window went back to 24 h plus 2 h.
- **Across hosts, on co-watcher (2026-09-15)**: the cutover's path on a fresh
  cluster (#296 step 16), roles first as above. `--latest --prefix watcher`
  restored the old VM's nightly `watcher/20260915T032405Z.dump` (alembic
  `2f8bb8f7100a`; source cluster `C.UTF-8`, like this one) into `watcher_dev`
  in 1.8 s, fetch included. Every gate passed, every table's row count equalled
  the dump's own `COPY` rows, and the dev server served the restored items.
- **co-watcher's own nightly, and the dead-man on the live monitor
  (2026-09-16)**, the #296 step 23 soak. The timer's first unattended run
  shipped `co-watcher/20260916T032228Z.dump` at 03:22:28Z and checked in `ok`.
  It restored into a rebuilt empty `watcher_dev` in 1.9 s, fetch included:
  every gate passed and all 17 tables equalled the dump's own `COPY` rows.
  Then the window was cut to `interval_seconds` 60 / `grace_seconds` 0: the
  sweep alerted at 13:21:02Z and the state read `missing`, a hand-started run
  recovered it at 13:21:28Z with `last_variables` naming its `co-watcher/`
  object, and the window went back to 24 h plus 2 h (next deadline
  2026-09-17T15:21Z). The monitor is read and patched with the check-in key
  itself, over `X-API-Key` — `Authorization: Bearer` answers 403 and reads as
  a key problem rather than a header one.
