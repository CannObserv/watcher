# Agent Skills

This project follows the [agentskills.io](https://agentskills.io) spec.

## Directory Layout

Two directories serve different discovery systems:

| Directory | Discovery system | Contents |
|---|---|---|
| `skills/` | agentskills.io | Committed overrides + symlinks → `vendor/` |
| `.claude/skills/` | Claude Code | Symlinks → `../../skills/<name>` |

Local overrides in `skills/` automatically shadow vendor skills in both systems. When adding a skill, always create both the `skills/<name>` entry and `.claude/skills/<name>` symlink.

## External Skill Repos (Git Submodules)

| Repo | Submodule path |
|---|---|
| [`gregoryfoster/skills`](https://github.com/gregoryfoster/skills) | `vendor/gregoryfoster-skills/` |
| [`obra/superpowers`](https://github.com/obra/superpowers) | `vendor/obra-superpowers/` |

Init after cloning: `git submodule update --init --recursive`

Submodule freshness auto-enforced by `UserPromptSubmit` hook in `.claude/settings.json`. Force-refresh: `git submodule update --remote --merge vendor/gregoryfoster-skills vendor/obra-superpowers`

To add a new external skill repo: follow the `managing-skills-claude` skill.

## Available Skills

| Skill | Source | Triggers |
|---|---|---|
| `reviewing-code-claude` | Local override | CR, code review, perform a review |
| `reviewing-architecture-claude` | `gregoryfoster-skills` | AR, architecture review, architectural review |
| `shipping-work-claude` | Local override | ship it, push GH, close GH, wrap up |
| `brainstorming` | Local override | brainstorm, design this, let's design |
| `systematic-debugging` | `obra-superpowers` | description-driven¹ |
| `verification-before-completion` | `obra-superpowers` | description-driven¹ |
| `test-driven-development` | `obra-superpowers` | description-driven¹ |
| `writing-plans` | Local override | write plan, implementation plan |
| `writing-skills` | `obra-superpowers` | write skill, new skill, author skill |
| `subagent-driven-development` | `obra-superpowers` | subagent dev, dispatch agents |
| `dispatching-parallel-agents` | `obra-superpowers` | parallel agents |
| `using-git-worktrees` | `obra-superpowers` | set up worktree, create worktree |
| `managing-skills-claude` | `gregoryfoster-skills` | add skill repo, add external skills, manage skills |

¹ Description-driven: `systematic-debugging` on any bug/test failure; `verification-before-completion` before any completion claim or commit; `test-driven-development` before writing implementation code.

## Local Overrides

A committed directory in `skills/` completely supersedes the vendor version (no inheritance). Must be fully self-contained.

| Skill | Override reason |
|---|---|
| `reviewing-code-claude` | FastAPI-specific review dimensions; ruff lint check; TDD discipline; Iron Law + rationalization-prevention table; Phase 3.5 verification gate |
| `shipping-work-claude` | `uv run pytest --no-cov` + `uv run ruff check` in pre-ship.sh; `#<n> [type]: <desc>` commit convention; Iron Law + HARD-GATE on partial issue closure |
| `brainstorming` | Project conventions (docs/plans/ path, commit format); invokes using-git-worktrees after design approval; FastAPI stack context; proactive-suggestion mode |
| `writing-plans` | Plans saved to `docs/plans/` (vendor default is `docs/superpowers/plans/`) |

## Authoring New Skills

Follow the `writing-skills` TDD cycle:
1. **RED** — run pressure scenarios without the skill; document where the agent fails
2. **GREEN** — write a minimal SKILL.md addressing those failures
3. **REFACTOR** — find new rationalizations, close loopholes, re-test

New project-specific skills go in `skills/<name>/` with a `.claude/skills/<name>` symlink to `../../skills/<name>`. Cross-project skills belong in `gregoryfoster/skills`.
