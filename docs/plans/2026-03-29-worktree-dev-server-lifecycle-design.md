# Worktree Dev Server Lifecycle

**Date:** 2026-03-29

## Goal

Codify the operational rule that **port 8001 belongs to worktrees** by adding a
dev server lifecycle step to the `using-git-worktrees` skill. When a worktree is
created, any existing process on 8001 is killed and the dev server restarts from
the new worktree automatically. Port 8001 should never serve code from main.

## Approved Approach

Add a new **Step 6: Start Dev Server** to `skills/using-git-worktrees/SKILL.md`,
immediately after the current Step 5 (Report Location). Update the report format,
reference section, and common mistakes accordingly.

### New Step 6: Start Dev Server

1. Kill any existing process on port 8001
   (`lsof -ti :8001 | xargs kill -9 2>/dev/null`)
2. Start uvicorn from the worktree directory on 8001 with `--reload`, backgrounded
3. Wait briefly, verify port is bound (`ss -tlnp | grep 8001`)
4. Include dev server status in the Step 5 report

Updated report format:

```
Worktree ready at <full-path>
Tests passing (<N> tests, 0 failures)
Dev server running on port 8001 (https://watcher.exe.xyz:8001/)
Ready to implement <feature-name>
```

### Updated Reference Section

Reword "Dev Server in a Worktree" from optional how-to into a statement of the
rule: port 8001 is exclusively for worktree code, managed automatically by Step 6.
Never serve main on 8001.

### New Common Mistake Entry

| Running dev server from main | 8001 belongs to worktrees; never serve main on 8001 | Always start 8001 from worktree, stop when worktree is torn down |

## Key Decisions

- **Always restart + report** — dev server restarts silently; mentioned in report
- **Kill before start** — handles stale servers from previous worktrees
- **Start lives in `using-git-worktrees`** — callers don't need to know about it
- **Teardown is out of scope** — will be handled separately in `shipping-work-claude`

## Out of Scope

- Stopping the dev server on worktree teardown (future: `shipping-work-claude`)
- Changes to AGENTS.md or COMMANDS.md (already document the port split)
- Nginx configuration (not involved in the current setup)
