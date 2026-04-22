# Notifications Polish Backlog (Issues #104–#109)

## Goal

Clear the open notifications-feature backlog (#104–#109) — six post-#102/#110 polish items spanning default templates, new template variables, UI form layout, and preview-pane UX — through a parallel-safe two-batch execution plan.

## Approved approach

- All 6 issues in scope; nothing deferred.
- Two batches: 4 parallel agents in Batch A, then 1 agent in Batch B once A is merged.
- Worktrees for branch isolation. Shared `batch/notifications-a` branch for the multi-agent batch; B1's feature branch is its own batch branch.
- Regular merge commit per batch into `main`, preserving per-agent commit history.

## Prioritization rubrics

Three dimensions, formula `(Foundation × 2) + (Correctness × 2) + Scope`, max 15.

| Dimension | 1 | 2 | 3 |
|---|---|---|---|
| **Foundation Leverage** | Standalone improvement | 1–2 other issues benefit | Multiple issues depend on or are simplified by this |
| **Correctness Risk** | Cosmetic / organizational | Edge-case incorrect behavior, runtime failure risk | Data loss, race conditions, silent failures |
| **Scope Clarity** | Requires design discovery | Clear direction, minor decisions needed | Mechanical — implementation is obvious from the issue |

Quality stance for this backlog: all dimensions matter roughly equally. Pre-production deployment context (runway to build right; few/no real notification subscribers).

## Scored backlog

| # | Title | Found ×2 | Corr ×2 | Scope | **Score** | Blast |
|---|---|:-:|:-:|:-:|:-:|---|
| #104 | Update default notification template | 4 | 4 | 3 | **11** | Low |
| #105 | Add Change URL template variable | 6 | 2 | 2 | **10** | Med |
| #108 | Template variables for diff options | 4 | 2 | 2 | **8** | Med |
| #107 | Reorder Subscribe event types | 2 | 2 | 3 | **7** | Low |
| #109 | Limit preview event selector | 2 | 2 | 3 | **7** | Low |
| #106 | Links exceed Preview pane width | 2 | 2 | 2 | **6** | Low |

## Conflict zones

Files touched by 2+ issues:

| File | Issues | Sequencing |
|---|---|---|
| `src/core/notifications/default_templates.py` | #104, #105, #108 | #104 changes DEFAULT_TITLE/BODY (≈L79–105); #105/#108 add to TEMPLATE_VARIABLES (≈L38–71) and ADDITIVE_BODY_SNIPPETS (≈L122–143). Hunks disjoint, but sequenced: A4 (#105+#108) before B1 (#104) to remove all collision risk and let B1 use `change_url` if desired. |
| `src/core/notifications/content.py` | #105, #108 | Bundled into A4 (sequential commits) — both add fields to `build_template_context()`. |
| `src/dashboard/templates/partials/notification_variable_chips.html` | #105, #108 | Bundled into A4 — both add chips to primary lists. |

Single-issue files (no contention):

- `src/core/notifications/events.py` → #107
- `src/dashboard/templates/partials/notification_preview.html` → #106 (rendered fragment)
- `src/dashboard/templates/partials/notification_form_preview_card.html` → #109 (selector wrapper)
- `src/api/schemas/content_config.py` → #105 (`include_watch_url` toggle)
- `src/dashboard/templates/partials/notification_form_content_body.html` → #105 (LINKS rename, Watch URL toggle)

**#106 vs #109 do not collide** — they live in different partials (`notification_preview.html` is the rendered fragment returned by `POST /notifications/preview`; `notification_form_preview_card.html` is the selector wrapper).

## Dependency graph

```
#106 ────────────────────►  (independent)
#107 ────────────────────►  (independent)
#109 ────────────────────►  (independent)

#105 + #108 (bundled) ──►  #104
        (variables)          (defaults that may use new vars)
```

## Batch execution plan

### Batch A — 4 parallel agents on shared branch `batch/notifications-a`

Orchestrator runs `git checkout main && git pull --ff-only && git checkout -b batch/notifications-a` **before** spawning any agents (per branch hygiene Rule 3).

| Agent | Issues | Scope summary | Files |
|---|---|---|---|
| **A1** | #106 | Wrap or clip long URLs in preview pane so they stay inside the card | `partials/notification_preview.html` (+ tests) |
| **A2** | #107 | Reorder `EVENT_TITLES` and `WatchEventType` to: Watch Created, Change Detected, Watch Error, Watch Recovered, Watch Paused, Watch Resumed, Watch Archived, Watch Deleted | `core/notifications/events.py` (+ any tests asserting order) |
| **A3** | #109 | Filter `<select name=preview_event>` options to `subscribed_events` at render time; reuse override-picker pattern. Empty events → keep all options | `partials/notification_form_preview_card.html` (+ integration tests) |
| **A4** | #105 → #108 (sequential commits) | (1) Add `change_url` template variable, rename "LINK" → "LINKS" in form, add Watch URL toggle (`include_watch_url`), add `change_url` chip. (2) Add `diff_snippet`/`diff_full` derived fields to `build_template_context()`, register both in `TEMPLATE_VARIABLES`, add chips. | `default_templates.py`, `content.py`, `content_config.py`, `notification_form_content_body.html`, `notification_variable_chips.html` (+ tests) |

**Gate:** Start immediately.

After all four agents signal completion, orchestrator runs the full pytest suite + ruff against `batch/notifications-a` and notifies the user for review and merge.

### Batch B — 1 agent on `feature/batch-b-104`

| Agent | Issues | Scope summary | Files |
|---|---|---|---|
| **B1** | #104 | Replace `DEFAULT_TITLE_TEMPLATES` and `DEFAULT_BODY_TEMPLATES` per issue spec. May use `change_url` from A4 if cleaner; otherwise inline `APP_URL`. | `default_templates.py` (+ tests) |

**Gate:** After Batch A merged to `main`. Orchestrator does `git checkout main && git pull --ff-only` before spawning B1.

## Key decisions

- **#105 + #108 bundled into one agent (A4) with sequential commits.** Both touch `default_templates.py`, `content.py`, and `notification_variable_chips.html`. Cohesive theme ("add template variables") makes a single agent more efficient than two with a gate.
- **#104 isolated to Batch B.** Strictly speaking, hunks in `default_templates.py` are disjoint from A4 (B1 edits template strings, A4 edits the variables registry), so git would auto-merge. Sequencing eliminates risk and lets B1 use the new `change_url` variable from A4#105 if the agent prefers cleaner code over the literal issue spec (which inlines the URL).
- **#106 and #109 in parallel despite both being "preview pane" work.** They live in different partials with no overlapping concerns — #106 is a wrapping fix on the rendered fragment, #109 is a `<select>` filter on the selector wrapper.
- **#107 split from A4 even though it's also a "form area" change.** Different file (`events.py` enum/dict, not a partial), independent test surface, and would only conflict on test files if A4's tests happened to assert event order. Easy to keep separate.

## Out of scope

None. All 6 open notification issues (#104–#109) are addressed. The optional client-side dynamic re-filter on subscribe-checkbox toggle in #109 is included in scope (server-render filter is the floor; JS is encouraged if straightforward).

## Deferred items

None for this backlog. Larger items in the open backlog (#3, #4, #5, #33, #36, #39, #63, #71) are unrelated to notification UI/template polish and remain on the project backlog.
