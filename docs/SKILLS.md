# Agent Skills

This project follows the [agentskills.io](https://agentskills.io) spec.

## Directory Layout

Two directories serve different discovery systems:

| Directory | Discovery system | Contents |
|---|---|---|
| `skills/` | agentskills.io | Committed overrides + symlinks → `skills-vendor/` |
| `.claude/skills/` | Claude Code | Symlinks → `../../skills/<name>` |

Local overrides in `skills/` automatically shadow vendor skills in both systems. When adding a skill, always create both the `skills/<name>` entry and `.claude/skills/<name>` symlink.

## External Skill Repos (Git Submodules)

| Repo | Submodule path |
|---|---|
| [`gregoryfoster/skills`](https://github.com/gregoryfoster/skills) | `skills-vendor/gregoryfoster-skills/` |
| [`obra/superpowers`](https://github.com/obra/superpowers) | `skills-vendor/obra-superpowers/` |

Init after cloning: `git submodule update --init --recursive`

Submodule freshness auto-enforced by the `skills-submodule-update` `SessionStart` hook registered in `.claude/settings.json`. Force-refresh: `git submodule update --remote --merge skills-vendor/gregoryfoster-skills skills-vendor/obra-superpowers`

To add a new external skill repo: follow the `managing-skills` skill.

## Skill Triggers

| Skill | Triggers / when to invoke |
|---|---|
| `reviewing-code-python-fastapi` | CR, code review |
| `reviewing-architecture` | AR, architecture review |
| `enforcing-architecture` | add a fitness function, enforce this contract, lock this rule (delegated to by `reviewing-architecture` on a `fitness` directive) |
| `shipping-work-python-fastapi` | ship it, push GH, close GH, wrap up |
| `brainstorming` | brainstorm, design this, let's design |
| `writing-plans` | write plan, implementation plan |
| `writing-skills` | write skill, new skill, author skill |
| `systematic-debugging` | any bug, test failure, unexpected behavior |
| `verification-before-completion` | before any completion claim or commit |
| `test-driven-development` | before writing implementation code |
| `subagent-driven-development` | dispatch agents for plan execution |
| `dispatching-parallel-agents` | 2+ independent tasks in parallel |
| `using-git-worktrees` | feature work needing isolation |
| `managing-skills` | add skill repo, manage external skills |
| `init-socraticode` | init socraticode, set up code search, index this project, audit the SocratiCode install |
| `socraticode` (codebase MCP) | see **Code Exploration Policy** above |

**Code Exploration Policy** is the `AGENTS.md` section of that name — the negative
rule and the prefetch query. The tool-by-tool guidance is **SocratiCode (Codebase
Search)** below.

## Skill Sources

The trigger list is **Skill Triggers** above. Each project skill is sourced from one of:

| Source | Path | Notes |
|---|---|---|
| Local override | `skills/<name>/` | Committed in this repo; supersedes any vendor copy of the same name |
| `gregoryfoster/skills` | `skills-vendor/gregoryfoster-skills/` | Cross-project skills authored for Cannabis Observer |
| `obra/superpowers` | `skills-vendor/obra-superpowers/` | Upstream community skills |

Description-driven skills (`systematic-debugging`, `verification-before-completion`, `test-driven-development`) activate from their description field rather than an explicit trigger phrase — they fire on any bug/test failure, before any completion claim, and before writing implementation code respectively.

## Local Overrides

A committed directory in `skills/` completely supersedes the vendor version — there is no
inheritance, so whatever the directory does not provide, the skill does not have. That does
**not** mean copying the vendor tree: both overrides fork `SKILL.md` alone and symlink every
sibling asset back to `skills-vendor/`, so upstream fixes to the parts that were never
customized arrive with a submodule bump. Each override records its base commit in a
`<!-- forked from <vendor>@<sha> -->` marker directly under the frontmatter — that marker is
what makes the next refresh a 3-way diff instead of a guess.

| Skill | Override reason |
|---|---|
| `shipping-work-python-fastapi` | `SKILL.md` only: watcher commit convention in Step 2, and Step 1 pointed at [scripts/pre-ship.sh](../scripts/pre-ship.sh). All six `scripts/` entries are vendor symlinks — the ship gate is **not** forked |
| `brainstorming` | `SKILL.md` only: `docs/plans/` path, `#<n> [type]:` commit format, a GitHub issue on the architectural path, `writing-plans` optional rather than mandatory, `using-git-worktrees` after design approval, TDD as the bounded path's workflow, and the exe.dev proxy note for the visual companion's port. `visual-companion.md`, `spec-document-reviewer-prompt.md` and `scripts/` are vendor symlinks |

**The ship gate's env loading lives outside the skill.** [scripts/pre-ship.sh](../scripts/pre-ship.sh)
is watcher's wrapper, in the location upstream's Step 1 resolution loop probes first. It sources
[scripts/load-env.sh](../scripts/load-env.sh) — the shared loader the whole repo now uses, which
parses each env file rather than sourcing it, so a secrets file cannot execute and a malformed
line cannot decide whether the gate runs — then `exec`s the vendored gate through the `skills/`
symlink. Forking the gate to add those lines is the failure mode this replaced: the fork stops
receiving upstream fixes without saying so.

## SocratiCode (Codebase Search)

This project is indexed with SocratiCode. Always use its MCP tools to explore the codebase before reading files directly.

**Core principle: search before reading.** The index gives you a map of the codebase in milliseconds; raw file reading is expensive and context-consuming.

### Workflow

1. **Start most explorations with `codebase_search`.** Hybrid semantic + keyword (vector + BM25, RRF-fused) in a single call. Broad queries for orientation ("how is auth handled"), precise queries for symbol lookup. **Use grep instead** when you already know the exact identifier, error string, or regex pattern.
2. **Follow the graph before following imports.** Use `codebase_graph_query` to see what a file imports and what depends on it before opening it. Check dependents before modifying or deleting.
3. **Use Impact Analysis BEFORE refactoring/renaming/deleting.** Symbol-level call graph (`codebase_impact`, `codebase_flow`, `codebase_symbol`, `codebase_symbols`) goes deeper than the file graph — it knows which functions call which.
4. **Read files only after narrowing via search.** Never read a file just to find out if it's relevant.
5. **Use `codebase_graph_circular`** when debugging unexpected behavior or import-related errors.
6. **Check `codebase_status`** if search returns no results — the project may not be indexed yet.
7. **Leverage context artifacts** for non-code knowledge (DB schemas, API specs, infra configs). Run `codebase_context` early; use `codebase_context_search` for specific schemas/endpoints.

### When to use each tool

| Goal | Tool |
|------|------|
| Understand what a codebase does / where a feature lives | `codebase_search` (broad query) |
| Find a specific function, constant, or type | `codebase_search` (exact name) or grep if you know the exact string |
| Find exact error messages, log strings, or regex patterns | grep / ripgrep |
| See what a file imports or what depends on it | `codebase_graph_query` |
| Check blast radius before modifying or deleting a file | `codebase_impact` (symbol-level) or `codebase_graph_query` (file-level) |
| What breaks if I change function X? | `codebase_impact target=X` |
| What does this entry point actually do? | `codebase_flow entrypoint=X` |
| List entry points in this codebase | `codebase_flow` (no args) |
| Who calls this function and what does it call? | `codebase_symbol name=X` |
| What functions/classes exist in this file? | `codebase_symbols file=path` |
| Search for symbols by name across the project | `codebase_symbols query=X` |
| Spot architectural problems | `codebase_graph_circular`, `codebase_graph_stats` |
| Visualise module structure | `codebase_graph_visualize` |
| Verify index is up to date | `codebase_status` |
| Discover what project knowledge (schemas, specs, configs) is available | `codebase_context` |
| Find database tables, API endpoints, infra configs | `codebase_context_search` |

> **Keep the connection alive during indexing.** Indexing runs in the background. Some MCP hosts disconnect idle connections. Call `codebase_status` roughly every 60 seconds after starting `codebase_index` until it completes.

### Index scope

**Index scope (#240).** `.socraticodeignore` (repo root, gitignore syntax) keeps the
vendored skill trees — `skills-vendor/` and the `.claude/skills/` symlink farm — out of
the semantic index; vendor prose otherwise outranks this repo's own code in
`codebase_search`. `skills/` stays indexed: it holds the committed first-party overrides,
and its vendor symlinks resolve to `skills-vendor/` paths that are already excluded.
Editing the file only changes what *subsequent* scans pick up — chunks embedded by an
earlier index survive it (vendor hits kept ranking after the file landed). Purging them
takes a clean rebuild: `codebase_remove` then `codebase_index`, which re-embeds the whole
repo — budget a maintenance window for it.

### SessionStart hooks

Three hooks are registered in `.claude/settings.json`, each a symlink into
`skills-vendor/` so an upstream fix arrives on the normal submodule refresh
rather than needing a re-install. Install and audit them through the vendored
installer — never by hand:

| Hook | Marker | What it does |
|---|---|---|
| `socraticode-reminder.sh` | `socraticode-prefetch` | Prints the `ToolSearch` prefetch query below, every session |
| `socraticode-health.sh` | `socraticode-health` | Once-per-UTC-day infra check (#276); silent when clean |
| `skills-submodule-update.sh` | `skills-submodule-update.sh` | Once-per-UTC-day submodule pointer bump, `main` only |

```bash
V=skills-vendor/gregoryfoster-skills/skills
bash $V/managing-skills/scripts/install-hook.sh --hook socraticode-health.sh \
  --skill init-socraticode --marker socraticode-health --copy-fallback
bash $V/managing-skills/scripts/install-hook.sh --hook socraticode-reminder.sh \
  --skill init-socraticode --marker socraticode-prefetch --marker socraticode-reminder \
  --copy-fallback
bash $V/managing-skills/scripts/install-refresh.sh
```

Append `--check` to any of the three to audit without writing: each reports its
symlink and its registration independently and exits 3 if either is missing, or
if the hook is a copy where a vendored source exists to link at instead.

**The health hook reports; it never repairs.** No re-index, no Docker start, no
file edit — it runs before an agent has context. Findings land on stdout and in
`.git/socraticode-health.log` (tail-bounded to 200 lines); act on them with
`codebase_index` or by re-running `init-socraticode`.

**It costs one session start per UTC day.** Measured here at **~8s** against a
warm MCP server; a cold start pays for `npx -y socraticode` on top, bounded by
the `HEALTH_TIMEOUT_MS=60000` ceiling the hook exports. A day-stamped lock in
`.git/` means every other session that day is free. If a session start stalls
for several seconds with no output, this is why — it is not hung.

The hook's own output carries the reports-never-repairs caveat on every path
that prints a finding, so the warning always arrives attached to the thing it
qualifies. That is why `AGENTS.md` says nothing about it: the policy block sat 3
tokens under its 6,000 budget, any clause pushed the file over, and a signal the
hook already emits at the moment of need does not need a standing line in the
file loaded on every invocation.

**Why it exists (#276).** Adding an artifact to `.socraticodecontextartifacts.json`
does not index it, and nothing warns you: `codebase_context_search` simply answers
from the artifacts it has, and `codebase_status` stays `green` while reporting the
shortfall on a line nobody reads. The hook compares the manifest's declared count
against per-artifact status and names any gap. It also re-checks graph edge yield
and a FAILED last operation, so an install that was green in January is not assumed
green in June.

**Registering a hook evicts its group-mates**
([gregoryfoster/skills#222](https://github.com/gregoryfoster/skills/issues/222)).
`install-hook.sh`'s dedupe strips the whole `SessionStart` matcher group whose
*first* command matches the marker, so a sibling hook sharing that group
disappears silently — its symlink survives, so `doctor.sh` still reads healthy.
The mirror case duplicates: a marker matching at index ≥1 is not stripped at all.
Keep one hook per group — the installers append that shape — and after any
install run all three `--check` commands above, not just the one you ran.

### Prefetch query

The `socraticode-reminder.sh` hook above prints this every session.

Prefetch query (run via `ToolSearch` once per session if the SessionStart reminder isn't loaded):

`select:mcp__plugin_socraticode_socraticode__codebase_search,mcp__plugin_socraticode_socraticode__codebase_symbol,mcp__plugin_socraticode_socraticode__codebase_symbols,mcp__plugin_socraticode_socraticode__codebase_flow,mcp__plugin_socraticode_socraticode__codebase_impact,mcp__plugin_socraticode_socraticode__codebase_graph_query,mcp__plugin_socraticode_socraticode__codebase_graph_circular,mcp__plugin_socraticode_socraticode__codebase_graph_stats,mcp__plugin_socraticode_socraticode__codebase_graph_visualize,mcp__plugin_socraticode_socraticode__codebase_status,mcp__plugin_socraticode_socraticode__codebase_context,mcp__plugin_socraticode_socraticode__codebase_context_search`

### Linked Projects

Cross-project search to the sister `notifier` index is enabled via `SOCRATICODE_LINKED_PROJECTS=/home/exedev/notifier` in `.claude/settings.local.json` (gitignored — per-instance config, not a project commitment). The value may be relative (resolved from the project root) or absolute; absolute is recommended since the MCP server's CWD isn't guaranteed across hosts. Pass `includeLinked: true` on `codebase_search` to fan out across both indexes; results carry a `[watcher]` / `[notifier]` label.

Upstream reference: [giancarloerra/socraticode#agent-instructions](https://github.com/giancarloerra/socraticode#agent-instructions)

## Authoring New Skills

Follow the `writing-skills` TDD cycle:
1. **RED** — run pressure scenarios without the skill; document where the agent fails
2. **GREEN** — write a minimal SKILL.md addressing those failures
3. **REFACTOR** — find new rationalizations, close loopholes, re-test

New project-specific skills go in `skills/<name>/` with a `.claude/skills/<name>` symlink to `../../skills/<name>`. Cross-project skills belong in `gregoryfoster/skills`.
