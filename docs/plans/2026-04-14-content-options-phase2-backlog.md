# Content Options Phase 2 — Backlog Orchestration Plan

**Date:** 2026-04-14
**Issues:** #89, #90, #91, #92, #93, #94, #95
**Tracking issue:** TBD

---

## Goal

Clear the post-#88 content options backlog in priority order using a hybrid parallel/sequential agent strategy. All issues are additive follow-ups to the notification content options system shipped in #88, plus one UI layout bug (#95). Pre-production context — prioritize full test coverage and clean abstractions over speed.

---

## Approved Approach

Three sequential batches. Batch A maximizes parallelism on isolated work. Batches B and C are single-agent to avoid merge conflicts on the heavily contested core files (`content.py`, `content_config.py`, `notification_content_options.html`).

---

## Prioritization Rubrics

**Formula:** Score = (Foundation × 2) + (Correctness × 2) + Scope (max 15)

| Dimension | 1 | 2 | 3 |
|---|---|---|---|
| **Foundation Leverage** | Standalone improvement | 1–2 other issues benefit | Multiple issues depend on or are simplified by this |
| **Correctness Risk** | Cosmetic / organizational | Edge-case incorrect behavior, runtime failure risk | Data loss, race conditions, silent failures |
| **Scope Clarity** | Requires design discovery | Clear direction, minor decisions needed | Mechanical — implementation is obvious from the issue |

Blast radius (files touched across issues) drives *sequencing*, not score.

---

## Scored Backlog

| # | Title | F | C | S | Score | Blast |
|---|---|---|---|---|---|---|
| #89 | Jinja2 title/body templates (Phase 2) | 2 | 2 | 2 | **10** | High |
| #92 | include_change_dashboard_url | 2 | 1 | 2 | **8** | Med |
| #91 | include_tags/description + Watch migration | 2 | 1 | 2 | **8** | Med |
| #95 | Button layout bug (Notifications header) | 1 | 1 | 3 | **7** | Low |
| #94 | include_last_changed_at | 1 | 1 | 3 | **7** | Low |
| #93 | include_significance | 1 | 1 | 3 | **7** | Low |
| #90 | Per-event content options accordion | 1 | 1 | 2 | **6** | Med |

**Scoring notes:**
- #89 Correctness=2: Jinja2 rendering of user-provided templates can raise exceptions that could silently break dispatch — requires careful error handling
- #92 Foundation=2: `app_url` extraction to a shared constant benefits dispatcher.py and future notification work
- #91 Foundation=2: Watch model migration (tags/description) benefits the broader domain model; #91 is unblocked by including the migration in scope
- #90 scores lowest: the API already stores overrides correctly; pure UI completeness, no architectural or correctness risk

---

## Conflict Zones

| File | Issues | Required merge order |
|---|---|---|
| `src/api/schemas/content_config.py` | #89, #91, #92, #93, #94 | Additive options (#94→#93→#92) → Watch-backed options (#91) → Phase 2 templates (#89) |
| `src/core/notifications/content.py` | #89, #91, #92, #93, #94 | Same as above |
| `src/dashboard/templates/partials/notification_content_options.html` | #89, #90, #92, #93, #94 | Additive checkboxes first → Watch fields → Phase 2 textarea inputs → accordion |
| `src/dashboard/routes.py` | #89, #90 | Phase 2 form fields (#89) → per-event overrides (#90) |
| `src/workers/pipeline.py` | #92, #93 | Either order (disjoint lines); #94→#93→#92 chosen for ascending complexity |
| `src/workers/tasks.py` | #91, #94 | #94 in Batch A; #91 in Batch B (after A merged) |
| `src/core/notifications/dispatcher.py` | #89, #92 | #92 extracts `app_url` constant in Batch A; #89 consumes it in Batch C |

---

## Dependency Graph

```
#95  ────────────────────────────────────► isolated; merge any time in Batch A

#94 ┐
#93 ├── additive content options (pipeline/tasks metadata + content.py + template)
#92 ┘   bundled into single agent; sequential commits eliminate all conflicts
         #92 also extracts app_url constant from dispatcher.py

#91 ─── Watch model migration (tags, description) → content option fields
         must follow #92/#93/#94 to inherit extended content.py cleanly

#89 ─── Phase 2 Jinja2 templates; consumes app_url from #92;
         architecturally caps the content options system
    └── #90 extends same template sections and form-parsing function
         → bundle after #89 in same agent
```

---

## Batch Execution Plan

| Batch | Agent | Issues | Files | Gate |
|---|---|---|---|---|
| A | A1 | #95 | `partials/watch_notifications.html` | Start immediately |
| A | A2 | #94 → #93 → #92 (sequential commits) | `tasks.py`, `pipeline.py`, `dispatcher.py`, `content.py`, `content_config.py`, `notification_content_options.html` | Start immediately |
| B | B1 | #91 | Watch model, new migration, `tasks.py`, `content.py`, `content_config.py`, `notification_content_options.html` | After Batch A merged |
| C | C1 | #89 → #90 (sequential commits) | `content.py`, `content_config.py`, `dispatcher.py`, `notification_content_options.html`, `routes.py` | After Batch B merged |

**Branch strategy:**
- Batch A: orchestrator checks out `batch/a` before spawning A1 and A2; both use `isolation: "worktree"`; output accumulates on `batch/a`
- Batch B: B1 is single-agent; its feature branch serves as the batch branch directly
- Batch C: C1 is single-agent; its feature branch serves as the batch branch directly
- Merge to `main` with regular merge commits (preserves per-agent history)
- Sync local `main` (`git pull --ff-only`) before every batch launch

**Intra-batch commit ordering:**

*A2 — #94 → #93 → #92:*
- #94 first: touches only `tasks.py` + content additions (cleanest, most isolated)
- #93 next: adds `significance` to `pipeline.py` change_metadata
- #92 last: adds `change_id` to `pipeline.py` + extracts `app_url` constant from `dispatcher.py` (most cross-cutting of the three)

*C1 — #89 → #90:*
- #89 first: Jinja2 backend (content.py rendering, dispatcher.py title override, template textarea inputs, routes.py form parsing for title_template/body_template)
- #90 second: extends the same template sections and form-parsing function established by #89; builds on the structure rather than adding in isolation

---

## Key Decisions

**Bundle #92+#93+#94 into one agent (A2) rather than parallel agents.**
All three write to `content.py`, `content_config.py`, and `notification_content_options.html`. True parallelism is impossible without merge conflicts. Bundling into sequential commits within one agent eliminates all intra-batch conflicts cleanly. The bundle is semantically coherent — all three are identical-pattern additive content options.

**#91 goes in Batch B despite scoring below #89.**
#91 requires a Watch model migration. It must come after the additive content options agents (A2) have extended `content.py` and `content_config.py` so the migration agent inherits a stable, extended codebase. Putting it after A also ensures the migration is on a known-good `main` before the architectural Phase 2 work (#89) begins.

**#89 scores highest but ships last (Batch C).**
High blast radius — touches every contested file. Also consumes the `app_url` constant extracted by #92 (Batch A). Sequencing it last means it inherits all prior additions to `content.py` and `content_config.py` cleanly and can write the architectural override layer on top of a fully populated system.

**#89 and #90 bundled into C1.**
Both heavily modify `notification_content_options.html` and `routes.py` form parsing. #90's accordion builds directly on the template structure #89 establishes. A gate between them would require rebasing on a near-identical file state — bundling is cleaner.

---

## Deferred Items

- **Template preview** (#89 notes): render Jinja2 template against a synthetic event in the dashboard. Deferred by the issue itself — ship template inputs without live preview.
- **AI-powered summaries**: explicitly out of scope per #89.
- **Template variable autocomplete**: explicitly out of scope per #89.

---

## Out of Scope

Nothing surfaced during design that was ruled out. All 7 issues are in scope.
