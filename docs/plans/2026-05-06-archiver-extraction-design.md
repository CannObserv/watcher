---
title: Archiver Service Extraction (Information service → /home/exedev/archiver)
date: 2026-05-06
status: approved (design)
---

# Archiver Service Extraction

## Goal

Extract the in-tree Information service (`src/information/`) from the Watcher repo into its own standalone repo at `/home/exedev/archiver`, renamed to **Archiver**. Follow the Notifier extraction pattern (#132): separate repo on the same VM, separate systemd unit, generated Python SDK, watcher consumes the SDK as a path dependency. Archiver becomes the seed of a broader information-domain service that — per `docs/research/2026-05-06-archiver-information-model.md` — will eventually replace WordPress as source-of-truth for Information Items, introduce a content-addressed `InfoSource` + `SourceRevision` model, and integrate with a future Replicator service. The extraction itself stays a rename + lift; model evolution lands separately afterward.

## Background

Phase 1–3a stood the Information service up inside the Watcher repo as a sibling FastAPI app on port 8020, with its own alembic root (`alembic_information/`), its own systemd unit (`information.service`), and a generated Python SDK at `clients/python/`. That arrangement was always intended as a prototype-in-place: the original design doc (`2026-05-03-information-source-specifications-design.md`) explicitly planned for "Information service extracts to a sibling repo and relocates off-VM" in its "Later" phase.

Several signals make now the right time to extract:

- The codebase is starting to feel like two repos in one — separate alembic root, separate FastAPI app, separate systemd unit, separate SDK, but shared `src/core/` and a single `pyproject.toml`. Continued co-habitation invites accidental coupling.
- Replicator is on the horizon (per the research doc) and will need the same SDK + the same content-acquisition primitives as Watcher. Establishing the extraction pattern before Replicator stands up keeps the boundary clean.
- The "Information" name has always been awkward (singular vs. plural ambiguity, called out in the original design doc). "Archiver" generalizes better as the service grows to own InfoSources, SourceRevisions, and Replicator-orchestration state.

## Vocabulary

This extraction is a **service-name-only rename**. The data model vocabulary stays untouched.

| Layer | Before | After |
|---|---|---|
| Service name (prose, repo, systemd unit, port-8020 binding) | Information service | Archiver service |
| SDK package name | `information_client` | `archiver_client` |
| Repo path on VM | `src/information/` (in watcher) | `/home/exedev/archiver/` |
| Code-level identifiers | `InfoItem`, `info_item_id`, `InfoSpec`, `info_spec_id` | unchanged |
| HTTP routes | `/api/v1/info-items/`, `/api/v1/info-specs/`, `/api/v1/tools/*` | unchanged |
| Database table names | `info_items`, `info_specs` | unchanged |
| Redis stream topics | `info.changes`, `info.spec_changes`, `info.notifications` | unchanged |
| Watcher columns | `watches.info_item_id`, `changes.info_item_id`, `changes.info_spec_id` | unchanged |

Future evolution to `InfoSource` / `SourceSpec` / `SourceRevision` lands as a deliberate v2 in the new repo, decoupled from this rename.

## Key decisions

### 1. Pure rename + extract (Q1)

Archiver = today's Information service, lifted to a new repo and renamed. Storage of payloads (the previously-planned port-8030 "Archive" service) is now scoped to a separate future **Replicator** service. The new repo at `/home/exedev/archiver` does **not** absorb storage responsibilities in this work.

### 2. Service-name-only rename, no cascade into data model (Q2)

Per the table above. The original Phase 3 design's deliberate vocabulary (`InfoItem`, `InfoSpec`, `info_item_id`) is preserved verbatim. Watcher's runtime barely changes — only the SDK import name flips. HTTP routes, DB columns, stream topics, all stable.

### 3. Shared content-acquisition code: notifier-style mirror (Q3/Q4)

Both repos own full copies of the content-acquisition stack:

- `src/core/fetchers/{base,http}.py`
- `src/core/extractors/{base,html,csv_excel,pdf}.py`
- `src/core/simhash.py`
- `src/core/extraction_defaults.py` (new — see decision 7)
- `src/core/logging.py`

Discipline: when one repo changes any of these files, the change is mirrored to the other. Drift is acceptable for this initial extraction; fingerprint-parity concerns become acute only when Replicator joins the consumer set, at which point we'll revisit (likely toward an SDK-resident reference implementation, the "Option 4" alternative discussed during design).

**Why not a shared package or SDK-resident extractor today:** with only two consumers (Watcher + Archiver authoring tools) and no production data, the operational cost of a third package outweighs the drift risk. The sharing strategy is a **deferred** decision, not a permanent one.

### 4. Fetcher service deferred (Q3 follow-up)

A dedicated egress microservice ("Fetcher") that centralizes outbound HTTP, rate limiting, robots.txt, and Playwright pooling is a legitimate future option but **not in this scope**. Each service owns a thin local `Fetcher` boundary. When/if a Fetcher service ships, the swap is mechanical and per-service.

### 5. Separate Postgres database (Q5)

Archiver owns its own Postgres database (`archiver` / `archiver_test`) on the existing Postgres instance. Watcher loses direct DB access to `info_items` / `info_specs` rows; the SDK is the only path. Cost: a one-time fixture re-author (no production data, per the original design's pre-production assumption). Benefit: data-residency clarity, easier off-VM relocation later.

### 6. Big-bang migration (Q6)

Single coordinated cutover. Pre-production state means the cost of "all things change at once" is low and the cost of running a phased dual-stack window is needless ceremony.

### 7. Relocate `extraction_config` (required cleanup)

Currently at `src/information/core/tools/extraction_config.py`, imported by `src/workers/pipeline.py` — a reverse coupling where watcher depends on a service-internal module for InfoSpec consumer defaults. After extraction the import breaks; before extraction it's already in the wrong place architecturally.

Resolution:
- **Watcher:** new module `src/core/extraction_defaults.py` carrying the same constants. `src/workers/pipeline.py` imports from it.
- **Archiver:** keeps a peer copy at `src/core/extraction_defaults.py` for use within authoring tools (`preview_extraction`, etc.). One of the mirrored files per decision 3.
- The original location at `src/information/core/tools/extraction_config.py` ceases to exist after extraction.

This is the first commit of the migration, before the extraction itself.

### 8. SDK rename: `information_client` → `archiver_client`

Generalizes naturally for future Source/Revision/ReplicationSpec types. Keeps `InfoItem`, `InfoSpec` exports (per decision 2). Watcher swaps imports in a single mechanical pass. Path-dependency continues to point at `/home/exedev/archiver/clients/python/` after the move.

### 9. Repo skeleton in `/home/exedev/archiver/`

```
archiver/
├── AGENTS.md                                     # forked from watcher's, scoped to archiver
├── CLAUDE.md → AGENTS.md                         # symlink (matches watcher pattern)
├── README.md
├── LICENSE
├── pyproject.toml                                # archiver — own deps
├── uv.lock
├── alembic.ini                                   # promoted from alembic_information.ini
├── alembic/                                      # promoted from alembic_information/
├── deploy/
│   └── archiver.service                          # renamed from information.service
├── docs/
│   ├── COMMANDS.md
│   ├── DEPLOYMENT.md
│   ├── SKILLS.md
│   ├── plans/
│   └── research/
│       └── 2026-05-06-archiver-information-model.md   # carried forward
├── scripts/
│   ├── dump_openapi.py
│   └── smoke_phase3a.sh
├── src/
│   ├── api/                                      # ex src/information/api/
│   │   ├── main.py
│   │   ├── deps.py
│   │   ├── serializers.py
│   │   ├── routes/
│   │   └── schemas/
│   └── core/
│       ├── models/                               # info_item.py, info_spec.py
│       ├── tools/                                # without extraction_config (decision 7)
│       ├── info_spec_schema/
│       ├── extraction_defaults.py                # mirrored
│       ├── fetchers/                             # mirrored
│       ├── extractors/                           # mirrored
│       ├── simhash.py                            # mirrored
│       ├── database.py
│       └── logging.py
├── clients/python/                               # archiver_client SDK
│   ├── pyproject.toml
│   ├── README.md
│   ├── scripts/regen.sh
│   └── src/archiver_client/
│       ├── generated/
│       └── (hand-written wrappers)
├── tests/                                        # ex tests/information/, restructured
├── skills/                                       # forked
├── skills-vendor/                                # forked
└── .claude/
```

Notes:
- No speculative directories for unborn `info_sources/` or `source_revisions/` modules. The flat `core/models/` shape can absorb them when they land.
- `alembic_information/` flattens to `alembic/` since archiver owns its own alembic root.
- `docs/research/2026-05-06-archiver-information-model.md` moves with the new repo so the trajectory stays accessible.

### 10. Watcher's clean-up after the move

Files/directories deleted from watcher:
- `src/information/`
- `clients/python/` (moves to archiver)
- `alembic_information/`, `alembic_information.ini`
- `deploy/information.service`
- `tests/information/` (or wherever Information service tests live)
- `scripts/smoke_phase3a.sh`
- `docs/plans/2026-05-03-information-service-phase1-plan.md`, `2026-05-05-information-service-phase3a-plan.md` (move to archiver/docs/plans/)
- `docs/research/2026-05-06-archiver-information-model.md` (moves to archiver)

Files modified in watcher:
- `pyproject.toml` — drop information-service deps; flip `information_client` path-dep to `archiver_client` pointing at `/home/exedev/archiver/clients/python/`.
- `src/workers/pipeline.py` — import `extraction_defaults` from new local location.
- All `from information_client …` → `from archiver_client …` (mechanical sed pass + manual review).
- `AGENTS.md` — drop "Information service authoring tools (Phase 3a)" section + Information port-8020 references; update the "Notifier extraction" callout to also list Archiver mirror domains.
- Memory file at `/home/exedev/.claude/projects/-home-exedev-watcher/memory/` — add a `project_archiver_extraction.md` (peer to `project_notifier_extraction.md`).

### 11. Operational outputs

- New systemd unit on the VM: `archiver.service` listening on port 8020 (same port the old `information.service` used). Old unit retired.
- New Postgres databases on the existing instance: `archiver`, `archiver_test`. Old `info_items` / `info_specs` tables in the watcher database are dropped (pre-production, no data preservation).
- New env file: `/etc/archiver/.env` for production secrets (`DATABASE_URL`, `ARCHIVER_API_KEY` — analogous to Notifier's pattern).
- Watcher's `INFORMATION_BASE_URL` env var renamed to `ARCHIVER_BASE_URL`. `INFORMATION_API_KEY` → `ARCHIVER_API_KEY`. Both `/etc/watcher/.env` and `.env` updated; old vars dropped.

## Sequencing

The migration is a single coordinated pass, but the steps within it have a natural order:

1. **Pre-extraction cleanup in watcher (one feature branch, multiple commits)**
   - Relocate `extraction_config` per decision 7. Update `src/workers/pipeline.py`. Tests green.
   - Confirm no other reverse imports remain (`grep -rn "from src.information" src/core src/api src/workers src/dashboard`).

2. **Initialize `/home/exedev/archiver` as a fresh git repo**
   - `git init` at `/home/exedev/archiver`. No relation to watcher's history.
   - First commit: lift `src/information/` → `src/`, `alembic_information/` → `alembic/`, `clients/python/` → `clients/python/`, deploy unit + scripts. Mirror the shared `src/core/` files. Rewrite imports.
   - Second commit: rename to `archiver` everywhere (package name, SDK module name, systemd unit, env-var prefix, env-file path). Update README/AGENTS.md.
   - Third commit: pyproject + uv.lock, smoke `uv sync` + `uv run pytest`.
   - Fourth commit: `dump_openapi.py` + `clients/python/scripts/regen.sh` adjusted, regenerate SDK, smoke against a local archiver dev server.

3. **Watcher cutover**
   - Same feature branch as step 1.
   - Delete `src/information/`, `clients/python/`, `alembic_information*`, `deploy/information.service`, etc.
   - Repoint `archiver_client` path dep in `pyproject.toml` to `/home/exedev/archiver/clients/python/`.
   - Mechanical `information_client` → `archiver_client` import rewrite.
   - Env-var rename in `/etc/watcher/.env` and `.env`.
   - `uv sync`, full test run, smoke `tools/info_changes_consumer.py` and dashboard authoring flows against the new archiver dev server.

4. **VM cutover**
   - Stop `information.service`, install `archiver.service`, `systemctl daemon-reload`, `systemctl enable --now archiver.service`.
   - Drop watcher database's `info_items` / `info_specs` tables; create `archiver` / `archiver_test` databases; `alembic upgrade head` against `archiver`.
   - `systemctl restart watcher`.
   - End-to-end smoke: create an InfoItem via Archiver, create a Watch via Watcher referencing it, observe a Change drain.

5. **Documentation pass**
   - Update both repos' AGENTS.md to reflect the post-extraction reality.
   - Carry the research doc (`2026-05-06-archiver-information-model.md`) into archiver/docs/research/ as the trajectory anchor.
   - Save a memory record at `project_archiver_extraction.md` mirroring the Notifier extraction memory.

## Out of scope

- Introduction of `InfoSource`, `SourceSpec`, `SourceRevision` models (research doc territory; deferred to a v2 effort in the new repo).
- Replicator service stand-up (separate future work).
- Fetcher service stand-up (deferred indefinitely; see decision 4).
- Migration to SHA-256 fingerprinting (comes with the new model, not the rename).
- Any change to `info.changes` / `info.spec_changes` stream contracts.
- Any change to InfoSpec JSON Schema (`v1.json`).
- WordPress integration changes.
- SDK publication to a real index (continues as path dep on this VM).
- Cross-repo automation for mirroring shared content-acquisition files (manual discipline for now; revisit if drift becomes a real cost).
- Production data migration (none exists).

## Open questions / follow-ups

- When Replicator stands up, do we revisit the SDK-resident reference-impl pattern (Option 4 from design discussion) for fingerprint parity? Likely yes, when concrete pain shows up.
- Whether the watcher repo's `tools/info_changes_consumer.py` reference consumer eventually relocates to the archiver repo or stays in watcher as the canonical consumer example. Defer.
- Whether `INFORMATION_BASE_URL` deprecation gets a transitional period (read either env var with a warning) or is a hard cut. Default: hard cut, per "no production data."

## References

- #132 — Notifier extraction (parent architectural pattern)
- `docs/plans/2026-05-03-information-source-specifications-design.md` — original Information service design (whose "Later: extract" step this design implements)
- `docs/research/2026-05-06-archiver-information-model.md` — Archiver future-state research, carried into the new repo
- `/home/exedev/notifier/` — sibling extraction template
- AGENTS.md — project conventions
- Memory: `project_notifier_extraction.md`
