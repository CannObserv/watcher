---
title: "Phase 0 — adopt cannobserv (co-core + co-core-aio) via the private GCS index"
date: 2026-07-24
status: executed — CI green (pending deploy restart)
---

# #220 — adopt cannobserv via find-links (co-core + co-core-aio)

**Tracking:** [#220](https://github.com/CannObserv/watcher/issues/220) · mechanism
mirrors [archiver#75](https://github.com/CannObserv/archiver/issues/75) /
[archiver PR #102](https://github.com/CannObserv/archiver/pull/102) · subsumes #226.
**Precedent:** archiver shipped the identical adoption first
(`docs/plans/2026-07-23-72-phase-0-cannobserv-adoption.md`); watcher mirrors it.

## Problem

Phase 0 of the cluster-integration strategy wires watcher to the shared `cannobserv`
substrate and adopts one trivial pure util end-to-end, proving the toolchain before the
heavier phases (content-acquisition → co-core; the mirrored `fetchers/`/`extractors/`/
`simhash` de-dup) land. Distribution is settled: **find-links against the private GCS
index `gs://co-gcs-pypi`**, not git-tag `tool.uv.sources`. Watcher never pinned cannobserv
by any mechanism, so this is a clean first adoption.

## Decisions (this repo)

- **Package set:** `co-core` + `co-core-aio` (watcher is an async FastAPI service), **not**
  `cannobserv`/`co-core-sync` (those drag in google/trello/pikepdf). `co-core-aio` adds only
  `httpx` (already a direct dep).
- **Version:** `co-core>=0.3,<0.4` + `co-core-aio>=0.3,<0.4`; resolves to **v0.3.4** (latest),
  subsuming #226.
- **Validation util:** `co_core.pure.util.hashing.sha256`, asserted equal to
  `Chunk.content_hash` (`src/core/extractors/base` — `hashlib.sha256(text.encode()).hexdigest()`).
  On the Phase-1 fingerprint trajectory, so the smoke test doubles as the contract a future
  co-core swap must keep. `tests/core/test_cannobserv_smoke.py` (red → green under TDD).
- **CI stood up as part of this** (watcher had none) — see below.

## What changed

1. `scripts/sync_wheelhouse.py` — mirrors `gs://co-gcs-pypi/wheels/` → `./.wheelhouse`
   (atomic per-file, size-skip). Copied from archiver; env vars renamed `WATCHER_WHEELHOUSE_*`.
2. `pyproject.toml` — `co-core`/`co-core-aio` floors; `[tool.uv] find-links = ["./.wheelhouse"]`.
   No `[tool.uv.sources]` git entries for cannobserv.
3. `.gitignore` — ignore `.wheelhouse/`, **track `!.wheelhouse/.gitkeep`**. ⚠️ The gotcha:
   `find-links` makes *every* `uv` invocation require the dir, and `uv run` reads config
   *before* the sync creates it; the tracked `.gitkeep` makes the dir exist at checkout (bit us
   live during execution — the sync step itself couldn't run until the dir existed).
4. `uv.lock` — regenerated against the real GCS wheels (find-links locks by filename, not hash),
   committed.
5. `.github/workflows/ci.yml` — **new.** lint + test jobs; sibling archiver checkout; notifier
   SSH→HTTPS rewrite; keyless WIF auth; wheelhouse sync before `uv sync`. Test job syncs
   archiver's wheelhouse too (conftest's `uv run alembic` subprocess needs co-core).
6. `deploy/watcher.service` — non-fatal `ExecStartPre=-… sync_wheelhouse.py` before `ExecStart`
   so restarts self-heal. Unit reinstalled (`daemon-reload`, no restart) to keep the #233
   installed-unit parity test green.
7. Docs — AGENTS.md "Environment & Tooling" (wheelhouse) + a CI subsection.

## Verification

- Wheelhouse synced live via the VM's `co-pypi-reader` key (76 objects).
- `uv sync` resolves `co-core==0.3.4` / `co-core-aio==0.3.4` from `.wheelhouse`; `uv.lock` pins them.
- `tests/core/test_cannobserv_smoke.py` red (`No module named 'co_core'`) → green.
- Full suite **629 passed**; `ruff check` + `ruff format --check` clean.
- CI YAML validated; notifier `v0.2.1` reachable over HTTPS. GH Actions not run here (see risks).

## Operator prerequisites (agent cannot perform)

- **VM key — done:** `GOOGLE_APPLICATION_CREDENTIALS=/etc/watcher/co-pypi-reader.json` present in
  `/etc/watcher/.env`; bucket read verified.
- **WIF grant — done:** `attribute.repository/CannObserv/watcher` bound to `co-pypi-reader`
  (`roles/iam.workloadIdentityUser`) alongside archiver's; the org-scoped `github-ci` provider
  (`vars.GCP_WIF_PROVIDER`) needed no change. Command in AGENTS.md §CI. First green run:
  GH Actions run 30116832872 (628 passed, 1 skipped).
- **Deploy restart — pending:** next `sudo systemctl restart watcher` picks up the new unit;
  `uv run uvicorn` then resolves co-core from the (already-populated) wheelhouse, and the
  `ExecStartPre` keeps it fresh.

## Follow-ups / risks

- **Watcher migrations not independently smoke-tested in CI** — a bare `alembic upgrade head`
  needs the archiver-owned `information` schema seeded first (intermediate cross-schema FK in
  9e86f9e4d704), and the suite has always used `create_all`, not migrations. The standalone step
  was dropped; pytest is the schema signal. A dedicated migration-chain check (seed information
  schema → upgrade head) is a reasonable separate follow-up.
- **No cannobserv call sites yet** — Phase 0 adopts only a pure util; the `list_all` pagination
  caveat (#77) has nothing to audit until a paginated client is actually used.
- **Deploy restart pending** — next `sudo systemctl restart watcher` picks up the unit; `uv run
  uvicorn` resolves co-core from the populated wheelhouse, and `ExecStartPre` keeps it fresh.
