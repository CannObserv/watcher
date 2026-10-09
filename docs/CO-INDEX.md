# The shared index on co-index

How this repo's SocratiCode index lives in the cohort's shared store on `co-index` (#300): the client contract, what is and is not indexed there, and how to verify the store before trusting a miss. The tool reference is [SOCRATICODE.md](SOCRATICODE.md).

## Shared index on `co-index` (#300)

The vector store and the embedding model live on the cohort's fifth VM, `co-index`
(`index` on the tailnet), shared with archiver, broker, replicator and notifier.
Tracking issue: [CannObserv/notifier#57](https://github.com/CannObserv/notifier/issues/57);
design and decisions D0–D14 in `docs/plans/2026-09-11-shared-qdrant-vm-design.md` there.

**Why not locally.** A cold local index pulls two images and the embedding model and peaks
around **1.2 G** at the cgroup (measured on CannObserv/broker), on a swapless host where
agent sessions and any index they launch sat at `oom_score_adj` -1000 (0 since #337 —
killable, not affordable): the pressure landed on everything else, `watcher.service`
included (#307; [HOST-MEMORY.md](HOST-MEMORY.md)).

**The client contract is two committed files**, so every checkout addresses the same
collections wherever the working tree sits on disk:

| File | Carries | Tracked |
|---|---|---|
| `.socraticode.json` | `projectId` (names the collections: `codebase_watcher`, `watcher_symgraph_*`) and `linkedProjects` | yes |
| `.claude/settings.json` → `env` | the six non-secret client variables (`QDRANT_MODE`/`QDRANT_URL`, `OLLAMA_MODE`/`OLLAMA_URL`, `EMBEDDING_MODEL`/`EMBEDDING_DIMENSIONS`) | yes |
| `.claude/settings.local.json` | `QDRANT_API_KEY`, and nothing else | **never** — git-ignored |

[tests/deploy/test_socraticode_config.py](../tests/deploy/test_socraticode_config.py) pins
every value above and the index scope, and asks `git` — not `.gitignore` — whether the key
file is both ignored *and* untracked. Qdrant holds a single global `service.api_key`: no
key list, no per-client identity, no per-collection scope, so every cohort VM holds the
same secret and a leak anywhere is a rotation everywhere **with no overlap window**.
Install it with notifier's `scripts/install_qdrant_key.sh` (key on **stdin**, never argv;
never under `bash -x`), run from an operator machine — `tag:index:22` was retired after
the soak, so this VM cannot reach `co-index` over SSH. Expect `installed 64 chars`; any
other length is a truncated transfer, which 401s exactly like a wrong key.

Validate the contract with no server and no network:

```bash
D=skills-vendor/gregoryfoster-skills/skills/init-socraticode/scripts/mcp-driver.mjs
node $D validate-store .      # external mode, projectId, what the path hash would have been
node $D validate-manifest .   # every context artifact still resolves
node $D resolve               # which server a launch would get (the #307 pin)
```

**Traps, every one of them measured during the cohort's rollout.** All six fail *green*:

1. **A missing linked directory is dropped silently.** `loadLinkedProjects` filters on
   `fs.existsSync` with no warning, and `searchMultipleCollections` swallows a
   per-collection failure the same way — so a green `codebase_search` is never evidence
   that every sibling answered.
2. **`projectId` renames your collections.** Resolution order is `SOCRATICODE_PROJECT_ID`
   env > `.socraticode.json` > SHA-256 of the absolute path. Adopting the file orphans
   anything indexed under the path hash — here that would have been `3c54a78f3ffa`, and
   nothing was: the local store was verified empty before the file landed.
3. **`QDRANT_URL`, never `QDRANT_HOST`.** The fallback builds
   `${KEY ? https : http}://${QDRANT_HOST}:${QDRANT_PORT}` with `QDRANT_PORT` defaulting to
   **16333**, not 6333 — so a key with no URL assumes https against the wrong port and the
   error reads like a network fault.
4. **Full MagicDNS name only.** `https://index.taild0fb76.ts.net:6333`, not
   `https://index:6333` — the short name is not in the certificate's SAN. Qdrant serves TLS
   because SocratiCode *refuses* to send the key over a non-TLS, non-localhost connection
   (notifier#57 D14).
5. **Never set `QDRANT_COLLECTION_PREFIX` or `SOCRATICODE_BRANCH_AWARE`.** The prefix is
   prepended to the instance-global `socraticode_metadata` collection too, so one VM
   setting it splits the cohort namespace; branch-awareness appends the branch name to the
   project id, giving a fresh collection set per branch. The test file guards both across
   every env surface on this host.
6. **The session's server is the plugin's `npx`, not the pin.** `SOCRATICODE_SPEC` set
   before Claude Code starts pins its version (#322), never its path: the pin is reached
   by the health hook and `preflight.sh --check`, not the session. A cold npm cache makes
   that launch a ~1,700-tarball install, which does not fit Claude Code's 30s connect
   timeout: the session reports `CONNECTION_CLOSED` and runs with no tools while every
   check on the pin passes. Measured 2026-09-20 (#314), which is why `scripts/cleanup.sh`
   no longer wipes `~/.npm/_cacache` weekly. A session that lost the server reconnects
   with `/mcp`.

**The inverse trap: a red that succeeded** (*Per-tool notes*, `codebase_context_index`).
Measured here at ~0.4 s per chunk on co-index's shared CPU embedder: aborted 13:37:47Z,
finished 14:24:37Z (2026-09-22), the session blocked for all 30 minutes. While the
session's file watcher is live (`/tmp/socraticode-locks/<projectId>-watch.lock`) a save
lands in both collections within ~20 s, so usually there is nothing to run; staleness the
health hook reports comes from edits made with no watcher, and `codebase_update` repairs
it — not the `codebase_context_index` the hook prints (gregoryfoster/skills#317). Delegate
a full re-index to a background subagent; a second writer fails fast on
`/tmp/socraticode-locks/<projectId>-context.lock`.

**The `env` block applies only in a trusted folder.** Untrusted, `QDRANT_MODE` reverts to
`managed` and SocratiCode tries to start Docker rather than reporting missing
configuration — silent while a local store exists: archiver (CannObserv/archiver#226)
adopted with its containers up, and its stale local `codebase_archiver` answered ~23 %
short with no warning. Here the local store was confirmed empty **before**
`.socraticode.json` landed, then Docker was removed (2026-09-19; packages purged by #310,
whose five `autoremove` orphans a later `apt-get install docker.io` would bring back). A
revert now fails loud — `docker: command not found` — and nothing here prunes images.

**An already-running server never picks the `env` block up.** The server that indexes must
start *after* the block exists — a fresh session, or an out-of-band launch. Cap it: the
everyday launch paths (plugin, health hook, preflight) are uncapped, and broker took a
production VM down launching one (CannObserv/broker#17, design in broker#27).

```bash
systemd-run --user --scope -p MemoryHigh=1200M -p MemoryMax=1536M -p CPUQuota=100% \
  choom -n 500 -- node $D index .
```

`OOMScoreAdjust`/`choom` is not optional: at -1000 (sessions before #337, and again if a
rebuilt VM brings the old exe-init back) a cgroup cap stalls rather than kills.
Measured here 2026-09-19: **69 min wall** on
the pinned 1.14.0 server, installing nothing, `watcher.service` untouched, five green
collections and no path-hash collection for this repo.
