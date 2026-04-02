# Screenshot, Dashboard, and Pipeline Backlog — Orchestration Plan

**Date:** 2026-04-02
**Issues:** #54, #55, #56, #57, #58, #59, #60, #62
**Deferred:** #63 (digest notifications — out of scope this wave)

---

## Goal

Clear the post-#53 backlog: screenshot enhancements (#54, #55, #56, #57), dashboard UX improvements (#58, #59, #62), and a pipeline extraction improvement (#60). Eight issues across three merge batches, maximizing parallel throughput where file coverage is disjoint.

---

## Approved Approach

Hybrid parallelism: parallel agents within each batch, sequential gates between batches. Git worktrees for branch isolation (`isolation: "worktree"`). Regular merge commits to main to preserve per-agent history. Three batches: 3 parallel → 2 parallel → 1 sequential pair.

---

## Prioritization Rubrics

**Score = (Foundation × 2) + (Correctness × 2) + Scope** — max 15

| Dimension | 1 | 2 | 3 |
|---|---|---|---|
| **Foundation Leverage** | Standalone improvement | 1–2 other issues benefit | Multiple issues depend on or are simplified by this |
| **Correctness Risk** | Cosmetic / organizational | Edge-case incorrect behavior, runtime failure risk | Data loss, race conditions, silent failures |
| **Scope Clarity** | Requires design discovery | Clear direction, minor decisions needed | Mechanical — obvious from the issue |

Foundation and Correctness are doubled to weight architectural and safety concerns over mechanical effort.
Blast radius (files touched across issues) drives *sequencing*, not score.

---

## Scored Backlog

| # | Title | Foundation | Correctness | Scope | **Score** | Blast |
|---|---|:---:|:---:|:---:|:---:|:---:|
| #54 | Screenshot: skip/handle non-HTML (PDF, CSV) | 2 | 2 | 2 | **10** | Med |
| #60 | Ignore-by-selector: CSS exclusions for HTML extractor | 2 | 2 | 2 | **10** | Low |
| #56 | On-demand screenshot re-capture endpoint | 1 | 2 | 2 | **8** | Med |
| #57 | Visual diff: screenshot comparison between snapshots | 1 | 2 | 2 | **8** | Med |
| #58 | Watch list: Health column | 1 | 2 | 2 | **8** | Low |
| #59 | Snapshot content viewer on watch detail | 1 | 2 | 2 | **8** | Med |
| #62 | Change History → lifecycle event timeline | 1 | 2 | 2 | **8** | High |
| #55 | Configurable viewport size | 1 | 1 | 3 | **7** | Low |

**Scoring notes:**
- #54 Foundation=2: pipeline correctness for non-HTML; #57 visual diff benefits from knowing which snapshots have valid screenshots
- #60 Foundation=2: extractor is core pipeline; cleaner chunks improve diff signal for every HTML watch
- #62 Blast=High: touches `routes.py`, `context.py`, and `watch_detail` template — three contested files
- #55 Correctness=1: current hardcoded defaults (1280×800) work fine; purely additive

---

## Conflict Zones

### Contested files and required merge order

| File | Issues | Required order |
|---|---|---|
| `src/api/schemas/watch.py` | #55, #60 | #60 before #55 (both add to `validate_fetch_config`; bundle in one agent) |
| `src/core/screenshot.py` | #55, #56 | #55 before #56 (viewport params must exist before on-demand endpoint uses them) |
| `src/dashboard/routes.py` | #56, #59, #62 | #62 first (modifies `watch_detail_page`); #56 and #59 add new routes after |
| `src/dashboard/context.py` | #57, #58, #62 | Parallel-safe: #57 touches `get_change_detail`, #58 touches `get_watch_list`, #62 adds new function |
| `src/dashboard/templates/watch_detail*` | #56, #59, #62 | #62 first (structural refactor); #56 and #59 add to refactored template (bundle in one agent) |

### Non-contested high-blast files
- `src/workers/pipeline.py` — #54 only
- `src/core/extractors/html.py` — #60 only
- `src/core/models/change.py` + migration — #57 only

---

## Dependency Graph

```
#54 ──────────────────────── standalone (pipeline.py screenshot guard)
#58 ──────────────────────── standalone (context.get_watch_list + watches template)
#57 ──────────────────────── standalone (Change model + migration + change_detail)
     [A2/A3 share context.py but different functions — clean merge]
                                         │
                              ┌──────────┴──────────┐
                              ▼                     ▼
#60 → #55 (sequential)    #62 (solo)
  schemas + extractor      routes + context (new) + watch_detail (refactor)
                                         │
                              ┌──────────┘
                              ▼
                       #56 → #59 (sequential)
                       routes (new) + watch_detail (additions)
```

---

## Batch Execution Plan

### Batch A — 3 parallel agents (start immediately)

| Agent | Issue | Files | Notes |
|---|---|---|---|
| A1 | #54 | `workers/pipeline.py`, `core/screenshot.py` (guard call site), tests | Add `content_type` guard before screenshot step; evaluate PDF thumbnail |
| A2 | #58 | `dashboard/context.py` (get_watch_list), watches list template, tests | Derive health state from Snapshot.fetched_at vs interval; badge variants |
| A3 | #57 | `models/change.py`, alembic migration, `context.py` (get_change_detail), change_detail template, tests | `visual_change_score` nullable float; before/after thumbnails when both snapshots have screenshots |

**Intra-batch note:** A2 and A3 both write to `context.py` but to different functions (`get_watch_list` vs `get_change_detail`) — merge will be clean.

**Gate:** All three merged and tests passing on `batch/a` before launching Batch B.

---

### Batch B — 2 parallel agents (after Batch A merged)

| Agent | Issues | Files | Notes |
|---|---|---|---|
| B1 | #60 → #55 (sequential commits) | `api/schemas/watch.py`, `core/extractors/html.py`, `core/screenshot.py`, tests | Commit #60 first (higher score, establishes `validate_fetch_config` pattern); then #55 adds viewport kwargs |
| B2 | #62 | `dashboard/routes.py`, `dashboard/context.py` (new timeline func), `templates/watch_detail*`, tests | Structural refactor — replaces change history section with unified timeline; sets template shape for Batch C additions |

**Intra-batch note:** B1 and B2 are fully disjoint (backend schemas/extractor/screenshot vs dashboard) — genuinely parallel.

**Gate:** Both merged and tests passing on `batch/b` before launching Batch C.

---

### Batch C — 1 agent, 2 sequential commits (after Batch B merged)

| Agent | Issues | Files | Notes |
|---|---|---|---|
| C1 | #56 → #59 (sequential commits) | `dashboard/routes.py` (new routes), `templates/watch_detail*` (additions), tests | #56 first: POST endpoint + re-capture button (screenshot section); then #59: GET content route + viewer link (snapshot metadata section). Bundled to avoid watch_detail template conflict. |

**Gate:** Merged to main and tests passing. Ready for deployment.

---

## Key Decisions

1. **#57 in Batch A, not Batch B.** Visual diff has no dependency on the schema/extractor changes in B1 or the timeline refactor in B2. Advancing it to Batch A reduces the critical path by one gate.

2. **#60 leads B1's sequential pair.** Higher score (10 vs 7) and establishes the `validate_fetch_config` extension pattern that #55's viewport validation follows in the second commit.

3. **#62 gets its own Batch B slot.** Highest blast in the backlog — touches routes, context, and the watch_detail template. Runs parallel to B1 (disjoint files) but must complete before C1 adds to the refactored template.

4. **#56 and #59 bundled in C1.** Both add to `routes.py` and `watch_detail.html` post-#62 refactor. Parallel would require coordinating template additions at the same file; bundling eliminates the risk with no throughput cost (only 1 agent in this batch anyway).

5. **#55 after #56 in the dependency graph — but same agent as #60.** The on-demand endpoint (#56, Batch C) calls `capture_screenshot()` with viewport params from the watch's `fetch_config`. Since B1 lands before C1, the viewport kwargs are in place when C1 is written.

---

## Deferred Items

| Issue | Reason |
|---|---|
| #63 | Digest notifications: explicitly excluded this wave by user. Significant scope (new worker, DB columns, per-channel rendering). Revisit in next backlog pass. |
| #3, #4, #5 | Phase 7 fetchers: deferred from prior session (2026-03-23). Not yet unblocked. |
| #33 | Redis-backed rate limiting: depends on multi-process scale not yet needed. |
| #36, #39 | i18n / modal focus trapping: deferred YAGNI. |

---

## Out of Scope

- Animated GIF / video for visual diff (#57 explicitly excludes)
- Full-page scroll screenshots
- Pixel diff scoring for non-HTML content types (no screenshots to compare)
- Any work requiring #3 (Playwright fetcher) or #4 (WebRecorder) as prerequisites
