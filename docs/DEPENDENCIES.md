# Dependency sources

The runtime dependencies that resolve from somewhere other than PyPI: co-core and co-core-aio from the private co-pypi index, and the notifier SDK from a git tag. How each is fetched, authenticated, pinned and upgraded. Plus certifi, the one transitive floored by constraint, and the advisory fixes whose floor is the lock. Deployment itself is [DEPLOYMENT.md](DEPLOYMENT.md).

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

On the host, after the merge, run `bash scripts/co-pypi.sh uv sync --locked`, then restart. Currently pinned: **v0.37.0** (#370, #386), the headless-Watcher contract set (cannobserv#568): `WatchStatusState`'s three #565 levels, which #370 publishes, and `ProcessingFailureReason`'s `dead_lettered`, which #386 routes. The 0.32.0 → 0.37.0 catch-up was diffed wheel-to-wheel on every module watcher imports: `changes.py` only adds (the three levels and `WatchStatusEmit`'s refusal of a reason or code outside `"error"`, `FetchPolicyState.generation`, `dead_lettered` on the `Literal`, the `info.watch-events` / `info.watch-check` payloads; `Generation` moves, unchanged); `envelope.py` and `streams.py` add those two streams' dispatch entries, constants and kinds, and otherwise move in comments; `effects/bus`, `bus/exceptions.py`, `extract/__init__.py`, `util/hashing.py`, `util/blobstore.py` and all of `co_core_aio/bus.py` are byte-identical. Every Breaking entry 0.33.0–0.36.0 is on co/v1 or WordPress models this repo does not import. **Deploy order is free:** the new columns are Watcher's own, and `dead_lettered` decodes on any Watcher (`reason: str`). Previously **v0.32.0**, exact since #325's shadow (bumped when CannObserv/processor coordinates a contract change; 0.32.0 carries cannobserv#523: `extension_media_type` gains `.xml` → `application/xml`, `.rss` → `application/rss+xml` and `.atom` → `application/atom+xml`, so a feed URL behind a missing or ambiguous header now resolves to the XML essence Processor's `XmlExtractor` takes — taken in step with CannObserv/processor#29 (#366), because Processor never re-resolves the essence on the `content.process` command. The 0.19.7 → 0.32.0 catch-up was diffed wheel-to-wheel on every module watcher imports: `extract/media_type.py` gains only those three extensions; `extract/__init__.py` adds `UnsupportedExtractionError` and `XmlExtractConfig` to its re-exports and still pulls no parser; `streams.py` and `changes.py` move in docstrings and comments only (`extraction_error` now also names a refused spec; the `Literal` is unchanged); `envelope.py`, `bus/exceptions.py`, `effects/bus`, `util/hashing.py`, `util/blobstore.py`, `extract/canonical.py`, `extract/spec_fingerprint.py` and all of `co_core_aio/bus.py` are byte-identical. Everything else that moved — `co_v1` / `wordpress` adapters, the non-contract models, `workflows/`, the concrete parsers, `dispatch.py` and `extraction_config_from_spec`'s new refusals — is a surface watcher does not import (#350). **Deploy order is free:** 0.19.7's `extractor_for_essence` sends an unknown essence to HTML, so a Watcher-first deploy extracts a feed exactly as before until Processor takes 0.32.0. Previously **v0.19.7** — floors were `>=0.19.6,<0.20` — 0.19.6 carries `SourceRevisionObservedEvent.blob_fingerprint` (cannobserv#493), the raw-bytes digest #329 echoes onto `content.revisions`, and its Emit twin's bare-hex validator; 0.19.7 is docs plus the `destination_refused` failure token, which watcher journals generically because it enumerates no reasons. Diffed wheel-to-wheel against 0.19.4, every module watcher imports that moved moved additively: `changes.py`, `envelope.py` and `streams.py` gain the persist pair (`ContentPersistCommand` / `BlobPersistedEvent` / `PersistFailedEvent`, `content.persist`, their envelope keys) and `blobstore.py` gains `FingerprintMismatch` and the signed-URL bounds; watcher publishes and consumes none of them. Previously **v0.19.4** — floors `>=0.19.4,<0.20`: 0.19.4 carries cannobserv#486: `canonical_text` / `canonical_text_fingerprint` / `processor_version` / `spec_schema_version`, the lifted media-type dispatch that `src/core/media_type.py` now re-exports, and the `content.process` / `content.derived` contract that #324–#326 adopt. The 0.13 → 0.19 bump (#324) was diffed wheel-to-wheel on every module watcher imports: `changes.py`, `streams.py`, `envelope.py` and `extract/__init__.py` change additively, `html.py` is #457's `default_factory` idiom only, `base.py` / `pdf.py` / `csv_excel.py` / `bus/exceptions.py` are byte-identical, and `co_core_aio/bus.py` adds `claim_stale_page` and widens `dead_letter`, neither of which watcher's consumers call differently; every Breaking entry 0.14.0–0.19.4 is on co/v1, transcript or `ext/` surfaces this repo does not import. Previously **v0.13.2** — floors `>=0.13.1,<0.14`: 0.13.1 adds `group_name`, the derived consumer-group naming the #285 rename adopts, and 0.13.2 changes only `co_v1` adapters; 0.10.0 widens `WatchStatusEmit.health` with `"unknown"` (cannobserv#328), the value the #264 status producer emits for a pre-first-check item; 0.9.3 carried `RegistryAnnouncementState` / `WatchStatusState` and the `INFO_REGISTRY` / `INFO_WATCH_STATUS` streams the registry consumer reads, cannobserv#324 and #254; 0.8.1 already carried the `spec_fingerprint` derivation the revisions producer imports unconditionally, cannobserv#309; `co-core-aio` carries the **`bus`** extra for the bus publishers (first the fetch-policy producer, #245, removed in #374) and the tail reader). The 0.10 → 0.13 bump (#285) is **additive on every surface Watcher touches**, established by diffing the wheels rather than by reading release notes: across `co_core` / `co_core_aio` 0.10.0 → 0.13.2 the only module Watcher imports that changed at all is `pure/adapters/bus/streams.py`, and it changed additively (`StreamKind`, `stream_kind`, `group_name`; every stream constant byte-identical). `pure/models/changes.py`, `pure/adapters/bus/envelope.py`, `pure/adapters/bus/exceptions.py`, `pure/extract/*`, `pure/util/hashing.py` and **all of `co_core_aio/bus.py`** are byte-identical; everything else that moved is `pure/adapters/co_v1/*` and `pure/models/task_performer.py`, which this repo does not import. Re-run that diff on the next multi-minor bump — a floor spanning three MINORs is not covered by any one release's notes. The 0.9 bump is **additive on every surface Watcher touches** — the `content.fetch` / `content.blobs` / `content.revisions` paths are untouched, and the `ChangeEventPayload` union simply widens; the MINOR is driven by `WordPressConfig` / `relationship.py` / `cdio` changes this repo has no exposure to. The 0.8 bump (cannobserv#300, adopted in #252) is **breaking in both directions**: `info_source_id` is required on all three content contracts and `BlobAvailableEvent.command_id` stopped being optional, so a 0.7.7 fact no longer decodes here — the deploy-ordering rule that follows is in [docs/CONTENT-PIPELINE.md](../docs/CONTENT-PIPELINE.md) → "`info_source_id` on the wire". `co-core` carries no extra: the `extract` parsers left with local extraction (#350). The content-acquisition pipeline (fetch → extract → fingerprint) is co-core's, run by Replicator and the processor; watcher no longer fetches at all (#241 step 5) — `src/core/fetch.py` is gone; the `watcher/0.1.0` User-Agent now lives in `src/core/fetch_commands.py` beside its only consumer and rides out on every `content.fetch` command's headers to preserve fingerprint byte-continuity.

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
- **The token reaches whatever the wrapped `uv` spawns:** `git`, and the build backends for `notifier-client` and the editable project. CI's `uv sync` step is the same. The bound is `co-pypi-reader`'s read-only role and the token's hour.
- **Dependabot's version lookup asks PyPI before the index,** where the co-core names are unclaimed. Who registers them is CannObserv/replicator#169's open decision. Part 2 of #358 ignores both names.
- **Rollback:** reverting #358 restores `find-links`, and the wheelhouse needs refilling before a restart. The deploy checkout keeps its `.wheelhouse/` files until part 1 has been through a restart, and `.gitignore` keeps them out of `git status` until they go.

## notifier-client

**notifier-client (#284).** The notifier SDK is a **pinned git-tag source** in `[tool.uv.sources]` — `https://github.com/CannObserv/notifier.git`, `subdirectory = "clients/python"`, `tag = "vX.Y.Z"`. The repo is public, so it needs no credential: no SSH key, no host alias, no CI URL rewrite (the `ssh://git@github-notifier/` alias it replaced existed only on the retired shared VM). **The tag is the pin** — uv enforces no version floor against a git source, so `"notifier-client"` stays bare in `[project.dependencies]`; a floor there would document intent and enforce nothing. **Upgrade:** change the tag, `bash scripts/co-pypi.sh uv sync` (refreshes `uv.lock`, freezing the commit), read notifier's [`clients/python/CHANGELOG.md`](https://github.com/CannObserv/notifier/blob/main/clients/python/CHANGELOG.md). A `Removed` dependency can no longer take a module this repo imports: `tests/test_dependency_imports.py` requires every third-party import declared here, bar an allowlist that names each exception's provider — `croniter`, via procrastinate (#294). **To stay put, do nothing.** `tests/test_dependency_sources.py` fails a git source that is not HTTPS (the scheme only — it cannot tell a public repo from a private one) or not pinned by `tag`, and any workflow under `.github/workflows/` that rewrites a git URL (`insteadOf` in any capitalisation — git config keys are case-insensitive). Currently pinned: **v0.3.1** — public API unchanged from 0.2.1; it dropped `python-dateutil` from the SDK's dependencies, which nothing here imports (it still resolves via `procrastinate` and `co-core`'s `dateparser`). Release procedure and the trigger for graduating to a published package: notifier's [`docs/RELEASING.md`](https://github.com/CannObserv/notifier/blob/main/docs/RELEASING.md).

## certifi floor

**certifi (#391).** httpx and requests trust certifi's bundle **instead of** the OS store, so an OS-level distrust (apt, exe.dev's remediation) never reaches the GCS calls (backup, restore, blobs) or the operator probe. certifi is transitive — under httpx and `google-cloud-storage` → requests — and nothing imports it, so its floor is `[tool.uv] constraint-dependencies = ["certifi>=2026.10.19"]`, not a dependency line; it resolves from PyPI. 2026.10.19 drops **emSign ECC Root CA - G3**, the root the leaked eMudhra intermediates chain to (GHSA-6q48-6c7w-9gw2); it deliberately keeps emSign G1, C1 and C3, so check for G3, not for "no emSign". `tests/test_dependency_certifi_floor.py` fails if the constraint is dropped or admits an older release, if the installed certifi is below it, or if its bundle still carries G3. **Raise the floor** the same way for a future distrust: bump the constraint and the test's `_FLOOR`, `bash scripts/co-pypi.sh uv lock`, then `… uv sync`, then `sudo systemctl restart watcher` — the running process keeps the bundle it loaded until it restarts; `watcher-backup.service` and a restore are fresh processes and pick it up on their own. The alternative, `truststore` (defer to the OS store), touches every client construction site including co-core's and the GCS client's, and was not taken.

## Advisory floors

**Lock-only advisory fixes (#392).** A runtime advisory fixed by moving the lock alone (`uv lock --upgrade-package <pkg>`) keeps its floor in `uv.lock`, not in `pyproject.toml`: no declared bound moves, and `tests/test_dependency_advisory_floors.py` fails if the *installed* release of any row drops below the one that fixed its advisories. It never asks OSV — `bash scripts/audit_lock.sh` does that (`uv audit --frozen`, runtime and dev apart), and needs the network. **Fix the next one** the same way: `bash scripts/co-pypi.sh uv lock --upgrade-package <pkg>` (one package per commit, so a bisect lands on it), add its row, `… uv sync`, then `sudo systemctl restart watcher` after the merge. A transitive that needs a floor no lock bump can hold takes a constraint, as certifi does above. **Ordering trap:** anyio ≥ 4.15 deprecates an alias starlette < 1.7 imports in `TestClient`, and the suite's `error::DeprecationWarning` filter makes that a collection error — move starlette first.

**The dev group too (#400).** The host's patch run audits the whole lock (`uv audit --frozen`, both groups: #382's `pending.language`), and the service venv is synced with `dev`, so a dev advisory is fixed the same way and gets a row. A dev package's upgrade may need a specifier to stay in its major: `-P 'soupsieve>=2.9.0,<3'`, since bare `-P soupsieve` takes 3.x. The specifier binds that one run, never the lock. **Removing** a dependency needs no extra host step: the unit's `uv run` is an inexact sync and never uninstalls, but its start step is an exact offline sync (#358), so it removes a dropped package.

**Finding the next one (#397).** `.github/workflows/audit.yml` runs `scripts/audit_lock.sh` weekly, on a push or PR to `main` that touches `uv.lock`, `pyproject.toml` or the audit itself, and on `workflow_dispatch`. It is **report-only**: the job is always green, and a finding is a warning annotation (`uv.lock runtime advisories` / `uv.lock dev advisories`) plus the run's job summary. An `… audit did not run` warning means OSV or uv failed, never a clean lock. No allowlist yet: the first advisory accepted rather than fixed adds one, each ID with a reason and a review-by date (uv's `--ignore <ID>`). Pinned by `tests/scripts/test_audit_lock.py` and `tests/ci/test_audit_workflow.py`.

## Dependabot PRs (#283)

`.github/dependabot.yml` proposes **action bumps only** (`github-actions`, weekly,
one PR per action); `tests/ci/test_dependabot.py` pins it, because a broken
config goes quiet rather than red. `uv` is out until #358 part 2: part 1 put
co-core on the co-pypi index, which the updater reads over OIDC, and part 2
ignores co-core's pins, bumped when Processor coordinates a contract change. Every
`uses:` is `@<sha> # vX.Y.Z` (#360); the same test fails any other form.

**A Dependabot run reads its own credential context.** Its `GITHUB_TOKEN` is
read-only and only *Dependabot* secrets reach it, so the four WIF jobs depend on
`vars.GCP_WIF_PROVIDER` resolving and on GitHub issuing the OIDC token there;
`css` needs neither. Both held on the first batch (#354–#357, 2026-10-09: every
job green, WIF auth and wheelhouse sync included, actor `dependabot[bot]`). On a red PR read the **Authenticate to Google Cloud** step
first, and match its error. *"must specify exactly one of
workload_identity_provider or credentials_json"* — the variable did not reach the
run. *"did not inject $ACTIONS_ID_TOKEN_REQUEST_TOKEN"* — no OIDC token was
granted. Past both, a token-exchange failure — the `github-ci` provider's
attribute condition or the SA binding ([COMMANDS.md](COMMANDS.md) → *CI*). None is the bump's fault.

**How one lands — locally, like everything else.** Dependabot only opens PRs;
watcher merges to `main` on the VM, behind the ship gate:

```bash
# Read the action's release notes, not just the diff: a major bump can change inputs.
gh pr comment <N> --body "@dependabot rebase"  # only if main moved; wait for its force-push
gh pr checks <N> --watch                       # every job green, on the commit that lands
git fetch origin "$(gh pr view <N> --json headRefName -q .headRefName)"
git merge --ff-only FETCH_HEAD
bash scripts/pre-ship.sh
git push origin main                           # GitHub marks the PR merged
```

Fast-forward only: the commit CI tested is the commit that lands, and its subject
already takes the repo's form (`chore: bump …`, the `commit-message` prefix). One
PR at a time — after each push the next may need `@dependabot rebase`. **Restart**
(`sudo systemctl restart watcher`) only when the diff touches something outside
`.github/`; an action bump never does.

**`context-cadence.yml` is generated** (`install-cadence.sh`, vendored in
`skills-vendor/`), and Dependabot bumps its `actions/checkout` with `ci.yml`'s.
Take the bump; a later re-run of the generator reverts it to the generator's
own `@v5` (gregoryfoster/skills#373), which the pin test fails — so after a
regeneration, restore its `uses:` line before committing.
