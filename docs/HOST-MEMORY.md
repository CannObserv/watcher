# Host memory posture

How this host keeps its production service alive with no swap, sharing memory
with agent sessions the OOM killer cannot pick (#307, #309). Part of the
install, not tuning done later: [DEPLOYMENT.md](DEPLOYMENT.md) → *Installation*
points here.

**Measured 2026-09-23 on `co-watcher`: 7.75 GiB total (resized from 3.8 GiB,
#309), no swap, one active production unit.** `preflight.sh --check` no longer
calls the host small, but still warns on the missing swap — and it checks the
effective slice reservation below. This host is in the "shared" case rather than
the "measured, no action" one:

```bash
bash skills-vendor/gregoryfoster-skills/skills/init-socraticode/scripts/preflight.sh --check
```

The hazard is not that something is large — it is who the kernel picks when
something is. exe.dev session processes inherit `oom_score_adj` **-1000** from
`exe-init` and `sshd`, so the OOM killer can never choose the agent session that
is spiking and takes the host's production service instead. On
CannObserv/broker's VM on 2026-09-16 nothing that spiked was killed: the kernel
failed atomic allocations in `tailscaled` and `ksoftirqd`, the bus was down 57m
48s, and a downstream consumer never reconnected. The steps below answer that,
and none substitutes for another.

**1. Don't install a server at any launch — pin both.** Two things launch
SocratiCode here, each pinned on its own, both at **1.14.0**:

- **The driver** — `mcp-driver.mjs`, which `.claude/hooks/socraticode-health.sh`
  runs from SessionStart once per UTC day, and `index`, `status`, `verify` —
  prefers a pre-install at `~/.socraticode/pin`.
- **The session** runs the plugin's own launch, `npx -y --prefer-online
  ${SOCRATICODE_SPEC:-socraticode@latest}`, from
  `~/.claude/plugins/cache/socraticode/socraticode/<ver>/.claude-plugin/mcp.json`
  (the manifest `plugin.json` names; the two at the plugin root still hardcode
  `@latest` and are not live). The variable must be in Claude Code's
  environment **at exec**: `claudeCode.environmentVariables` in
  `~/.vscode-server/data/Machine/settings.json` sets
  `SOCRATICODE_SPEC=socraticode@1.14.0` (#322). `.claude/settings.json`'s `env`
  block carries the same value; alone, it did not pin the session here — below.

`--prefer-online` revalidates against the registry on *every* launch, so a
floating spec installs on any day the package moved. An exact one resolves from
the npx cache without installing, as long as the cache survives — which is why
`scripts/cleanup.sh` stopped wiping it weekly (#314). Cold, the session's launch
does not fit Claude Code's 30s MCP connect timeout, and the session comes up with
no tools while the health hook and `preflight.sh --check` both pass: they reach
the pin, the session reaches npx. Measured on broker: 75 MB pinned, 129 MB
warm-npx, **1.2 G** for a cold install, with all 126 `MemoryHigh` throttle events
in the install and none in indexing.

The plugin build decides whether the variable does anything. It shipped after
the 1.14.0 release with no version bump, and until 2026-09-24 this host ran
plugin 1.6.1, whose hardcoded `npx -y socraticode` could not be pinned at all.
`preflight.sh --check` says when the installed plugin never reads it; `claude
plugin marketplace update socraticode`, then `claude plugin update
socraticode@socraticode`, fixes it.

**The settings `env` block is not enough here.** Measured 2026-09-24 on the VS
Code extension's 2.1.280, after a window reload: the server's environment held
`SOCRATICODE_SPEC=socraticode@1.14.0`, its argv `npm exec socraticode@latest` —
the plugin's args were expanded without the block, whose merged environment
still reached the child. Present at exec (inherited, or `--settings`), the same
variable launched 1.14.0 on 2.1.280 and 2.1.281; with the machine setting, a
fully reconnected session (extension 2.1.281 by then) launched `npm exec
socraticode@1.14.0`. On co-replicator's 2.1.280 the block alone did pin the
session (gregoryfoster/skills#327); why the hosts differ is unknown
(gregoryfoster/skills#332), and whether the block alone works on 2.1.281 is
unmeasured — so keep the machine setting. It is machine-scoped, so it cannot be
a committed workspace setting; a terminal `claude` needs the variable exported
in its shell.

Re-pin as a decision, never on a schedule, and do all four steps —
`tests/deploy/test_socraticode_config.py` fails when the pre-install, the
settings block and the machine setting disagree:

```bash
npm view socraticode version        # pick a literal; never @latest
# 1. The driver's pre-install:
systemd-run --user --scope -p MemoryHigh=1200M -p MemoryMax=1536M \
  choom -n 500 -- npm install --prefix ~/.socraticode/pin socraticode@<version>
# 2. The session's npx entry, under the same cap. npx keys its cache directory
#    on the spec string, so this is the entry the session's launch reuses;
#    skip it and the first session after the re-pin installs uncapped:
systemd-run --user --scope -p MemoryHigh=1200M -p MemoryMax=1536M \
  choom -n 500 -- npm exec --yes --prefer-online --package=socraticode@<version> -- true
# 3. SOCRATICODE_SPEC in .claude/settings.json: socraticode@<version>
# 4. The same value in ~/.vscode-server/data/Machine/settings.json, then
#    restart the session (verified here by closing the window and reconnecting)

# Says which path the driver takes, without launching a server:
node skills-vendor/gregoryfoster-skills/skills/init-socraticode/scripts/mcp-driver.mjs resolve
```

**Verify the process, never a report.** Only the launched argv is evidence:
`ps -eo args | grep 'npm exec socraticode'` must read `npm exec
socraticode@1.14.0`. `preflight.sh --check`, the daily health hook and `claude
mcp list` run from a session's shell all take the version from
`SOCRATICODE_SPEC` in their own environment — which the settings block puts
there whether or not the launch saw it. Observed 2026-09-24: preflight printed
*Plugin session launches socraticode 1.14.0 … no launch installs* while the
session's server was plugin 1.6.1's unpinned `npm exec socraticode`, and the
driver reports whatever version the environment names. Its hint still
prescribes the settings block alone (gregoryfoster/skills#332). Run bare,
`claude mcp list` treats this folder as untrusted and reports `@latest` either
way.

**2. The service takes a reservation, never a cap.** `deploy/watcher.service`
carries `MemoryLow=512M` and `OOMScoreAdjust=-500`
(`tests/deploy/test_installed_unit_matches_repo.py` pins all three facts,
including the *absence* of a cap). `MemoryHigh=` on a production unit throttles
reclaim rather than failing an allocation, so the unit slows to a crawl while
still reporting `active` — worse for a dashboard than an honest failure. The cap
belongs on the install that spikes, which is where step 1 put it.
`OOMScoreAdjust` is deliberately not -1000: an unkillable service on a host with
no swap wedges the box instead of shedding one process. **The reservation holds
only because `system.slice` grants it** — see step 4; for #307's whole life it
protected nothing.

**3. The kernel needs a reserve; earlyoom cannot act for it here.**
`vm.min_free_kbytes` shipped at ~8 MB here, which is what lets an atomic
allocation in `tailscaled` fail while memory is nominally available;
`/etc/sysctl.d/60-watcher-memory.conf` raises it to 64 MB. It does not live in
this repo, so **a rebuilt VM loses it silently** — nothing fails, the box is
simply back to an 8 MB reserve. Recreate it as part of the install:

```bash
# The shipped default here was ~8 MB, which is what lets an ATOMIC allocation
# fail in an unrelated process while memory is nominally available. 64 MB is
# ~0.8% of this host (7.75 GiB; ~1.6% before #309's resize).
sudo tee /etc/sysctl.d/60-watcher-memory.conf >/dev/null <<'CONF'
# Kernel free-memory reserve for co-watcher (watcher#307). See
# docs/HOST-MEMORY.md.
vm.min_free_kbytes = 65536
CONF
sudo sysctl --system
cat /proc/sys/vm/min_free_kbytes    # 65536
```

**earlyoom is declined here (#323).** #307 installed it to shed the `npm`/`node`
process that was actually spiking. It never could: everything a session
launches inherits the session's -1000, and earlyoom 1.7 skips a -1000 process
exactly as the kernel does, `--prefer` or not (`kill.c`, after the bonus is
added). What it could reach is the kernel's own list — measured 2026-09-24,
with 1.7 GiB of RSS at -1000 and 634 MiB eligible:

| `oom_score` | process (adj) |
|---|---|
| 734, 733 | `systemd --user`, `(sd-pam)` (+100) |
| 666–671 | postgres's backends and auxiliaries; logind, timesyncd, cron (0) |
| 503 | journald (-250) |
| 404 | `tailscaled` (-400) |
| 348, 336 | watcher's `uvicorn`, `uv` (-500) |

#307's `--avoid` moved postgres, `tailscaled` and watcher down that list, never
off it. earlyoom starts at 10% available (~790 MiB here) and works down it, so a
session that holds the host past 90% without exhausting it would end watcher's
database connections — a crash recovery once it escalates to SIGKILL at 5% — and
then watcher itself, where the kernel takes nothing until memory is actually
gone. Apart from those three, nothing on the list holds more than ~35 MB.
What contains a spiking session here is step 1: the pin keeps a launch from
installing a server at all, and a deliberate install runs under `choom -n 500`
inside a `MemoryMax=1536M` scope — the one session process earlyoom *could*
reach, and one the scope already contains. CannObserv/replicator#112 declined
it on the same class of host.

`tests/deploy/test_earlyoom_decline.py` pins both halves live: earlyoom is not
running (`apt install earlyoom` starts it at once, on stock arguments), and this
session's root still reads -1000. A host set up from the old runbook: `sudo
systemctl disable --now earlyoom`. If the premise flips — notifier's sessions
sit at 0, and what decides it is unknown — revisit the decline, and never
`$`-anchor `--prefer`: `comm` is truncated to 15 characters, so the server is
`npm exec socrat` and `^npm$` misses it (gregoryfoster/skills' `host-memory.md`
§4 lists the other traps). Re-measuring kills nothing and needs no root:

```bash
apt-get download earlyoom && dpkg-deb -x earlyoom_*.deb x   # in a scratch dir
timeout -s INT 2 ./x/usr/bin/earlyoom --dryrun -d -r 0 -m 99,98 -s 100,100 \
  --prefer '^(sshd|exe-init|MainThread|claude|node)' 2>&1 | grep -E 'new victim|^sending'
```

The `-d` table prints badness from *before* the -1000 skip, so a preferred
`sshd` shows 300 and is still passed over: read the `new victim` lines, not the
column.

**Docker is not among the spikers here any more.** #300 tore the daemon down
when the semantic index moved to the shared store on `co-index`, and #310
purged the packages themselves — so a stray invocation (an agent session, a
copied-in script, a vendored skill's preflight) can no longer socket-activate
`dockerd` plus `containerd` for ~120 MB here. `preflight.sh --check` reports it as *Docker
not needed*; a rebuilt VM that reinstalls `docker.io` gets that path back.

**4. The dependency chain takes reservations too — and so do the slices above it
(#309).** Killing or starving what watcher depends on is the same outage by
another route. Measured 2026-09-23, after the resize:

| cgroup | `memory.current` | `MemoryLow` | `OOMScoreAdjust` (live, per process) | Set by |
|---|---|---|---|---|
| `init.scope` (agent sessions) | 3093M | — | -1000 | exe-init / sshd |
| `system.slice` | 644M | **1G** | — | `deploy/dropins/system.slice.d/` |
| └ `watcher.service` | 282M | 512M | -500 (`uv`, `uvicorn`) | `deploy/watcher.service` |
| └ `system-postgresql.slice` | 122M | **384M** | — | `deploy/dropins/system-postgresql.slice.d/` |
| &nbsp;&nbsp;└ `postgresql@16-main.service` | 122M | **384M** | -900 postmaster, **0** backends | `deploy/dropins/postgresql@.service.d/` (the template); OOM from Debian's unit |
| └ `tailscaled.service` | 92M | **128M** | **-400** | `deploy/dropins/tailscaled.service.d/` |

**A unit keeps no more `memory.low` than every slice above it grants.** cgroup v2
bounds effective protection by every ancestor's, and `exe-init` mounts `cgroup2`
without `memory_recursiveprot` (which only hands a parent's protection down
anyway). With `system.slice` at 0, watcher's 512M showed in `systemctl show` and
protected nothing. The competitor is `init.scope` — the agent sessions — so the
slice's 1G (its children's sum, ~13% of the host) deliberately moves reclaim onto
them.

**Postgres's -900 is the postmaster's alone**: Debian resets backends to 0, so a
killed backend costs a crash recovery, not the database, and at 0 they rank
ahead of watcher. The unit is `Restart=no`. The drop-in targets the
`postgresql@` template so a major upgrade's cluster inherits it — while two run
side by side, the slice must cover both — and both reservations follow
`shared_buffers` (128M) if it is raised.

**`tailscaled` at -400** goes after everything at the default 0 but before
watcher: the dashboard is reached through the exe.dev proxy, not the tailnet.

Install from the checkout — **a rebuilt VM loses these silently**:

```bash
for u in system.slice system-postgresql.slice postgresql@.service tailscaled.service; do
  sudo install -D -m 644 deploy/dropins/$u.d/10-watcher-memory.conf \
    /etc/systemd/system/$u.d/10-watcher-memory.conf
done
sudo systemctl daemon-reload        # MemoryLow= applies live
sudo systemctl restart tailscaled   # OOMScoreAdjust= applies at exec only
```

Verify the kernel, not `systemctl show` — the test reads `/sys/fs/cgroup` and
`/proc` at every level:

```bash
uv run pytest tests/deploy/test_memory_dropins.py   # sums, drift, live values
```
