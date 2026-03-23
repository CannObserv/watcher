---
name: orchestrating-issue-backlog
description: Prioritize an open issue backlog using agreed rubrics, analyze conflict zones and dependencies, design a parallel-safe batch execution plan using git worktrees, produce a design doc and GitHub issue, and hand off to an agent team.
compatibility: Designed for Claude. Requires git, gh CLI, and a project using git worktrees for branch isolation.
metadata:
  author: gregoryfoster
  version: "1.0"
  triggers: "orchestrate backlog, prioritize issues, plan issue execution, clear backlog"
---

# Orchestrating an Issue Backlog

Turn an open GitHub issue backlog into a prioritized, parallel-safe execution plan for an agent team. Interview the user to agree on rubrics, score all issues, identify conflict zones, design merge-safe batch assignments, and produce a design doc and tracking issue.

<HARD-GATE>
Do NOT assign priorities, design batches, write a design doc, or open a GitHub issue until rubrics are agreed upon and the scored backlog has been presented to and approved by the user. Each major section requires explicit approval before proceeding to the next.
</HARD-GATE>

## Checklist

Create a task for each item and complete them in order:

1. **Fetch all open issues** — `gh issue list --state open --limit 50 --json number,title,labels,body`
2. **Explore project context** — read AGENTS.md, recent commits, existing design docs
3. **Interview user** — establish rubrics and constraints (one question at a time)
4. **Score all issues** — apply rubrics, present table, get approval
5. **Analyze conflict zones** — identify files touched by multiple issues; build dependency graph
6. **Present dependency analysis** — get approval before batch design
7. **Design batch plan** — assign issues to merge batches; get approval
8. **Write design doc** — `docs/plans/YYYY-MM-DD-<topic>-backlog.md`; commit
9. **Open GitHub tracking issue** — link to design doc; list batches
10. **Write or update skill** — capture process learnings for reuse

---

## Process

### Step 1–2: Context gathering

Fetch issues and read project context before asking any questions. Go into the interview knowing:
- Rough categories of issues (architectural, bug, feature, infra)
- Which files are most frequently touched across issues
- Any issues that are likely already closed (cross-reference recent commits)

### Step 3: Interview (one question at a time)

Four questions establish everything needed. Ask them in order; do not stack multiple questions.

**Q1 — What does "quality" mean here?**
> Which matters most: testability, correctness, maintainability, or all roughly equally?

**Q2 — What is the deployment context?**
> Pre-production (runway to build it right), early production (real users, low volume), or active production (stability required)?

**Q3 — Are any issue categories explicitly deferred?**
> e.g. "Phase 7 fetchers are not a priority right now" — establishes what to exclude from scoring

**Q4 — Parallelism preference?**
> Maximize parallel agents, sequential waves, or hybrid (parallel within batches, gates between)?
> Follow up: worktrees for branch isolation? (almost always yes)

Record agreements explicitly as you go — they feed the design doc.

### Step 4: Scoring rubric

Use this three-dimension rubric unless the user requests different dimensions or weights.

**Score = (Foundation × 2) + (Correctness × 2) + Scope**, max 15.

| Dimension | 1 | 2 | 3 |
|---|---|---|---|
| **Foundation Leverage** | Standalone improvement | 1–2 other issues benefit | Multiple issues depend on or are simplified by this |
| **Correctness Risk** | Cosmetic / organizational | Edge-case incorrect behavior, runtime failure risk | Data loss, race conditions, silent failures |
| **Scope Clarity** | Requires design discovery | Clear direction, minor decisions needed | Mechanical — implementation is obvious from the issue |

**Blast radius** (files touched across issues) drives *sequencing*, not score. High-blast issues get their own batch slot even when high priority.

Present the scored table sorted by score descending. Include a blast column (Low/Med/High). Note any issues that appear closed by recent commits.

Get approval before moving to conflict analysis.

### Step 5–6: Conflict zone analysis

Identify files touched by 2+ issues — these drive sequencing decisions:

1. List each contested file and the issues that touch it
2. Determine required merge order within each file (usually: smaller targeted fixes first, wide refactors last, features after foundations)
3. Derive a dependency graph showing which issues must precede which

Present the conflict zones and dependency graph. Get approval.

### Step 7: Batch design

Group issues into **merge batches**. The core principle: within a batch, all agents work on branches with disjoint file coverage so PRs can be merged in any order. Between batches, a gate ensures prior work is merged and stable before the next batch begins.

**Batch design rules:**
- **Batch 0 / Batch A**: truly isolated issues — each touches files no other issue in this batch touches. Maximum agent count.
- **Subsequent batches**: ordered by the dependency chain of contested files. One agent per batch on the critical path; parallelize only where file coverage is genuinely disjoint.
- **Bundle related issues** in one agent when: they touch the same file(s) AND are best reviewed together (e.g. define constants then use them; fix protocol then add config models).
- **Correctness fixes first within a batch**: if a targeted bug fix touches a file that later gets a wide refactor, put the bug fix at the head of the refactor agent's commit sequence, not in an earlier parallel slot.
- **Features last**: issue categories scored below architectural work go in the final batch(es).

Present a table:

| Batch | Issues | Agents | Gate |
|---|---|---|---|
| A | #n, #m, ... | N (parallel) | Start immediately |
| B | #n → #m | 1 (sequential commits) | After A merged |
| ... | | | |

Include a note for any intra-batch merge ordering (e.g. "F1 merges first; F2 rebases before merge").

Get approval before writing the design doc.

### Step 8: Design doc

Path: `docs/plans/YYYY-MM-DD-<topic>-backlog.md`

Sections:
- **Goal** — one paragraph
- **Approved approach** — summary
- **Prioritization rubrics** — table + formula
- **Scored backlog** — full table
- **Conflict zones** — contested files and their required merge order
- **Dependency graph** — ASCII or text
- **Batch execution plan** — per-batch table with agents, issues, files, gate condition
- **Key decisions** — rationale for non-obvious choices (e.g. why a correctness fix leads a refactor batch)
- **Deferred items** — what was explicitly excluded and why
- **Out of scope** — anything that came up but was ruled out

Commit:
```
#<n> docs: add <topic> backlog orchestration plan
```

### Step 9: GitHub tracking issue

```bash
gh issue create \
  --title "<topic>: prioritized backlog clearance (<N> batches, <M> issues)" \
  --body "$(cat <<'EOF'
## Summary
<2–3 sentences>

## Design doc
\`docs/plans/YYYY-MM-DD-<topic>-backlog.md\`

## Scope
**Batch A — N parallel agents**
- #n Issue title
...

**Batch B — 1 agent (after A merged)**
- #n, #m Issue titles
...

**Deferred:** #n, #m (reason)
EOF
)"
```

Report the issue number.

### Step 10: Process documentation

After the plan is approved and committed, capture any adjustments made during this session:
- Did the user adjust rubric weights? Document the new formula.
- Were any standard questions skipped or reordered? Note why.
- Did any conflict analysis surface surprises? Record the pattern.
- Were any rubric dimensions inadequate for this project type? Flag for skill revision.

Update this skill file if patterns emerged that should be generalized.

---

## Agent Roles

### Branch strategy

Each **multi-agent batch** gets a shared feature branch (e.g. `batch/a`, `batch/f`). Workers use individual worktree branches (e.g. `feature/batch-a-13-schema-move`). The orchestrator merges worker branches into the batch branch sequentially, respecting any intra-batch ordering. Conflicts are returned to the responsible worker agent to resolve.

**Single-agent batches** do not need a separate batch branch — the agent's feature branch serves directly.

The human review happens against the **batch branch**: run tests, inspect the combined diff, then merge to `main` with a regular merge commit (preserving per-agent commit history).

Ask the user their preferred merge strategy (regular, squash, rebase) and record it in the design doc.

### Orchestrator agent

The orchestrator reads the batch plan and manages progression. It:
1. **Sync local main before every batch launch** — `git fetch origin && git merge --ff-only origin/main`. Agents worktree from local main; if local main is stale, agents base their work on the wrong commit.
2. Creates `batch/<X>` branch from `main` for each multi-agent batch at launch time
3. Launches all worker agents whose batch gate is currently satisfied simultaneously
4. On each worker completion signal, merges that worker's branch into the batch branch (respecting intra-batch ordering; returns conflicts to the worker)
5. When all workers are merged, runs the full test suite against the batch branch
6. Notifies the user: "Batch X ready for review: `batch/<X>`, N issues, tests passing"
7. Waits for merge confirmation before launching the next batch
8. On confirmation, syncs local main again, then launches all newly unblocked batches simultaneously

Never writes implementation code itself.

### Worker agents

Each worker agent follows this protocol before signaling completion:

1. **Set up worktree** — isolated branch `feature/batch-<X>-<issue>` in `.worktrees/`
2. **Implement with TDD** — red → green → refactor
3. **Run full test suite** — all tests must pass
4. **Run linter** — no violations
5. **Self-review diff** — check: correctness, test coverage, project conventions, no unintended side effects outside issue scope
6. **Address findings** — fix before signaling; do not signal with known issues
7. **Signal completion** — notify orchestrator the branch is ready to merge into the batch branch

**No PR is opened by the worker.** The orchestrator merges into the batch branch; the user reviews the batch branch as a whole.

## Key Principles

- **One question at a time** — stacking questions gets partial answers
- **Approval gates are real** — do not proceed past a section without explicit user sign-off
- **Blast radius ≠ priority** — a high-blast issue may score high but still must wait for lower-priority isolates to merge first
- **Correctness fixes lead refactors** — if a bug fix and a structural refactor both touch the same file, fix the bug in the first commit of the refactor branch, not in a separate earlier batch
- **Bundle when cohesive** — two issues that naturally sequence (define → use, protocol → config) belong in one agent with sequential commits, not two agents with a gate
- **Worktrees always** — each agent branch gets an isolated worktree; no shared working directory state between concurrent agents
- **Deferred is a decision** — explicitly name what is out of scope and why; don't silently omit
- **Batch feature branches for multi-agent batches** — gives the user a single integration point to test and review before merging to main; surfaces intra-batch conflicts at the batch branch, not at main
- **Single-agent batches skip the extra branch** — the agent's feature branch is the batch branch
- **No worker PRs** — workers signal to the orchestrator; the orchestrator merges into the batch branch; the user reviews the batch branch
- **Conflict resolution stays with the worker** — if a merge into the batch branch conflicts, the orchestrator sends it back to that agent
- **Self-review before signal** — worker agents resolve all findings before signaling; no known issues at signal time
- **Orchestrator launches all unblocked batches** — not just the next one in sequence; if two independent batches become unblocked simultaneously, launch both
- **Regular merge commit to main** — preserves per-agent commit history; ask user preference at design time

## Branch Hygiene Rules

These rules prevent the class of failures that produced the Batch B→C conflict:

### Rule 1 — Sync local main before every agent launch

`git push origin HEAD:main` from a feature branch advances `origin/main` but does **not** move local `main`. Worktree agents branch from local `main`. If local `main` is behind `origin/main`, agents silently base their work on the wrong commit.

**Before launching any batch:**
```bash
git checkout main
git pull --ff-only   # or: git fetch origin && git merge --ff-only origin/main
```

If `--ff-only` fails, the branches have diverged — stop and investigate before proceeding.

### Rule 2 — Never use `git push origin HEAD:main` to advance main

This is the root cause of Rule 1 violations. Always push from local `main`:
```bash
git checkout main
git merge --ff-only feature/batch-x   # or rebase; whatever the agreed strategy is
git push origin main
```

Or, if agents auto-merged to main (see Rule 3), just:
```bash
git push origin main   # from local main after verifying it is up to date
```

### Rule 3 — `isolation: "worktree"` auto-merges to local main, not origin/main

The `isolation: "worktree"` Agent tool parameter creates a temporary worktree, runs the agent in it, then merges any changes back to **the current local branch** (not to origin). This means:
- Per-agent feature branches do not persist after the agent completes
- Work lands on local `main` as agents complete — which is fine
- But local `main` diverges from `origin/main` until you push
- The next agent launch must therefore start with a `git pull --ff-only` to get any intervening pushes

To preserve isolated feature branches through agent completion, instruct the agent explicitly: `git checkout -b feature/batch-x` as its first step, commit there, and do **not** use `isolation: "worktree"`. The orchestrator then merges manually.

### Rule 4 — Fix commit messages before continuing after a rebase conflict

When `git rebase --continue` auto-generates a commit message from the conflict resolution, it replaces the original `#N type: description` format with a verbose blob. Fix it immediately with `git commit --amend` on that commit **before** continuing the rebase or adding more commits — amending the wrong commit requires a `reset --soft` recovery.

```bash
git rebase --continue          # resolves conflict, creates commit with bad message
git commit --amend -m "..."    # fix message before doing anything else
# only then: git rebase --continue for the next patch (if any)
```

---

## Process Log — Session 2026-03-23

**Agreements reached:**
- Rubric dimensions: Foundation Leverage, Correctness Risk, Scope Clarity
- Score formula: (Foundation×2) + (Correctness×2) + Scope (doubles Foundation and Correctness to weight architectural and safety concerns over mechanical effort)
- Blast radius drives sequencing, not score
- Phase 7 issues (#3, #4, #5) explicitly deferred until architectural foundation is solid
- Parallelism: maximize where file coverage is disjoint; git worktrees for isolation
- Deployment context: pre-production (runway to build right)
- Output: design doc + GitHub tracking issue + this skill

**Observed agent behavior (2026-03-23 execution):**
- `isolation: "worktree"` agents auto-merge their completed changes back to the repo's current branch (main) rather than leaving them on an isolated feature branch. This means per-agent branches cannot be selectively merged by the orchestrator — work lands on main as agents complete.
- **Impact on batch/a strategy**: batch/a still functions as a human review checkpoint. After all Batch A agents complete, fast-forward `batch/a` to the current main HEAD, run the test suite, and notify the user. The integration safety comes from the test run, not from a separate branch.
- **Impact on single-agent batches** (B–E): no change — the agent's worktree branch is the batch branch anyway.
- **Future consideration**: to preserve per-agent isolation in a multi-agent batch, explicitly instruct agents to create and stay on a named feature branch (e.g. `git checkout -b feature/batch-a-13`) rather than relying on the isolation parameter to enforce this.

**Clarifications added after initial design:**
- Orchestrator launches all unblocked batches simultaneously — not just the next numbered batch. Initial design implied sequential launching; user clarified all safe parallel work should start at once.
- Worker agents self-review and fix all findings before signaling completion. Keeps human review focused on merge decisions, not catching obvious issues.
- Multi-agent batches use a shared `batch/<X>` feature branch. The orchestrator merges worker branches into it sequentially; user tests and reviews the batch branch as a whole before merging to main. Surfaces intra-batch conflicts before they reach main.

**Branch management failures observed (2026-03-23, Batches B–C):**

1. **Local main staleness** — Batch B was pushed to `origin/main` via `git push origin HEAD:main` from a feature branch. This advanced `origin/main` to `aa24b27` but left local `main` at `15c15c9`. The Batch C agent launched from the stale local `main`, silently missing all Batch B commits. Consequence: `pipeline.py` and `notify.py` were carved from the pre-Batch-B `tasks.py`, introducing regressions in audit log patterns that Batch B had already migrated. Caught by CR, fixed in post-CR commit.
   - **Rule added**: Sync local main (`git pull --ff-only`) before every batch launch. Never push to origin from a feature branch using `HEAD:main`.

2. **`isolation: "worktree"` auto-merges to local branch** — Confirmed again: agents using this parameter commit to the orchestrator's current local branch, not to an isolated feature branch. Batch/x branches are manual fast-forward checkpoints, not isolation boundaries.

3. **`git rebase --continue` clobbers commit messages** — After manual conflict resolution in `tasks.py`, `git rebase --continue` replaced the `#14 refactor:` message with the full diff summary. Attempting to fix it via `git commit --amend` on the wrong HEAD commit (which was actually #19) required `git reset --soft` recovery and two additional commits.
   - **Rule added**: Immediately after `git rebase --continue`, amend the commit message before doing anything else.
- Single-agent batches skip the extra branch — agent's feature branch serves directly.
- Workers signal to the orchestrator, not by opening PRs. No individual agent PRs.
- Regular merge commit when merging batch branch to main (preserves per-agent history).

**Non-obvious decisions:**
- #25 (savepoint correctness fix) leads Batch B's refactor sequence rather than going in Batch A. Rationale: it fixes a race condition in `tasks.py` — the same file that Batch B's mechanical refactors will touch. Fixing it first ensures the refactors inherit correct transaction semantics.
- #27 and #28 (dashboard 404 + delete watch) were batched into a single agent (A5) despite being distinct issues, because they both touch `dashboard/routes.py`. Batching eliminated a merge conflict risk within Batch A.
- #16 (event constants) scored 13/15 — highest in the backlog — because it is a prerequisite for #18 (audit helper) and eliminates silent audit-log typo bugs across 8 files.
- The critical path (Batches B→C→D→E) runs through `tasks.py`. All four batches are single-agent sequential because the file accumulates changes from each batch that the next batch must build on.
