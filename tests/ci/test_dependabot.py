"""Pin ``.github/dependabot.yml`` — the bot that proposes action bumps (#283)
and Python dependency bumps (#358) — and the workflow pins it moves (#360).

Dependabot's config fails silently: a mistyped ecosystem, a bad directory or a
missing schedule stops producing PRs without erroring anywhere a person looks,
and a quiet bot reads like "no updates available". Notifier's pattern
(notifier#33):

* **Two ecosystems, ``github-actions`` and ``uv``.** #358 part 1 put co-core on
  the co-pypi index, which Dependabot's updater reads over OIDC; part 2 added
  the ``uv`` block.
* **Every exact pin is ignored by name.** co-core's ``==`` pins move when
  Processor coordinates a contract change, not on a bot's schedule, so the
  ``ignore:`` list *is* the ``==`` names in ``[project].dependencies``: a new
  exact pin can't be bumped by accident. A name-only entry ignores every
  version, and the updater then drops the package before any version lookup.
* **``increase-if-necessary``.** The default ``auto`` resolves to ``widen``,
  which uv can't do (dependabot-core#15290).
* **Dev tools are one grouped PR; runtime packages are one PR each.**
* **The open-PR limit outlasts a full batch.** Past the limit (default 5)
  Dependabot opens nothing and says so nowhere (notifier#103). The bounds are
  derived — one PR per action from the workflows, one per runtime package not
  ignored plus one per group from ``pyproject.toml`` — never transcribed.
* **Commits match AGENTS.md's convention**, because a Dependabot PR lands by
  fast-forwarding its commit onto ``main`` (docs/DEPENDENCIES.md → *Dependabot
  PRs*).
* **Every ``uses:`` pins a commit SHA with its version comment** (#360).
  Dependabot proposes refs in the pin's own form, so a floating major goes
  silent once upstream stops tagging majors (setup-uv after v7), and a moved
  tag is the tj-actions attack. The comment is what Dependabot reads and
  rewrites alongside the SHA.
* **setup-uv states ``prune-cache: true``**, since v9 flipped its default.

Pure file reads — no network, no database.
"""

import re
import tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / ".github" / "dependabot.yml"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PYPROJECT = REPO_ROOT / "pyproject.toml"

ECOSYSTEMS = ["github-actions", "uv"]

# AGENTS.md → Conventions → Commit Messages.
COMMIT_TYPES = {"feat", "fix", "refactor", "docs", "test", "chore"}

# `owner/repo@ref` or `owner/repo/path@ref`: what the github-actions updater bumps.
_ACTION = re.compile(r"^([\w.-]+/[\w.-]+)(?:/[^@]*)?@.+$")

# A `uses:` line as written, comment included — the parsed YAML drops comments.
_USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
# `<40-hex SHA> # vX.Y.Z`, the form Dependabot keeps in step.
_SHA_PIN = re.compile(r"^[^@]+@[0-9a-f]{40}$")
_VERSION_COMMENT = re.compile(r"^\s+# v\d+\.\d+\.\d+$")


def _load() -> dict:
    """The parsed config, or an empty document while the file is missing.

    Tolerating absence keeps collection alive, so the failure surfaces as
    ``test_config_exists``'s message rather than a collection error.
    """
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) if CONFIG.is_file() else {}


def _updates() -> list[dict]:
    return _load().get("updates", [])


def _directories(block: dict) -> set[str]:
    """A block's directories, whichever of the two spellings it uses."""
    return set(block.get("directories", [])) | (
        {block["directory"]} if "directory" in block else set()
    )


def _uses(node: object) -> list[str]:
    """Every ``uses:`` value in a parsed workflow, at any depth."""
    if isinstance(node, dict):
        found = [node["uses"]] if isinstance(node.get("uses"), str) else []
        return found + [u for value in node.values() for u in _uses(value)]
    if isinstance(node, list):
        return [u for item in node for u in _uses(item)]
    return []


def _actions() -> set[str]:
    """The distinct ``owner/repo`` actions the workflows pin — one PR each."""
    refs = [
        u
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        for u in _uses(yaml.safe_load(path.read_text(encoding="utf-8")))
    ]
    return {m.group(1) for ref in refs if (m := _ACTION.match(ref))}


def _uses_lines() -> list[tuple[str, int, str, str]]:
    """Every ``uses:`` line in the workflows: (file, line, ref, rest of line)."""
    return [
        (path.name, n, m.group(1), m.group(2))
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if (m := _USES_LINE.match(line))
    ]


def _is_pinned(ref: str, rest: str) -> bool:
    """A SHA pin with its version comment, or a ``./`` action in this repo."""
    return ref.startswith("./") or bool(_SHA_PIN.match(ref) and _VERSION_COMMENT.match(rest))


def test_config_exists() -> None:
    assert CONFIG.is_file(), (
        "no .github/dependabot.yml — nothing proposes the action bumps SHA pins "
        "hide (#283), or the Python bumps the caps hide from `uv lock` (#358)"
    )


def test_config_is_version_2() -> None:
    """Version 1 configs are dead; Dependabot ignores them without erroring."""
    assert _load().get("version") == 2


def test_exactly_the_two_ecosystems_are_configured() -> None:
    ecosystems = sorted(u.get("package-ecosystem") for u in _updates())
    assert ecosystems == ECOSYSTEMS, (
        f"ecosystems {ecosystems}: expected one block each for {ECOSYSTEMS} "
        "(#283's action bumps, #358's Python dependency bumps)"
    )


def _block(ecosystem: str) -> dict:
    blocks = [u for u in _updates() if u.get("package-ecosystem") == ecosystem]
    assert len(blocks) == 1, f"expected one {ecosystem} block, found {len(blocks)}"
    return blocks[0]


@pytest.fixture
def actions_block() -> dict:
    return _block("github-actions")


@pytest.fixture
def uv_block() -> dict:
    return _block("uv")


def _runtime_requirements() -> list[Requirement]:
    """``[project].dependencies``: the runtime packages, one PR each."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    return [Requirement(r) for r in project["dependencies"]]


def _exact_pins() -> set[str]:
    """The runtime names pinned ``==``: bumped by a coordinated change, never the bot."""
    return {
        canonicalize_name(r.name)
        for r in _runtime_requirements()
        if any(s.operator in ("==", "===") for s in r.specifier)
    }


def test_actions_block_covers_the_workflows(actions_block: dict) -> None:
    """``/`` is the spelling that scans ``.github/workflows/``; any other is silence."""
    assert _directories(actions_block) == {"/"}


def test_actions_block_runs_weekly(actions_block: dict) -> None:
    """No interval is a config Dependabot rejects — visibly only in the repo's
    Dependabot tab, which nobody watches."""
    assert actions_block.get("schedule", {}).get("interval") == "weekly"


def test_open_pr_limit_outlasts_a_full_batch(actions_block: dict) -> None:
    actions = _actions()
    assert actions, "found no `uses:` in .github/workflows/ — the derivation broke"
    limit = actions_block.get("open-pull-requests-limit", 5)
    assert limit > len(actions), (
        f"limit {limit} can be filled by {len(actions)} action PRs ({sorted(actions)}); "
        "past it Dependabot opens nothing and says so nowhere. Raise "
        "open-pull-requests-limit"
    )


@pytest.mark.parametrize("ecosystem", ECOSYSTEMS)
def test_commits_follow_the_repo_convention(ecosystem: str) -> None:
    """The PR's own commit lands on ``main`` unchanged, so it must read like ours."""
    prefix = _block(ecosystem).get("commit-message", {}).get("prefix")
    assert prefix in COMMIT_TYPES, (
        f"commit-message.prefix {prefix!r}: Dependabot's default 'Bump …' subject "
        f"matches none of AGENTS.md's types {sorted(COMMIT_TYPES)}"
    )


def test_uv_block_covers_the_project(uv_block: dict) -> None:
    """``/`` holds ``pyproject.toml`` and ``uv.lock``; any other directory is silence."""
    assert _directories(uv_block) == {"/"}


def test_uv_block_runs_weekly(uv_block: dict) -> None:
    """Weekly, like the actions block: each runtime bump costs the host a sync
    and a restart (docs/DEPENDENCIES.md → *Dependabot PRs*)."""
    assert uv_block.get("schedule", {}).get("interval") == "weekly"


def test_uv_strategy_is_not_the_unsupported_widen(uv_block: dict) -> None:
    """The default ``auto`` resolves to ``widen``, which uv can't do
    (dependabot-core#15290): the run fails in a log nobody reads."""
    assert uv_block.get("versioning-strategy") == "increase-if-necessary"


def test_uv_dev_tools_are_one_grouped_pr(uv_block: dict) -> None:
    """One group, for development dependencies only: a runtime bump deserves
    its own PR, and its own restart."""
    assert uv_block.get("groups") == {"dev-tools": {"dependency-type": "development"}}


def test_uv_ignores_exactly_the_exact_pins(uv_block: dict) -> None:
    """co-core's ``==`` pins move when Processor coordinates a contract change
    (#366), so the bot must never propose one. Deriving the list from
    ``pyproject.toml`` means a new exact pin is ignored or this fails."""
    pins = _exact_pins()
    assert pins, "found no `==` pin in [project].dependencies — the derivation broke"
    entries = uv_block.get("ignore", [])
    # As written, not canonicalized: whether Dependabot normalizes a
    # `dependency-name` before matching is unverified, so spell it canonically.
    names = [e.get("dependency-name", "") for e in entries]
    assert sorted(names) == sorted(pins), (
        f"ignore: {sorted(names)}, but the exact pins are {sorted(pins)}; each "
        "`==` runtime pin needs a name-only ignore entry, spelled canonically "
        "(lowercase, hyphens), and nothing else does"
    )
    narrowed = [e for e in entries if set(e) != {"dependency-name"}]
    assert not narrowed, (
        f"{narrowed}: an ignore narrowed by `versions` or `update-types` lets the "
        "bot propose the rest; a name-only entry ignores every version"
    )


def test_uv_open_pr_limit_outlasts_a_full_batch(uv_block: dict) -> None:
    """One PR per runtime package the bot may move, plus one per group. If that
    many can be due at once and the limit is lower, the rest go silent
    (notifier#103)."""
    ignored = _exact_pins()
    runtime = {canonicalize_name(r.name) for r in _runtime_requirements()} - ignored
    assert runtime, "found no runtime dependency to bump — the derivation broke"
    groups = len(uv_block.get("groups", {}))
    limit = uv_block.get("open-pull-requests-limit", 5)
    assert limit > len(runtime) + groups, (
        f"limit {limit} can be filled by {len(runtime)} runtime PRs and {groups} "
        "group PRs; past it Dependabot opens nothing and says so nowhere. Raise "
        "open-pull-requests-limit"
    )


def test_every_uses_line_is_read() -> None:
    """The line scan sees every ``uses:`` the parser does, so none escapes the pin rule."""
    parsed = sorted(
        u
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        for u in _uses(yaml.safe_load(path.read_text(encoding="utf-8")))
    )
    assert sorted(ref for _, _, ref, _ in _uses_lines()) == parsed, (
        "the line scan and the YAML parser disagree on the `uses:` refs: a quoted "
        "ref, a flow-style step or a `uses:` line inside a `run:` block. Write each "
        "step's `uses:` unquoted, on its own line, so the pin rule can read it"
    )


def test_every_action_is_sha_pinned_with_its_version() -> None:
    bad = [
        f"{name}:{n}: {ref}{rest}"
        for name, n, ref, rest in _uses_lines()
        if not _is_pinned(ref, rest)
    ]
    assert not bad, (
        "pin each action as `owner/action@<40-hex sha> # vX.Y.Z` (#360): a tag "
        "moves, and a floating major goes stale once upstream stops publishing "
        "it. A re-rendered context-cadence.yml reverts its pin (skills#373). "
        "Offending lines:\n  " + "\n  ".join(bad)
    )


def _steps_using(action: str) -> list[dict]:
    """Every workflow step whose ``uses:`` names ``action``."""
    return [
        step
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        for job in yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs", {}).values()
        for step in job.get("steps", [])
        if step.get("uses", "").split("@")[0] == action
    ]


def test_setup_uv_keeps_pruning_its_cache() -> None:
    """setup-uv v9 flipped ``prune-cache`` to ``false``; stating it keeps v5–v7's
    behaviour, so the Actions cache doesn't grow on a bump nobody read (#360).
    A step that turns the cache off (the lock audit, #397) has none to prune."""
    steps = _steps_using("astral-sh/setup-uv")
    assert steps, "no setup-uv step found — the derivation broke"
    cached = [s for s in steps if s.get("with", {}).get("enable-cache") is not False]
    assert cached, "no setup-uv step keeps a cache — the derivation broke"
    assert all(s.get("with", {}).get("prune-cache") is True for s in cached)


def test_a_local_action_needs_no_sha() -> None:
    """``./`` actions live in this repo: no tag to move, nothing to pin."""
    assert _is_pinned("./.github/actions/setup", "")
    assert not _is_pinned("actions/checkout@v7", "")
    assert _is_pinned("actions/checkout@" + "0" * 40, " # v7.0.1")
