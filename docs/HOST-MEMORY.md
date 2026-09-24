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
48s, and a downstream consumer never reconnected. Three things answer that, and
none substitutes for another.

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
  block carries the same value, and alone it pins nothing — below.

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

**The settings `env` block is not enough.** Claude Code expands the plugin's
args before it merges the project block, then hands the merged environment to
the server anyway. Measured 2026-09-24 on 2.1.280, after a window reload: the
server's environment held `SOCRATICODE_SPEC=socraticode@1.14.0`, its argv
`npm exec socraticode@latest`. Present at exec (inherited, or `--settings`),
the same variable launched 1.14.0 on 2.1.280 and 2.1.281. The machine setting
is machine-scoped, so it cannot be a committed workspace setting; a terminal
`claude` needs the variable exported in its shell.

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
#    reload the window

# Says which path the driver takes, without launching a server:
node skills-vendor/gregoryfoster-skills/skills/init-socraticode/scripts/mcp-driver.mjs resolve
```

**Verify the process, never a report.** Only the launched argv is evidence:
`ps -eo args | grep 'npm exec socraticode'` must read `npm exec
socraticode@1.14.0`. `preflight.sh --check`, the daily health hook and `claude
mcp list` run from a session's shell all take the version from
`SOCRATICODE_SPEC` in their own environment — which the settings block puts
there whether or not the launch saw it. With the session on `@latest`, preflight
still printed *Plugin session launches socraticode 1.14.0 … no launch installs*.
Run bare, `claude mcp list` treats this folder as untrusted and reports
`@latest` either way.

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

**3. The kernel needs a reserve, and something must act before it is desperate.**
`vm.min_free_kbytes` shipped at ~8 MB here, which is what lets an atomic
allocation in `tailscaled` fail while memory is nominally available;
`/etc/sysctl.d/60-watcher-memory.conf` raises it to 64 MB. `earlyoom` then sheds
a process while userspace can still make progress — and because it ranks by
`oom_score`, the combination of an unpickable session (-1000) and a de-prioritised
service (-500) means it picks the `npm`/`node` process that is actually spiking,
at the default 0 — the family `--prefer` names below, and since #310 the only
one left here.

Neither of those two lives in this repo, so **a rebuilt VM loses both silently** —
nothing fails, the box is simply back to an 8 MB reserve and no shedder. Recreate
them as part of the install:

```bash
# 1. The kernel reserve. The shipped default here was ~8 MB, which is what lets
#    an ATOMIC allocation fail in an unrelated process while memory is nominally
#    available. 64 MB is ~0.8% of this host (7.75 GiB; ~1.6% before #309's resize).
sudo tee /etc/sysctl.d/60-watcher-memory.conf >/dev/null <<'CONF'
# Kernel free-memory reserve for co-watcher (watcher#307). See
# docs/HOST-MEMORY.md.
vm.min_free_kbytes = 65536
CONF
sudo sysctl --system

# 2. The shedder. Acts while userspace can still make progress, before the
#    kernel is reduced to picking a production service.
sudo apt-get install -y earlyoom
sudo tee /etc/default/earlyoom >/dev/null <<'CONF'
# earlyoom for co-watcher (watcher#307). --avoid names the processes whose
# death IS the outage: `uv` is watcher.service's main process and `uvicorn` its
# server child, postgres backs it, tailscaled carries every peer hop.
# --prefer names the node family, which is what actually spikes here.
EARLYOOM_ARGS="-r 3600 --avoid '^(uv|uvicorn|postgres|tailscaled|systemd|sshd|exe-init)$' --prefer '^(node|npm|MainThread)$'"
CONF
sudo systemctl restart earlyoom && sudo systemctl enable earlyoom
```

Verify:

```bash
cat /proc/sys/vm/min_free_kbytes    # 65536
systemctl is-active earlyoom        # active
```

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
killed backend costs a crash recovery, not the database. earlyoom's `--avoid`
covers them; the kernel's killer does not. The unit is `Restart=no`. The drop-in
targets the `postgresql@` template so a major upgrade's cluster inherits it —
while two run side by side, the slice must cover both — and both reservations
follow `shared_buffers` (128M) if it is raised.

**`tailscaled` at -400** sits below every `npm`/`node` process but behind
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

**earlyoom's `--avoid` list is the userspace half**: it ranks by `oom_score`, so
it honours each adjustment above, and acts at 10% free — ~800 MB here.
