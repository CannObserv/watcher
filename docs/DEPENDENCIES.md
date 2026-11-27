# Dependency sources

The runtime dependencies that resolve from somewhere other than PyPI: co-core and co-core-aio from the private co-pypi index. How each is fetched, authenticated, pinned and upgraded. Plus certifi, the one transitive floored by constraint, and the advisory fixes whose floor is the lock. Deployment itself is [DEPLOYMENT.md](DEPLOYMENT.md).

## The co-pypi index

**co-core from co-pypi (#358).** `co-core` + `co-core-aio` (the shared cannabis-observer substrate) resolve from the private Artifact Registry index `https://us-west1-python.pkg.dev/co-gcs/co-pypi/simple/` (`[[tool.uv.index]] name = "co-pypi"`), the rules being cannobserv's RELEASING.md → *Path C*. Each holds quietly, so `tests/test_dependency_index.py` pins each one:
- **The URL is the org registry's byte for byte**, trailing slash included: Dependabot matches it by string.
- **`explicit = true`, plus a `[tool.uv.sources]` entry per name.** co-core never comes from PyPI, where the names are unclaimed, and nothing else comes from co-pypi.
- **Every co-core package is declared directly.** uv binds a source to a direct dependency only.
- **`uv.lock` holds each file's sha256, and `uv sync` refuses a file that misses it.** That retired #393's `find-links` wheelhouse, its digest table and both scripts: a `find-links` path locks no hash. Before the table went, its four 0.37.0 rows were proven equal to the index's (#358).

`gs://co-gcs-pypi` stays as cannobserv's mirror, and nothing here reads it. Since cannobserv v0.41.0 every release publishes to the index, and the publish fails unless the index lists each file under its local sha256.

**Auth: a token, per command.** The index takes the username `oauth2accesstoken` and an access token as the password, which uv reads from `UV_INDEX_CO_PYPI_USERNAME` / `UV_INDEX_CO_PYPI_PASSWORD`. **Never put either in an env file.**
- **On the host,** which has no gcloud, `bash scripts/co-pypi.sh uv <command>` runs `scripts/co_pypi_token.py` under `uv run --no-project --with google-auth`. It mints the token from the key at `GOOGLE_APPLICATION_CREDENTIALS` (`/etc/watcher/.env`: `co-pypi-reader.json`, `roles/artifactregistry.reader`), then `exec`s the one `uv` command with the pair in its environment alone.
  - It wraps nothing but `uv`, and never prints the token.
  - It needs `source scripts/load-env.sh` first.
  - Pinned by `tests/scripts/test_co_pypi_token.py`.
- **In CI,** each WIF auth step sets `token_format: access_token`, and only the `uv sync` step gets the pair, from the masked `steps.auth.outputs.access_token` (`tests/ci/test_co_pypi_ci.py`).
- **Needs it:** every `uv lock`, even `--upgrade-package` of a PyPI package (measured: 401 on co-pypi, warm cache or cold), and a `uv sync` that has to download. Every recipe below that locks or syncs runs through the wrapper. **Doesn't:** `uv sync --locked --offline` (the start check), `uv audit --frozen`, a `uv run` whose venv already matches the lock.

**Upgrade (#408, #414):**
1. Edit both `==` pins (`co-core`, `co-core-aio[bus]`) in `pyproject.toml`.
2. `bash scripts/co-pypi.sh uv lock`. The pin is exact, so `--upgrade-package` alone moves nothing.
3. `bash scripts/co-pypi.sh uv sync`.

On the host, after the merge, run `bash scripts/co-pypi.sh uv sync --locked`, then restart. Currently pinned: **v0.53.2** (#457), taken for cannobserv#780: `RegistryAnnouncementState.fetch_headers` / `fetch_credential_headers`, the fetch-header policy the reconcile stores and each command carries ([CONTENT-PIPELINE.md](CONTENT-PIPELINE.md)), beside 0.51.1's `ContentFetchCommand.credential_headers` (cannobserv#577). Earlier pins, what each carried and what its wheel diff found: [DEPENDENCIES-HISTORY.md](DEPENDENCIES-HISTORY.md).

**The 0.48.2 → 0.53.2 diff** (five MINORs), wheel-to-wheel on every module watcher imports; the synced venv is byte-identical to the tree diffed:
- `changes.py`: `ContentFetchCommand` gains `credential_headers`, which `ContentFetchCommandEmit` refuses under `browser`, off `fetch-<role>`, twice by case, as `user-agent` or a validator, or beside the same name in `headers`. Both Emits gain `hide_input_in_errors`. `RegistryAnnouncementState` gains the two maps, untyped `dict | None`, narrowed and refused on `RegistryAnnouncementEmit`. With no policy, every frame now ends `"credential_headers": null`. `FetchPolicyState.generation` is required (0.53.0); watcher reads no `FetchPolicyState`.
- `util/aliases.py` adds `validate_fetch_credential` and the anchored pattern (0.52.0) that `changes.py` imports. `envelope.py` and `extract/__init__.py` move in docstrings only (#559's `include_description`, read by Processor's extractor alone).
- Byte-identical: `effects/bus.py`, `bus/__init__.py`, `streams.py`, `bus/dead_letter.py`, `bus/exceptions.py`, `extract/media_type.py`, `extract/canonical.py`, `extract/spec_fingerprint.py`, `extract/simhash.py`, `util/blobstore.py`, `util/hashing.py`, `util/media_type.py` and all of `co_core_aio/bus.py`. Both `__init__` modules move only in `__version__`.
- Everything else that moved is a surface watcher does not import: co/v1, WordPress, transcripts, the extractors, `workflows/`, and the new `storage/`, `resources/` and audacity modules. No Breaking entry 0.49.0–0.53.2 has a caller in `src/`, `tests/` or `scripts/`. The `uv.lock` diff moves co-core and co-core-aio alone.

**Deploy order is free:** the new `credential_headers: null` decodes on every consumer (`extra="ignore"`), and Archiver sets neither map until Watcher announces support on cannobserv#780. **The `bus` extra still caps `redis<8`** (*Dependabot PRs*).

**Every bump is diffed wheel-to-wheel** on every module watcher imports — a floor spanning several MINORs is not covered by any one release's notes. The current pin's diff lives above; on the next bump it moves to that version's history entry. The download goes through the wrapper.

`co-core` carries no extra: the `extract` parsers left with local extraction (#350). The content-acquisition pipeline (fetch → extract → fingerprint) is co-core's, run by Replicator and the processor; watcher no longer fetches at all (#241 step 5) — `src/core/fetch.py` is gone; the `watcher/0.1.0` User-Agent now lives in `src/core/fetch_commands.py` beside its only consumer and rides out on every `content.fetch` command's headers to preserve fingerprint byte-continuity, unless the item's announced policy replaces it (#457).

**The start check (#358).** The unit's `ExecStart` is `uv run uvicorn …`, and `uv run` syncs first. A sync that has to download from co-pypi fails, because the unit holds no token. So one fail-closed `ExecStartPre` runs before it: `uv sync --locked --offline`. Each line measured under `unshare -n`, so a boot ahead of the network can't trip it:
- **A matching venv** prints `Checked N packages` and exits 0.
- **A `pyproject.toml` edit, a comment included,** rebuilds the project offline and exits 0. uv keys the project's own install on that file, which is why this is not `--check`: that exited 1 on every start after the #358 merge until a manual sync.
- **An extraneous package** is removed; it's exact, unlike `uv run`. **A locked file missing from the venv** is reinstalled from uv's cache against the lock's sha256.
- **A locked file in neither** exits 1: ``error: Failed to download `co-core-aio==0.37.0` ``, cause *Network connectivity is disabled*. **A `pyproject.toml` the lock no longer matches** exits 1 too, as a resolution failure.
- **The fix is the deploy's sync** through the wrapper.
- **`scripts/dev_server.sh` runs the same command first (#401),** and refuses with the fix named. Its dry run reports `LOCK_CHECK=run`.

**Trap:** a co-core installed by `uv pip install <file>` records a `file://` source, which the index lock doesn't take; the fix is the same sync. A venv `uv sync` built from the old wheelhouse does match: #358 measured the live one.

**Accepted limits:**
- **The check runs only where it's wired in,** the unit and the dev server. A hand-run `uv run` in the deploy checkout still syncs the service's venv. Since #358 it can't download co-core without the wrapper, so it fails rather than installing something unchecked.
- **The token reaches whatever the wrapped `uv` spawns:** the build backend for the editable project (and `git`, should a git source come back). CI's `uv sync` step is the same. The bound is `co-pypi-reader`'s read-only role and the token's hour.
- **Dependabot's version lookup asks PyPI before the index,** where the co-core names are unclaimed. Who registers them is CannObserv/replicator#169's open decision. Both names are ignored, so the updater never looks them up, and uv's resolution (`explicit`, a source per name) never asks PyPI. Dropping an ignore brings the limit back: a `uv` PR that moves co-core, or takes its `source` off the index URL, is a stop.
- **Rollback:** reverting #358 restores `find-links` and `scripts/sync_wheelhouse.py`; run it to refill `.wheelhouse/` before a restart. The deploy checkout's copy was deleted 2026-11-06, after part 1's first restart.

## notifier-client

**Retired by #379.** The notifier SDK, Watcher's one git-tag source (#284), went with the client that used it; Watcher holds no git source now. `tests/test_dependency_sources.py` still fails one that is not HTTPS or not pinned by `tag`, and any workflow that rewrites a git URL (`insteadOf`), should one come back.

## certifi floor

**certifi (#391).** httpx and requests trust certifi's bundle **instead of** the OS store, so an OS-level distrust (apt, exe.dev's remediation) never reaches the GCS calls (backup, restore) or the operator probe. certifi is transitive — under httpx and `google-cloud-storage` → requests — and nothing imports it, so its floor is `[tool.uv] constraint-dependencies = ["certifi>=2026.10.19"]`, not a dependency line; it resolves from PyPI. 2026.10.19 drops **emSign ECC Root CA - G3**, the root the leaked eMudhra intermediates chain to (GHSA-6q48-6c7w-9gw2); it deliberately keeps emSign G1, C1 and C3, so check for G3, not for "no emSign". `tests/test_dependency_certifi_floor.py` fails if the constraint is dropped or admits an older release, if the installed certifi is below it, or if its bundle still carries G3. **Raise the floor** the same way for a future distrust: bump the constraint and the test's `_FLOOR`, `bash scripts/co-pypi.sh uv lock`, then `… uv sync`, then `sudo systemctl restart watcher` — the running process keeps the bundle it loaded until it restarts; `watcher-backup.service` and a restore are fresh processes and pick it up on their own. The alternative, `truststore` (defer to the OS store), touches every client construction site including co-core's and the GCS client's, and was not taken.

## Advisory floors

**Lock-only advisory fixes (#392).** A runtime advisory fixed by moving the lock alone (`uv lock --upgrade-package <pkg>`) keeps its floor in `uv.lock`, not in `pyproject.toml`: no declared bound moves, and `tests/test_dependency_advisory_floors.py` fails if the *installed* release of any row drops below the one that fixed its advisories. It never asks OSV — `bash scripts/audit_lock.sh` does that (`uv audit --frozen`, runtime and dev apart), and needs the network. **Fix the next one** the same way: `bash scripts/co-pypi.sh uv lock --upgrade-package <pkg>` (one package per commit, so a bisect lands on it), add its row, `… uv sync`, then `sudo systemctl restart watcher` after the merge. A transitive that needs a floor no lock bump can hold takes a constraint, as certifi does above. **Ordering trap:** anyio ≥ 4.15 deprecates an alias starlette < 1.7 imports in `TestClient`, and the suite's `error::DeprecationWarning` filter makes that a collection error — move starlette first.

**The dev group too (#400).** The host's patch run audits the whole lock (`uv audit --frozen`, both groups: #382's `pending.language`), and the service venv is synced with `dev`, so a dev advisory is fixed the same way and gets a row. A dev package's upgrade may need a specifier to stay in its major: #400 ran `-P 'soupsieve>=2.9.0,<3'`, since bare `-P soupsieve` took 3.x (soupsieve left the lock with beautifulsoup4 in #377). The specifier binds that one run, never the lock. **Removing** a dependency needs no extra host step: the unit's `uv run` is an inexact sync and never uninstalls, but its start step is an exact offline sync (#358), so it removes a dropped package.

**Finding the next one (#397).** `.github/workflows/audit.yml` runs `scripts/audit_lock.sh` weekly, on a push or PR to `main` that touches `uv.lock`, `pyproject.toml` or the audit itself, and on `workflow_dispatch`. It is **report-only**: the job is always green, and a finding is a warning annotation (`uv.lock runtime advisories` / `uv.lock dev advisories`) plus the run's job summary. An `… audit did not run` warning means OSV or uv failed, never a clean lock. No allowlist yet: the first advisory accepted rather than fixed adds one, each ID with a reason and a review-by date (uv's `--ignore <ID>`). Pinned by `tests/scripts/test_audit_lock.py` and `tests/ci/test_audit_workflow.py`.

## Dependabot PRs (#283)

`.github/dependabot.yml` runs two weekly updaters; `tests/ci/test_dependabot.py`
pins both, because a broken config goes quiet rather than red.
- **`github-actions`:** one PR per action. Every `uses:` is `@<sha> # vX.Y.Z`
  (#360); the same test fails any other form.
- **`uv` (#358):** one PR per runtime package and one `dev-tools` PR for the dev
  group, `increase-if-necessary` (a bump past a cap rewrites the cap). It reads
  co-core from the co-pypi index over OIDC: cannobserv's org registry admits
  this repository, and `pyproject.toml`'s URL equals the registry's byte for
  byte. **Every `==` runtime pin is ignored by name** (co-core and co-core-aio
  today): they move when Processor coordinates a contract change (#408, #414),
  and the test derives the list. The open-PR limit outlasts the runtime
  packages not ignored plus the group, derived the same way.

**A Dependabot run reads its own credential context.** Its `GITHUB_TOKEN` is
read-only and only *Dependabot* secrets reach it, so every `ci.yml` job (all four
use WIF) depends on `vars.GCP_WIF_PROVIDER` resolving and on GitHub issuing the
OIDC token there. Both held on the first batch (#354–#357, 2026-10-09: every
job green, WIF auth and wheelhouse sync included, actor `dependabot[bot]`). On a red PR read the **Authenticate to Google Cloud** step
first, and match its error. *"must specify exactly one of
workload_identity_provider or credentials_json"* — the variable did not reach the
run. *"did not inject $ACTIONS_ID_TOKEN_REQUEST_TOKEN"* — no OIDC token was
granted. Past both, a token-exchange failure — the `github-ci` provider's
attribute condition or the SA binding ([COMMANDS.md](COMMANDS.md) → *CI*). None is the bump's fault.

**How one lands — by squash PR, like everything else (#445).** Dependabot opens
the PR; the `main` ruleset merges it on GitHub only once `lint`, `test`,
`integration` and `migrations` pass on a branch up to date with `main`. The host
pulls afterwards:

```bash
# Read the action's release notes, not just the diff: a major bump can change inputs.
gh pr comment <N> --body "@dependabot rebase"  # only if main moved; then the base check (unanswered? below)
gh pr view <N> --json mergeStateStatus -q .mergeStateStatus  # CLEAN: on main; BEHIND/DIRTY: not yet; else: the exact form below
gh pr checks <N> --watch --required            # the four required checks, green on the head
gh pr merge <N> --squash                       # refused while the branch is behind or a check is red
git pull --ff-only                             # on the host, in the main checkout: the deploy
```

**A moved head isn't a rebased one (#454).** A landing that conflicts in
`uv.lock` makes Dependabot rebase the batch's other PRs onto it unasked, so by
the next landing a head that moved is already behind. So the base check, not a
force-push, says the request was answered. `BLOCKED` (read on an up-to-date
head while its checks ran, #455) says only that the merge is blocked, not
where the head sits, so anything but `CLEAN`, `BEHIND` or `DIRTY` goes to the
exact form. It fetches the PR ref **first**, since `FETCH_HEAD` names the
first ref fetched and `git fetch origin main "pull/<N>/head"` would test `main`
against itself (`origin/main` still updates):

```bash
git fetch -q origin "pull/<N>/head" main && git merge-base --is-ancestor origin/main FETCH_HEAD  # 0: on main; 1: not yet; else: failed
```

`--watch` starts once the base check passes **and** `gh pr checks <N>` lists
the new head's runs: they register seconds after the push, and a watch begun
earlier can report too soon (cosmetic; `gh pr merge` refuses regardless).

Never GitHub's *Update branch* on a Dependabot PR nobody edited: it adds a commit
that isn't Dependabot's, and Dependabot then stops rebasing that PR.

**A rebase request can go unanswered** (6 of 20 in the first `uv` batch, #416).
If the base check still fails about 6 minutes after the request, with no
Dependabot reply and no *Dependabot Updates* job running, post it again: only a
push onto current `main` answers it. A reply is an answer (it refuses a PR
someone edited): read it, don't re-post. A single-package PR gets re-posts
until one takes; a **group** PR unanswered twice gets `@dependabot recreate`
instead: it rebuilds the PR from `main` (#442: the same diff, in 2 minutes) and
drops any manual edit, so only on a PR nobody edited.

Strict checks: the tree CI tested is the tree that lands. The squash commit gets
a new SHA, but the branch had to be up to date, so after the pull
`git fetch -q origin pull/<N>/head && git rev-parse FETCH_HEAD^{tree}` equals
`git rev-parse HEAD^{tree}` (the head branch is deleted on merge, so its commit
is fetched by the PR ref), and the push run re-tests it on `main`. The PR's
title already takes the repo's form (`chore: bump …`, the `commit-message`
prefix). One PR at a time — after each merge the next is behind and needs
`@dependabot rebase`. **Restart**
(`sudo systemctl restart watcher`) only when the diff touches something outside
`.github/`; an action bump never does.

**A `uv` bump also needs the host's venv to match before any restart.** The
unit's start step is `uv sync --locked --offline`, fail-closed (*The start
check*): a locked file that's in neither the venv nor uv's cache stops the next
start, a crash restart included. So each landing syncs, and the batch restarts
once:

```bash
gh pr diff <N> | grep -E '^[-+].*co[-_]core'   # before the merge: must print nothing
# …the merge and the pull above, then:
source scripts/load-env.sh && bash scripts/co-pypi.sh uv sync --locked
bash scripts/pre-ship.sh                       # tests/test_dependency_index.py: co-core still on the index
# After the batch's last landing:
sudo systemctl restart watcher                 # then /health reports the SHA, and NRestarts=0
```

- **co-core moving is a stop.** The ignore keeps the bot off it; without one,
  its version lookup asks PyPI first, where the names are unclaimed (*Accepted
  limits*).
- **A cap-crossing PR** rewrites the `pyproject.toml` specifier. It's a major:
  read its changelog against the code, as #294's caps intend.
- **redis majors are ignored (#441), and co-core-aio holds them anyway:**
  0.53.2's `bus` extra still requires `redis<8,>=5` (measured, #457), so no
  redis 8 bump resolves. The operator tied taking 8 to CannObserv/broker#85,
  the broker's next Redis (2026-11-17), so the `uv` block ignores redis majors
  only. 7.x minors and patches still flow. Remove the entry from
  `.github/dependabot.yml` and from `NARROWED_IGNORES` in
  `tests/ci/test_dependabot.py` once broker#85 settles **and** a co-core-aio
  release lifts `<8`. Then re-measure docs/BUS-CONNECTION-POLICY.md (redis-py 7.4.1).
- **A security PR** (its body lists the advisory) is a lock-only fix: add its
  `_FLOORS` row (*Advisory floors*) as a commit pushed to its branch before the
  merge (CI re-runs on it; Dependabot then won't rebase that PR, so merge it
  next, or bring it up to date with *Update branch*), or as a follow-up PR.
- **A batch shares `uv.lock`**, as do the hand-made co-core re-locks (#408,
  #414), so after each landing the next PR may need `@dependabot rebase`.
  `audit.yml` runs on each one (report-only).
- **Between landings main is ahead of live,** which the synced venv covers: a
  crash restart starts the landed lock.

**Is the `uv` updater running?** A failed run never shows as a red PR. Read the
job log (Actions → *Dependabot Updates*) for `registered gcp OIDC credentials
for python index`, `All versions of co-core ignored, no update allowed`, and,
once it re-locks for a bump, `authenticating request with OIDC token (host:
us-west1-python.pkg.dev)` and a `200`. A run with nothing to bump re-locks
nothing, so it may log neither of the last two. Symptoms: cannobserv
DEPENDABOT.md → step 4.

**`context-cadence.yml` is generated** (`install-cadence.sh`, vendored in
`skills-vendor/`), and Dependabot bumps its `actions/checkout` with `ci.yml`'s.
Take the bump: a re-run of the generator keeps an installed SHA pin
(gregoryfoster/skills#373; `--check` says `checkout pin: kept`). Since #445 it
renders the `pr` workflow (`.skills/merge_mode`): each run opens or updates one
PR from `context/weekly-measurement`. `GITHUB_TOKEN` opened it, so no check runs
on it until someone closes and reopens it. A run not on `main` fails before
measuring (gregoryfoster/skills#683). `main` is squash-only, so the PR merged mid-run fails the
run too: dispatch a fresh one (`gh workflow run context-cadence.yml`), never
*Re-run jobs*.
