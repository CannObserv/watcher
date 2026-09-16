# Tailscale — this node

Watcher reaches the broker and the notifier over the tailnet, not over the
public internet, so the node's identity is infrastructure rather than
convenience. This is the reference for it (#296 step 25).

## Identity

| | |
|---|---|
| Tailnet | `cannobserv.org.github` |
| Node name | **`watcher`** — MagicDNS `watcher.taild0fb76.ts.net` |
| OS hostname | **`co-watcher`** (they differ on purpose — see below) |
| Address | `100.66.24.24` |
| Tag | `tag:watcher`, non-expiring, pre-approved at join |
| Region | exe.dev `pdx`; nearest DERP **Seattle** |

**The two names differ, and both are load-bearing.** The tailnet node is
`watcher` because peers' ACLs and `WATCHER_BUS_REDIS_URL` name it; the OS
hostname is `co-watcher` because the backup's object prefix defaults to it, and
a host named `watcher` would have written into the retired VM's timeline
(`docs/RECOVERY.md` → *Restore*). A rename is survivable — `WATCHER_BACKUP_PREFIX`
overrides the prefix — but it is never *just* a rename: set that variable in the
same change, or the next nightly lands in the wrong timeline.

The exe.dev proxy is a separate path: `https://co-watcher.exe.xyz` is the public
dashboard (`WATCHER_PUBLIC_BASE_URL`), and from *inside* the VM that name
resolves to the VM's own address and skips the proxy — so a proxy check has to
come from outside.

**The old name is gone, and it is not ours any more.** The shared `lax` VM was
deleted on 2026-09-16 (#296 step 30, D12) and its tailnet node removed with it.
`watcher.exe.xyz` is therefore **released into exe.dev's global namespace, where
any account may claim it** — so every link delivered before the cutover is dead,
and anything that answers at that host in future is a stranger's, not a stale
copy of ours. Never treat it as this service, and never re-point anything at it.
The node name `watcher-lax` it carried at the end belongs to nothing.

## Who this node talks to

| Peer | Address | Used for |
|---|---|---|
| `broker` | `100.97.91.19` | the Redis bus — `WATCHER_BUS_REDIS_URL`, four streams out, two in |
| `notifier` | `100.98.9.17` | `http://notifier:9000` (prod) and `:9001` (dev tenant) |

Notifier binds its **tailnet address alone**, so a watcher off the tailnet
cannot reach it from loopback or from exe.dev's internal network — it starts
clean and fails every dispatch at call time.

## The cold-boot race (R4)

**MagicDNS answers after the address is up**, so a service that starts the
moment `tailscaled` does will resolve nothing. `deploy/watcher.service` orders
`After=tailscaled.service` but deliberately takes **no** `Wants=`/`Requires=`:
ordering alone does not close the race, and a Tailscale upgrade restarting the
agent must not take the dashboard down with it. What actually closes it is the
retry window on the startup probe in `src/core/bus.py`.

The first packets after a boot go by **DERP relay**, and a direct path forms a
few seconds later. Measured on the 2026-09-15 reboot: the bus PING answered in
52 ms on attempt 1 while relayed through DERP(sea), the direct path formed at
16:46:37, and latency settled at ~1 ms. Recorded for broker#8 as cold p50
7.31 ms, warm p50 1.55 ms. A single slow first reading is therefore normal and
not evidence of a problem; a *sustained* relay is.

Every number above came from this host and is re-derivable here; the reboot and
latency figures are the #296 step 22/24 records, posted on that issue and on
broker#8. Check which path is live:

```bash
tailscale status | grep broker      # "direct 16.145.19.221:13487" vs "relay sea"
tailscale netcheck | head -20
```

## Policy

Two rules matter here and are checked at every policy edit (#296 D3):

- **No `tag:watcher` source in any `ssh` or `:22` rule.** The tag carried
  operator reach on its first join, which is why the policy edit was a
  prerequisite to joining rather than a follow-up. This node must not be an SSH
  origin for anything.
- `autogroup:member → *:*` stays, so operators keep their own reach.

The two VMs of the migration **cannot reach each other** by design (D3/D10);
the temporary one-way edge used to copy the workspace was removed on
2026-09-15.

## If the node has to rejoin

Mint a key that is `tag:watcher` only, pre-approved, tagged, and **not**
ephemeral — an ephemeral node disappears from the tailnet on a clean shutdown
and takes its ACL grants with it. Then confirm, in this order: `tailscale
status` shows the tag; `curl http://notifier:9000/health` answers; the bus PING
in the service log succeeds on boot. Removing the VM does **not** remove its
node record — that is a separate deletion in the admin console (#296 step 30).
