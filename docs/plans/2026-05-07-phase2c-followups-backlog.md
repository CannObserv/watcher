---
title: Phase 2c follow-ups + Phase 5 Apprise strip — backlog orchestration
date: 2026-05-07
status: in-progress
issues: [137, 143, 144, 150]
deferred: [140]
---

# Phase 2c follow-ups + Phase 5 Apprise strip — backlog orchestration

## Goal

Clear five queued issues — three Phase 2c follow-ups (#143 consumer hardening, #144 sub-minute drain cadence, #150 test fixture optimization), the Phase 5 local-Apprise strip (#137), and inline InfoItem creation (#140) — using a parallel-safe batch plan that minimizes merge churn while keeping each diff individually reviewable.

## Approved approach

Two execution batches plus one deferred issue:

- **Batch A (3 parallel agents):** #143, #144, #150 — fully disjoint file coverage in `tools/`, `src/workers/`, and `tests/`. Lands first.
- **Batch B (1 agent):** #137 — high-blast Apprise strip whose effective scope spans core notifications, crypto, dependencies, alembic migration, dashboard apprise paths, partials, and tests. Single-agent avoids intra-batch coordination.
- **Deferred:** #140 (inline InfoItem creation) — removed from this round.

Worktrees for branch isolation. Hybrid parallelism: parallel within Batch A, gate to Batch B. Regular merge commit when each batch lands on `main`.

## Prioritization rubrics

Standard rubric: **Score = (Foundation Leverage × 2) + (Correctness Risk × 2) + Scope Clarity** — max 15.

| Dimension | 1 | 2 | 3 |
|---|---|---|---|
| Foundation Leverage | Standalone improvement | 1–2 other issues benefit | Multiple issues depend on or are simplified by this |
| Correctness Risk | Cosmetic / organizational | Edge-case incorrect behavior, runtime failure risk | Data loss, race conditions, silent failures |
| Scope Clarity | Requires design discovery | Clear direction, minor decisions | Mechanical — implementation obvious from issue |

Blast radius (files touched across issues) drives sequencing, not score.

## Scored backlog

| # | Title | F | C | S | Score | Blast |
|---|---|---|---|---|---|---|
| #143 | Operational hardening of `info_changes_consumer.py` | 1 | 3 | 2 | **10** | Low |
| #137 | Phase 5 — strip local Apprise dispatch | 1 | 2 | 3 | **9** | High |
| #140 | Inline InfoItem creation from Watch form | 1 | 2 | 2 | **8** | Med |
| #150 | Optimize archiver alembic test-fixture | 1 | 1 | 2 | **6** | Low |
| #144 | Sub-minute drain cadence | 1 | 1 | 1 | **5** | Med |

**Per-issue rationale:**

- **#143 (10)** — Correctness=3: poison messages currently block the consumer group; no DLQ, no retry, no graceful shutdown. Foundation=1: doesn't unblock other queued issues. Scope=2: clear sub-tasks but multiple judgment calls.
- **#137 (9)** — Scope=3: explicit checklist; mostly mechanical removal. Correctness=2: dead-code stripping is safe, the column-drop migration is destructive but has a preflight check. Foundation=1: removes ~537 LOC and two deps but doesn't unblock other queued issues.
- **#140 (8)** — Correctness=2: error/validation paths matter, no data-loss surface. Scope=2: design choices (modal vs disclosure, audit emission). Foundation=1.
- **#150 (6)** — Correctness=1: current approach works; xdist not in use. Scope=2: issue accepts either implementation or "close with explanation". Foundation=1.
- **#144 (5)** — Correctness=1: pure latency, current path is correct. Scope=1: requires design discovery (3 architectural options + cadence target unset). Foundation=1.

## Conflict zones

| File | Issues touching it | Notes |
|---|---|---|
| `src/dashboard/routes.py` | #137 (notification CRUD, ~lines 1458–2838), #140 (watch creation, ~lines 222–301) | Different sections; risk concentrated at the import block at top of file |
| `src/dashboard/templates/` | #137 (apprise partials → likely deletes; notification pages → edits), #140 (`watch_form.html` → addition) | Different files, same directory |

#137's effective scope is broader than the issue body. The body lists `src/core/notifications/`, `crypto.py`, `pyproject.toml`, alembic migration, and core+worker tests — but dropping the `apprise_url` columns and removing `encrypt_apprise_url`/`decrypt_apprise_url` forces dashboard cleanup too: `src/dashboard/routes.py:28` imports those crypto helpers, and 20+ dashboard route call-sites read/write `apprise_url`. The agent picking up #137 must also delete:

- `src/dashboard/templates/partials/apprise_plugin_form.html`
- `src/dashboard/templates/partials/apprise_raw_url_form.html`
- Apprise paths in notification pages (`watch_notification_*.html`, `domain_notification_*.html`, `notification_*.html`)
- `src/core/notifications/apprise_builder.py`
- Dashboard apprise tests (`test_apprise_plugin_form.py` and apprise paths in form-migration tests)

Required merge order on `src/dashboard/routes.py`: #137 first, then #140 if/when it returns. (#140 is deferred from this round, so this is a future-tense note.)

No other contested files. #143, #144, #150 each occupy distinct directory subtrees.

## Dependency graph

```
Batch A (parallel, isolated):    #143  #144  #150
                                   │     │     │
                                   └─────┴─────┴────► merge to main

Batch B (after A):                 #137  ◄── high-blast core+dashboard cleanup
                                     │
                                     └────► merge to main

Deferred:                          #140  ◄── revisit after #137 stabilizes
```

## Batch execution plan

### Batch A — 3 parallel agents

Branch: `batch/a-phase2c-followups` (orchestrator-owned, holds the merged work of all three workers).

| Agent | Issue | Worker branch | Files | Notes |
|---|---|---|---|---|
| A1 | #143 | `feature/batch-a-143-consumer-hardening` | `tools/info_changes_consumer.py`, `tests/tools/test_info_changes_consumer.py` | Add retry/backoff on Redis reconnect, DLQ pattern (`info.changes.dead`), structured logging via `get_logger`, per-message processing timeout, metrics (messages_consumed/dlq/last_lag), graceful SIGTERM drain. Decision: stay in `tools/` (issue default). |
| A2 | #144 | `feature/batch-a-144-drain-cadence` | `src/workers/changes_drain.py` (+ possibly `src/api/main.py` lifespan, possibly new `deploy/watcher-drain.service`) | Default: option 1 (asyncio loop in watcher process gated by `pg_try_advisory_xact_lock`). Cadence target: 10 s. Agent may justify options 2/3 in PR description if implementation surfaces a strong reason. |
| A3 | #150 | `feature/batch-a-150-test-fixture` | `tests/conftest.py`, `AGENTS.md` (one-line note) | Implement cache-check optimization (skip alembic invocation when `information.alembic_version` already matches HEAD). Add brief AGENTS.md note that pytest-xdist is unsupported pending fixture rework. |

Gate: start immediately. Workers signal completion to the orchestrator; orchestrator verifies merge into `batch/a-phase2c-followups`, runs full test suite when all three are merged, notifies user for review.

### Batch B — 1 agent

Branch: `feat/137-phase5-apprise-strip` (single-agent batch — feature branch *is* the batch branch).

| Agent | Issue | Branch | Scope |
|---|---|---|---|
| B1 | #137 | `feat/137-phase5-apprise-strip` | **Per issue body**: simplify `dispatch_event_notifications`, delete `dispatcher.py`, drop `DispatchCandidate.apprise_url` field, remove `apprise` and `cryptography` from `pyproject.toml`, strip `encrypt_apprise_url`/`decrypt_apprise_url` from `crypto.py`, alembic migration to drop `apprise_url` columns from `notification_templates` and `watch_notification_configs`, delete `scripts/migrate_channels_to_notifier.py`, remove local-dispatch-path tests in `tests/core/notifications/` and `tests/workers/test_notify.py`. **Implicit per conflict analysis**: dashboard apprise route paths in `src/dashboard/routes.py`, deletion of `templates/partials/apprise_*.html`, edits to notification page templates to drop apprise URL inputs, deletion of `src/core/notifications/apprise_builder.py`, deletion/cleanup of dashboard apprise tests. **Prereq verification at agent start**: run `SELECT count(*) FROM watch_notification_configs WHERE remote_channel_id IS NULL AND is_active`; abort if rows > 0. |

Gate: after Batch A is merged to `main`. Single-agent → no separate batch branch needed.

## Key decisions

1. **#137 alone in Batch B, not parallel with anything else.** Its effective scope (core notifications + crypto + dashboard cleanup + migration) is too wide for safe parallelism. Splitting into multiple agents would create more merge friction than it saves.
2. **#140 deferred.** Removed from this round per user direction. The dashboard `routes.py` conflict with #137 made it the obvious sequence-dependent issue; removing it eliminates a downstream batch.
3. **#144 default to option 1 (asyncio loop).** Issue lists three architectural options without preference. Pre-production posture supports the simpler choice (no new systemd unit, no Postgres NOTIFY/LISTEN lifecycle complexity). Agent must justify in PR description if it deviates.
4. **#143 stays in `tools/`.** Issue lists this as default; no reason yet to graduate to a systemd unit.
5. **#137 prereq check enforced at agent start, not assumed.** The ≥1-week soak window is a human responsibility before merging Batch B; the SQL check (`remote_channel_id NULL` rows) is a hard gate the agent runs before destructive work.

## Deferred items

- **#140 — Inline InfoItem creation** — removed from this round per user direction. Re-score and re-batch in a later orchestration cycle.

## Out of scope

- **Notifier soak window verification** — human responsibility before merging Batch B (review notifier service logs for >1 week of stable Phase 4 operation).
- **Inline InfoSpec creation UI** — explicitly out of scope per #140 issue body; only InfoItem inline creation was ever in scope.
- **`info_changes_consumer.py` graduating to a systemd unit** — defer to Archive service Phase 3 when the Archive's reference consumer supersedes the tools/ script.

## Branch hygiene rules (orchestrator)

Per skill rules 1–4:

1. Sync local main before every batch launch (`git checkout main && git pull --ff-only`). Worktree agents branch from local main; stale local main = wrong commit base.
2. Never `git push origin HEAD:main` from a feature branch. Always push from local main after merge.
3. `isolation: "worktree"` merges to the orchestrator's *current* local branch. Check out `batch/a-phase2c-followups` before spawning workers so their output accumulates there, not on `main`.
4. After any rebase conflict, fix the auto-generated commit message immediately with `git commit --amend` before continuing.

## Agent role summary

- **Orchestrator** (this Claude session): syncs main, creates batch branch, launches workers, verifies merges, runs full test suite per batch, notifies user for review, merges batch branch to main on confirmation. Does not write implementation code.
- **Workers** (subagents via `Agent` with `isolation: "worktree"`): TDD red→green→refactor, run full test suite, run linter, self-review diff, address findings, signal completion. No PRs opened by workers.

Merge strategy: regular merge commit (preserves per-agent commit history under one merge commit per batch).
