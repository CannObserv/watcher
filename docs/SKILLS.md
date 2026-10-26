# Agent Skills

This project follows the [agentskills.io](https://agentskills.io) spec.

## Directory Layout

Two directories serve different discovery systems:

| Directory | Discovery system | Contents |
|---|---|---|
| `skills/` | agentskills.io | Committed overrides + symlinks → `skills-vendor/` |
| `.claude/skills/` | Claude Code | Symlinks → `../../skills/<name>` |

Local overrides in `skills/` automatically shadow vendor skills in both systems. When adding a skill, always create both the `skills/<name>` entry and `.claude/skills/<name>` symlink — [tests/test_skill_links.py](../tests/test_skill_links.py) fails on either half missing; the doctor sees only links that dangle.

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
| `brainstorming` | brainstorm, design this, let's design. Vendored as-is: its own description claims all creative work, and `AGENTS.md` narrows that and overrides its handoff (*Design work* below) |
| `writing-plans` | write plan, implementation plan |
| `writing-skills` | write skill, new skill, author skill |
| `systematic-debugging` | any bug, test failure, unexpected behavior |
| `verification-before-completion` | before any completion claim or commit |
| `test-driven-development` | before writing implementation code |
| `subagent-driven-development` | dispatch agents for plan execution |
| `dispatching-parallel-agents` | 2+ independent tasks in parallel |
| `using-git-worktrees` | feature work needing isolation |
| `managing-skills` | add skill repo, manage external skills |
| `orchestrating-issue-backlog` | orchestrate backlog, prioritize issues, plan issue execution, clear backlog — parallel agent team; serial burn-downs are `coordinating-epics` |
| `curating-context` | curate context, context budget, hone AGENTS.md, trim AGENTS.md, prune context |
| `coordinating-epics` | coordinate the epic, burn down, check-in, I finished #N, next issue, what's next on the epic. Every epic body edit goes through its `epic-edit.sh`, each check-in starts with `scan.sh` (#384). v1's hand steps: a cross-repo parent's (#368) other repos, since `scan.sh` scans one (gregoryfoster/skills#400), and CI by job on the final SHA (gregoryfoster/skills#394) |
| `using-mayfly-chat` | mayfly, open a channel, join the channel, chat with `<repo>`, agent chat, a mayfly.chat URL handed over. Needs Node.js 18+. **Never commit a channel URL** — it is read, write and delete access, and this repo has no structural test for it; run the leak check in the skill's `references/security.md` before committing anything a session produced |
| `patching-hosts` | patch the host, OS updates, security updates, apply updates, unattended-upgrades, needrestart, is this host patched. Reads `.skills/patching-hosts` (#382). **Never from a Shelley session for an apply or a reboot**: `shelley.service` is `KillMode=process`, so `reboot-chain.sh` ends the session driving it (#399) — use SSH or VS Code |
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
**not** mean copying the vendor tree: the override forks `SKILL.md` alone and symlinks every
sibling asset back to `skills-vendor/`, so upstream fixes to the parts that were never
customized arrive with a submodule bump. Each override records its base commit in a
`<!-- forked from <vendor>@<sha> -->` marker directly under the frontmatter — that marker is
what makes the next refresh a 3-way diff instead of a guess.
[tests/test_fork_markers.py](../tests/test_fork_markers.py) holds it to that: every non-symlinked
`skills/*/SKILL.md` must carry a marker, and its SHA must resolve in that vendor's submodule and
be an ancestor of the pinned commit. Falling *behind* the pin is fine — that is sync debt, paid
down on the fork's own schedule; a stamp that names no commit, or one ahead of the pin, is a
marker that cannot be diffed against.

**The same commit is recorded twice, and a re-sync bumps both** (#301, #303). The marker is for
the reader; `synced-from: "<repo> <tag> (<commit>)"` in the frontmatter metadata is what
`.skills/doctor.sh` reads, and it is the doctor's *second* drift comparison — a diff of that
commit against the vendor now, scoped to the override's own real files. It is the only
comparison that can see a vendor `SKILL.md` change that carried no `version:` bump, which is
most of them (gregoryfoster/skills#286); an `obra/superpowers` fork would have it alone, since
that repo ships no `version:`. Leaving `synced-from:` at the old
commit re-reports drift that was just paid down; dropping it gives up the comparand entirely,
and the doctor reports an override nothing can compare as the same failure as no drift
detection at all.

| Skill | Override reason |
|---|---|
| `shipping-work-python-fastapi` | `SKILL.md` only: watcher commit convention in Step 2 and Step 1.5 naming watcher's `.skills/` tailoring. Step 1's `skill-scripts` block is upstream's, verbatim: it resolves each script on its own, so [scripts/pre-ship.sh](../scripts/pre-ship.sh) wins for `pre-ship.sh` alone and the other five fall through to the skill directory — a one-directory block sent them to `scripts/` and exit 127 (#320), which [tests/test_shipping_work_override.py](../tests/test_shipping_work_override.py) now runs the block to catch. All six `scripts/` entries are vendor symlinks — the ship gate is **not** forked |

**The ship gate's env loading lives outside the skill.** [scripts/pre-ship.sh](../scripts/pre-ship.sh)
is watcher's wrapper, in the location upstream's Step 1 resolution loop probes first for that script. It sources
[scripts/load-env.sh](../scripts/load-env.sh) — the shared loader the whole repo now uses, which
parses each env file rather than sourcing it, so a secrets file cannot execute and a malformed
line cannot decide whether the gate runs — then runs the vendored gate through the `skills/`
symlink and, once it passes, `pytest -m integration` plus `scripts/check_no_skips.py`
(#353): the vendored gate deselects that mark, and here the local gate is the last one before
deploy. A wrapper-side addition, not a gate change — a gate change belongs in
gregoryfoster/skills. Forking the gate to add those lines is the failure mode this replaced: the fork stops
receiving upstream fixes without saying so.

**No `.skills/pre-ship-uv-args`, deliberately** (#318). Vendor 1.5 reads extra `uv run`
arguments from that file ([gregoryfoster/skills#304](https://github.com/gregoryfoster/skills/issues/304))
for a project whose tests need a non-default group (`--group seed`). Watcher's only
`[dependency-groups]` entry is `dev`, which uv includes by default, so the file would be empty.
Add one only when a test starts importing from a group uv does not install by default —
and teach `scripts/pre-ship.sh` to pass the same arguments to its integration run first:
until it does, the wrapper exits 2 while the file exists (#353).

**The doc spot-check is tailored, not forked** (#281). `doc-check.sh` matches its
sensitive-path entries against whole path *segments* at any depth
([gregoryfoster/skills#252](https://github.com/gregoryfoster/skills/issues/252)) — under the
old start-of-path matching a nested layout reported `No sensitive paths changed` and exited 0,
a miss byte-identical to a pass. Two committed files replace its built-in defaults wholesale:

| File | Role |
|---|---|
| [.skills/doc-sensitive-paths](../.skills/doc-sensitive-paths) | What Step 1.5 watches. Drops the three defaults that match nothing here (`schema.sql`, `src/models/`, `.env.example`), adds `scripts/`, `src/dashboard/`, `src/workers/`, `tools/` |
| [.skills/doc-sections](../.skills/doc-sections) | The advice printed on a hit — watcher's own docs, each line naming the change that triggers it |

Tailor them together: the list says what the gate watches and the sections say what to do about
a hit, so tailoring only the list leaves advice written for someone else's stack
([gregoryfoster/skills#261](https://github.com/gregoryfoster/skills/issues/261)).
`deploy/` reaching `tests/deploy/`, and `scripts/` reaching `tests/scripts/` and the vendored
skill symlinks, are kept over-matches — for a spot-check that exits 1 and asks a human to look,
over-matching is the cheap failure, and narrowing either means listing files individually, which
is the under-match #252 fixed. Both files are guarded by
[tests/test_doc_sensitive_paths.py](../tests/test_doc_sensitive_paths.py): an entry matching no
tracked file fails, as does advice naming a doc that no longer exists, and a differential test
runs the real `doc-check.sh` over a throwaway repo so the guard's matcher cannot drift from the
gate's.

## Design work

`brainstorming` is `obra/superpowers`' skill, vendored unmodified (#396). It used to be an
override, and every upstream rewrite meant re-applying the same few deltas; they are watcher
preferences, not skill logic, so they now live in `AGENTS.md` → *Agent Skills*, where they hold
whatever the vendored text says. Upstream defers to them on the doc's location ("Your human
partner's preferences override both"); the other two rows are overridden outright:

| Upstream (v7.0.1) | Here |
|---|---|
| Description: use before *any* creative work, including "modifying behavior" | An explicit ask, or a feature with no design yet. An issue body or coordinator brief is already the approved design |
| Design doc in `docs/superpowers/specs/` | `docs/plans/YYYY-MM-DD-<topic>-design.md`, committed as `#<n> docs:` on the issue's branch |
| HARD-GATE: after approval, "invoke superpowers:writing-plans and no other skill" | A project's approved doc → GitHub issue → `using-git-worktrees`; `writing-plans` optional. There is no `superpowers:` namespace here — `writing-plans` is the gregoryfoster skill |

Upstream sizes work as *a small change* (short doc, no builder check) or *a project* (full doc
plus a builder-check subagent). The issue-and-worktree handoff is for a project; a small change
goes straight to TDD.

### The visual companion on this VM

The vendored guide, `visual-companion.md`, is written for a local machine. Four things differ
here; the first fails the launch, the next two leave the tab unreachable, the last leaks the key
or opens nothing:

1. **Script path.** The guide writes `bash scripts/start-server.sh`; from the repo root that is
   watcher's own `scripts/` — the cwd trap of
   [gregoryfoster/skills#63](https://github.com/gregoryfoster/skills/issues/63). Use
   `skills/brainstorming/scripts/start-server.sh`; it derives `SCRIPT_DIR` from `$0`, so the
   symlink is safe.
2. **Bind and port.** The default is `127.0.0.1` on a random port in 49152–65535; the exe.dev
   proxy forwards only 3000–9999, and not to a loopback bind. Pass `--host 0.0.0.0` and a
   `BRAINSTORM_PORT` in range — not 8000 (systemd) or 8001 (dev server).
3. **URL host.** `--url-host co-watcher.exe.xyz`, not the guide's `localhost` example.
4. **No `--open`** — there is no browser on the VM. Hand the user the emitted `url` verbatim: its
   `?key=` is the session credential, and the server answers 403 without it, which is what keeps
   the `0.0.0.0` bind from being an open door.

```bash
BRAINSTORM_PORT=7391 bash skills/brainstorming/scripts/start-server.sh \
  --project-dir "$PWD" --host 0.0.0.0 --url-host co-watcher.exe.xyz
```

`--project-dir` writes the session, `.last-token` included, under `.superpowers/brainstorm/`;
`.gitignore` covers it, guarded by
[tests/test_brainstorm_session_is_not_committable.py](../tests/test_brainstorm_session_is_not_committable.py).
Reachability through the proxy cannot be checked from the VM's own shell — curl fails the same
way against port 8000, which is serving fine — so confirm with the user that the tab loaded.

## SocratiCode (Codebase Search)

This project is indexed into the cohort's **shared store on `co-index`**, not a local one.
The goal→tool table, per-tool notes and index scope are in
**[SOCRATICODE.md](SOCRATICODE.md)** — generated by `init-socraticode` above its END marker,
this repo's own notes below it; the client contract and its green-failing traps are
[CO-INDEX.md](CO-INDEX.md)'s. The prefetch query is not copied
anywhere: the reminder hook below prints it, and a transcription drifts silently
(gregoryfoster/skills#209).

### SessionStart hooks

Three hooks are registered in `.claude/settings.json`, each a symlink into
`skills-vendor/` so an upstream fix arrives on the normal submodule refresh
rather than needing a re-install. Install and audit them through the vendored
installer — never by hand:

| Hook | Marker | What it does |
|---|---|---|
| `socraticode-reminder.sh` | `socraticode-prefetch` | Prints the `ToolSearch` prefetch query, every session |
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

**The health hook reports; it never repairs.** No re-index, no file edit, no
config rewrite — it runs before an agent has context. Findings land on stdout
and in `.git/socraticode-health.log` (tail-bounded to 200 lines); act on them
with `codebase_index` or by re-running `init-socraticode`.

**It costs one session start per UTC day.** Measured here at **~8s** against a
warm MCP server; a cold start pays for the pinned server's launch on top, bounded by
the `HEALTH_TIMEOUT_MS=60000` ceiling the hook exports. A day-stamped lock in
`.git/` means every other session that day is free. If a session start stalls
for several seconds with no output, this is why — it is not hung.

The hook's own output carries the reports-never-repairs caveat on every path
that prints a finding, so the warning always arrives attached to the thing it
qualifies. `AGENTS.md` states the rule too, in one clause under *Code Exploration
Policy* — an agent that reads a finding at session start should not re-index on
its say-so. The two are deliberate belt-and-braces: the hook's line cannot drift
from the hook, and the policy line arrives even in a session where the hook is
silent.

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

Upstream reference: [giancarloerra/socraticode#agent-instructions](https://github.com/giancarloerra/socraticode#agent-instructions)

## Authoring New Skills

Follow the `writing-skills` TDD cycle:
1. **RED** — run pressure scenarios without the skill; document where the agent fails
2. **GREEN** — write a minimal SKILL.md addressing those failures
3. **REFACTOR** — find new rationalizations, close loopholes, re-test

New project-specific skills go in `skills/<name>/` with a `.claude/skills/<name>` symlink to `../../skills/<name>`. Cross-project skills belong in `gregoryfoster/skills`.
