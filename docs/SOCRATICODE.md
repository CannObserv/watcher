# SocratiCode — the shared index on `co-index`

Watcher does not host its semantic index. This doc is the client contract
against the cohort's shared Qdrant, the traps that fail *green*, and the
verification commands.

Everything else about SocratiCode — the goal→tool table, the workflow, index
scope, the SessionStart hooks and the `ToolSearch` prefetch query — stays in
[SKILLS.md](SKILLS.md).

## Shared index on `co-index` (#300)

Watcher hosts no semantic index. Both the vector store and the embedding model live
on the cohort's fifth VM, `co-index` (`index` on the tailnet), shared with archiver,
broker, replicator and notifier. Tracking issue:
[CannObserv/notifier#57](https://github.com/CannObserv/notifier/issues/57); design and
decisions D0–D14 in `docs/plans/2026-09-11-shared-qdrant-vm-design.md` there.

**Why not locally.** #307 measured this host at **3.8 GiB with no swap**, sharing memory
with `watcher.service` and with agent sessions the OOM killer cannot pick
(`oom_score_adj` -1000 from exe-init/sshd — so it takes the service instead). A cold
local index pulls the Qdrant and Ollama images plus the embedding model and peaks around
**1.2 G** at the cgroup, measured on CannObserv/broker. Adoption is not tidiness here; it
is the difference between a host that can support an index and one that cannot.

**The client contract is two committed files**, so every checkout addresses the same
collections wherever the working tree sits on disk:

| File | Carries | Tracked |
|---|---|---|
| `.socraticode.json` | `projectId` (names the collections: `codebase_watcher`, `watcher_symgraph_*`) and `linkedProjects` | yes |
| `.claude/settings.json` → `env` | the six non-secret client variables (`QDRANT_MODE`/`QDRANT_URL`, `OLLAMA_MODE`/`OLLAMA_URL`, `EMBEDDING_MODEL`/`EMBEDDING_DIMENSIONS`) | yes |
| `.claude/settings.local.json` | `QDRANT_API_KEY`, and nothing else | **never** — git-ignored |

[tests/deploy/test_socraticode_config.py](../tests/deploy/test_socraticode_config.py) pins
every value above, and asks `git` — not `.gitignore` — whether the key file is both
ignored *and* untracked. Qdrant holds a single global `service.api_key`: no key list, no
per-client identity, no per-collection scope, so every cohort VM holds the same secret and
a leak anywhere is a rotation everywhere **with no overlap window**. Install it with
notifier's `scripts/install_qdrant_key.sh` (key on **stdin**, never argv; never under
`bash -x`), run from an operator machine — `tag:index:22` was retired after the soak, so
this VM cannot reach `co-index` over SSH. Expect `installed 64 chars`; any other length is
a truncated transfer, which 401s exactly like a wrong key.

Validate the contract with no server and no network:

```bash
D=skills-vendor/gregoryfoster-skills/skills/init-socraticode/scripts/mcp-driver.mjs
node $D validate-store .      # external mode, projectId, what the path hash would have been
node $D validate-manifest .   # the 10 context artifacts still resolve
node $D resolve               # which server a launch would get (the #307 pin)
```

**Traps, every one of them measured during the cohort's rollout.** All five fail *green*:

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

**The `env` block applies only in a trusted folder.** Untrusted, `QDRANT_MODE` reverts to
`managed` and `OLLAMA_MODE` to `auto`, and SocratiCode tries to start Docker containers
rather than reporting missing configuration. That failure is loud only while no local store
exists — archiver (CannObserv/archiver#226) adopted with its managed containers still up
and ended with two collections named `codebase_archiver`, the stale local one answering
~23 % short with no warning at either layer. Hence the order used here: the managed
container was stopped and the local store confirmed empty **before** `.socraticode.json`
landed. **Docker was then torn down here** (2026-09-19, once the shared index verified):
container, image and the empty `socraticode_qdrant_data` volume removed, then
`systemctl disable --now docker.socket docker.service` and `containerd` stopped — ~85 MB
of resident `containerd` and 277 MB of image. `docker ps` now answers *Cannot connect to
the Docker daemon*, which is the loud failure trap 6 assumes rather than a stale local
collection answering quietly.

That teardown is why `scripts/cleanup.sh` asks systemd before pruning images instead of
testing for the binary: the binary is still installed, the daemon is not, and the script
runs under `set -euo pipefail` — so an unguarded `docker image prune -f` would abort the
weekly run at that line and silently skip the journal vacuum below it
([tests/deploy/test_cleanup_docker_guard.py](../tests/deploy/test_cleanup_docker_guard.py)).

**An already-running server never picks the `env` block up.** The server that indexes must
start *after* the block exists — a fresh session, or an out-of-band launch. Cap it: the
everyday launch paths (plugin, health hook, preflight) are uncapped, and broker took a
production VM down launching one (CannObserv/broker#17, design in broker#27).

```bash
systemd-run --user --scope -p MemoryHigh=1200M -p MemoryMax=1536M -p CPUQuota=100% \
  choom -n 500 -- node $D index .
```

`OOMScoreAdjust`/`choom` is not optional — a session-launched process inherits -1000 and a
cgroup cap on it stalls rather than kills.

**Measured here, 2026-09-19.** That exact invocation ran **69 min wall** on the pinned
1.14.0 server, installing nothing, and left five green collections in the shared store:

| Collection | Points |
|---|---|
| `codebase_watcher` | 3820 |
| `context_watcher` | 1495 |
| `watcher_symgraph_file` | 344 |
| `watcher_symgraph_index` | 157 |
| `watcher_symgraph_meta` | 1 |

`watcher.service` was untouched throughout, and the store holds no path-hash collection
for this repo — the rename in trap 2 orphaned nothing, because there was nothing to orphan.

## Linked projects and cross-repo search

`linkedProjects` in `.socraticode.json` names the other four cohort repos by **relative**
path — `../archiver`, `../broker`, `../replicator`, `../notifier`. Pass
`includeLinked: true` on `codebase_search` to fan out; results carry a `[watcher]` /
`[archiver]` / … label.

Each entry is used for exactly two things: the directory's own `.socraticode.json` gives
the **project id** (→ collection name), and the directory **basename** gives the display
label. No path reaches the search — every byte of sibling source comes from Qdrant. So a
sibling needs a **link stub**, not a clone:

```
/home/exedev/<sibling>/.socraticode.json    →  { "projectId": "<sibling>" }
```

`../broker`, `../replicator` and `../notifier` are stubs on this VM, each with a `README.md`
saying so — `/home/exedev/notifier` is **1202 bytes, two files, zero source**, and notifier
hits come back with real paths and line numbers all the same.

**`../archiver` is the exception, and must stay a real checkout.** `tests/conftest.py`
defaults `ARCHIVER_REPO_PATH` to `/home/exedev/archiver` and runs that repo's alembic to
build the `information` schema — so the same path serves two unrelated consumers, and
replacing it with a stub turns four tests red
(`tests/test_conftest_archiver_migrations.py`). That was tried on 2026-09-19 and reverted.
Check `ARCHIVER_REPO_PATH` before touching this one; the other three have no such
consumer.

Which means the clone-drift hazard is real here rather than avoidable. That checkout
predated archiver#226, so it carried no `.socraticode.json` and fell through to the SHA-256
of its absolute path — `codebase_7a9d625938ee`, which nothing has ever indexed. Measured
before the pull: a query aimed squarely at archiver's registry domain returned `[watcher]`,
`[replicator]` and `[broker]` hits and **not one `[archiver]` hit**. After
`git -C /home/exedev/archiver pull`, the same query returns five. **Keeping that clone
current is now a search dependency, not just hygiene.**

Note what this does *not* trip: the health check counts a linked path as resolved when the
**directory** exists, so it reports `4 of 4` while archiver answers nothing. Resolution is
the floor, not the proof — which is trap 1 restated, and the reason `includeLinked` needs a
query whose answer you already know before you trust it.

`SOCRATICODE_LINKED_PROJECTS` is **not** an override — `loadLinkedProjects` unions it with
the file into one `Set`. The absolute `/home/exedev/notifier` this repo carried in
`settings.local.json` was therefore never a conflict, just a no-op that `fs.existsSync`
dropped without a word; #300 removed it in favour of the committed relative entries.

Confirm the set resolves — this is the only thing that will tell you a stub is missing:

```bash
node $D health-check .   # → linkedProjects: configured 4, missing []
```

