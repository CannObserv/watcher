# Architectural Foundation Backlog — Orchestration Plan
_2026-03-23_

## Goal

Clear the open issue backlog in priority order, establishing testability, correctness, and maintainability before building Phase 7 (advanced fetching). Work is parallelized where file-level blast radii are disjoint; git worktrees isolate each agent's branch.

## Approved Approach

Six sequential merge batches. Peak parallelism is 6 agents (Batch A). Batches B–E are single agents on the critical path through `tasks.py`. Phase 7 issues (#3, #4, #5) are explicitly deferred until Batch F is stable.

## Prioritization Rubrics

Three dimensions, each scored 1–3. **Score = (Foundation×2) + (Correctness×2) + Scope**, max 15.

| Dimension | 1 | 2 | 3 |
|---|---|---|---|
| **Foundation Leverage** | Standalone improvement | 1–2 other issues benefit | Multiple issues depend on this |
| **Correctness Risk** | Cosmetic / organizational | Edge-case incorrect behavior or runtime KeyError risk | Data loss / race condition / silent failure |
| **Scope Clarity** | Requires design discovery | Clear direction, minor decisions | Mechanical — implementation is obvious |

**Blast radius** (files touched) drives *sequencing within a batch*, not priority score.

## Scored Backlog

| # | Title | Found. | Correct. | Clarity | Score | Blast |
|---|---|---|---|---|---|---|
| #16 | Extract event type constants | 3 | 2 | 3 | 13 | High |
| #18 | Centralize audit log helper | 2 | 2 | 3 | 11 | High |
| #20 | Pydantic configs for channels/extractors | 2 | 2 | 3 | 11 | Med |
| #25 | Savepoint boundary in check pipeline | 1 | 3 | 3 | 11 | Low |
| #14 | Decompose tasks.py monolith | 3 | 1 | 2 | 10 | High |
| #19 | Unify DI patterns | 3 | 1 | 2 | 10 | High |
| #21 | Decouple dashboard from worker internals | 2 | 1 | 3 | 9 | Med |
| #22 | Shared test data factories | 2 | 1 | 3 | 9 | Low |
| #9  | Per-watch ignore patterns | 1 | 2 | 2 | 8 | Med |
| #10 | Configurable change threshold | 1 | 2 | 2 | 8 | Med |
| #24 | Async Extractor protocol | 2 | 1 | 2 | 8 | Med |
| #13 | Move ChangeDetailResponse to schemas | 1 | 1 | 3 | 7 | Low |
| #17 | Change metadata summary helper | 1 | 1 | 3 | 7 | Low |
| #26 | Health check endpoint | 1 | 1 | 3 | 7 | Low |
| #27 | 404 error template | 1 | 1 | 3 | 7 | Low |
| #28 | Delete watch dashboard action | 1 | 1 | 3 | 7 | Low |
| #12 | Differentiate archive from deactivate | 1 | 1 | 2 | 6 | Med |
| #3  | Playwright fetcher | — | — | — | deferred | — |
| #4  | WebRecorder fetcher | — | — | — | deferred | — |
| #5  | Adaptive fetcher escalation | — | — | — | deferred | — |

> #11 (domains persistence) presumed closed by #30 — verify before execution begins.

## Conflict Zones

Three files are touched by multiple issues and drive sequencing:

- **`src/workers/tasks.py`** — #25, #16, #18, #21, #19, #14 (in merge order)
- **`src/dashboard/routes.py`** — #27+#28, #16, #18, #21 (in merge order)
- **`src/core/extractors/html.py`** — #24, #20 (protocol before configs)

## Dependency Graph

```
#16 (constants) ──► #18 (helper)
                         │
#25 (savepoint) ─────────┤
#27+#28 ─────────────────┤
                         ▼
                    #21 (decouple rate_limiter)
                         │
                         ▼
                    #19 (DI unification)
                         │
                         ▼
                    #14 (decompose tasks.py)
                         │
                    ┌────┴────┐
                    ▼         ▼
               #9+#10       #12
           (differ+thresh) (archive)

#24 ──► #20  (independent track)
#13, #17, #22, #26  (fully isolated)
```

## Batch Execution Plan

### Batch A — Maximum Parallelism (6 agents, start immediately)

All agents touch disjoint files. PRs may be merged in any order.

| Agent | Issues | Primary files |
|---|---|---|
| A1 | #13 — Move ChangeDetailResponse to schemas | `api/schemas/change.py`, `api/routes/changes.py` |
| A2 | #17 — Change metadata summary helper | `dashboard/context.py` |
| A3 | #22 — Shared test data factories | `tests/conftest.py` |
| A4 | #26 — Health check endpoint | `api/main.py`, `tests/api/test_health.py` |
| A5 | #27 + #28 — 404 template + delete watch UI | `dashboard/routes.py`, `dashboard/templates/` |
| A6 | #24 → #20 — Async extractor protocol, then Pydantic configs | `core/extractors/`, `core/notifications/`, `api/routes/notification_configs.py` |

### Batch B — Wide Mechanical Refactor (1 agent, 3 sequential commits)
**Gate: all Batch A PRs merged**

Single branch, single PR, three commits in order:
1. **#25** — Savepoint boundary: commit pipeline results before notification dispatch
2. **#16** — Define `EventType` constants in `audit_log.py`; replace ~15 string literals
3. **#18** — Add `audit()` helper; replace all `AuditLog()` instantiation patterns

Rationale: #25 fixes a correctness bug on the current file before mechanical refactors begin. #16 must precede #18 so the helper can reference constants.

### Batch C — Rate Limiter Decoupling (1 agent)
**Gate: Batch B merged**

- **#21** — Move `get_rate_limiter()` singleton to `core/rate_limiter.py`; update imports in `dashboard/routes.py` and `workers/tasks.py`

### Batch D — DI Unification (1 agent)
**Gate: Batch C merged**

- **#19** — Introduce `ServiceRegistry` in `core/registry.py`; refactor fetcher/extractor/channel instantiation in workers and routes; replace monkeypatching in tests with registry injection

### Batch E — Decompose tasks.py (1 agent)
**Gate: Batch D merged**

- **#14** — Split into `workers/pipeline.py` (check pipeline + helpers), `workers/notify.py` (dispatch), `workers/tasks.py` (thin task definitions)

### Batch F — Feature Completion (2 agents, parallel)
**Gate: Batch E merged**

| Agent | Issues | Notes |
|---|---|---|
| F1 | #9 + #10 — Ignore patterns + change threshold | Both touch `core/differ.py`; batch together |
| F2 | #12 — Differentiate archive from deactivate | Model migration + post-action logic |

F1 and F2 both touch `workers/pipeline.py` in different functions (diff step vs. post-action step). Merge F1 first; F2 rebases before merge.

### Deferred

**#3, #4, #5** — Phase 7 (Playwright, WebRecorder, adaptive escalation). Revisit after Batch F is stable and all architectural foundations are in place.

## Key Decisions

- **Worktrees**: each agent works in an isolated `.worktrees/<branch>` worktree; no shared working directory state
- **TDD throughout**: all implementations follow red → green → refactor per project convention
- **Batch B bundles #25+#16+#18**: one PR covers all three to reduce merge ceremony on the wide-reach mechanical changes
- **Phase 7 deferred**: browser-based fetching is more valuable built on a clean DI foundation (#19) and decomposed pipeline (#14)
- **#11 status**: verify closure before execution; if still open, it was addressed by #30

## Out of Scope

- Phase 7 advanced fetchers (#3, #4, #5)
- New feature work beyond what is described in open issues
- Dashboard UI improvements beyond #27, #28
