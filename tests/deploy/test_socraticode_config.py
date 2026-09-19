"""Drift tests for the shared SocratiCode index client config (#300).

Watcher does not host its semantic index. `.socraticode.json` and the `env`
block in `.claude/settings.json` are the whole client contract against the
cohort's shared Qdrant on `co-index`; both are committed so every checkout
addresses the same collections wherever the working tree sits on disk.

Hosting one here was ruled out on measurement, not taste: #307 found this host
at 3.8 GiB with no swap, sharing memory with `watcher.service` and with agent
sessions the OOM killer cannot touch (`oom_score_adj` -1000), while a cold
local index peaks at ~1.2 G pulling two images and an embedding model.

Each assertion below pins a failure mode that reports itself as *green*:

- a missing or malformed `.socraticode.json` degrades to the path-hash project
  id with no message (trap 2);
- `QDRANT_HOST` instead of `QDRANT_URL` builds an https URL against port 16333
  and surfaces as a network fault (trap 3);
- the short MagicDNS name is not in the Qdrant certificate's SAN (trap 4);
- `QDRANT_COLLECTION_PREFIX` and `SOCRATICODE_BRANCH_AWARE` fragment the cohort
  namespace while every health check still says green (trap 5);
- an ungitignored `.claude/settings.local.json` puts the cohort's single Qdrant
  key one `git add -A` from GitHub, which is what happened in CannObserv/broker
  (notifier#68, broker#18).

Upstream design and decisions D0-D14: `docs/plans/2026-09-11-shared-qdrant-vm-design.md`
in CannObserv/notifier, tracked by CannObserv/notifier#57.
"""

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / ".socraticode.json"
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
SETTINGS_LOCAL_NAME = ".claude/settings.local.json"

#: Upstream's own validator: config.js assertValidProjectId rejects anything else.
PROJECT_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

#: The other four cohort repos. A stub naming a collection nobody has indexed
#: yet is skipped per-collection, not fatal, so linking all four is safe.
COHORT_SIBLINGS = {"../archiver", "../broker", "../notifier", "../replicator"}

#: The six client variables, and the only values that reach the shared store.
EXPECTED_ENV = {
    "QDRANT_MODE": "external",
    "QDRANT_URL": "https://index.taild0fb76.ts.net:6333",
    "OLLAMA_MODE": "external",
    "OLLAMA_URL": "http://index:11434",
    "EMBEDDING_MODEL": "nomic-embed-text",
    # 768 is nomic-embed-text's width. A mismatch does not error - it writes
    # vectors Qdrant accepts and nothing can be searched against.
    "EMBEDDING_DIMENSIONS": "768",
}

#: Set either, and the cohort's collections split in a way nothing reports: the
#: prefix is prepended to the instance-global socraticode_metadata collection as
#: well as the per-project ones, and branch-awareness appends the branch name to
#: the project id - a fresh six-collection set, re-indexed from empty, per branch.
NAMESPACE_GUARD_VARS = ["QDRANT_COLLECTION_PREFIX", "SOCRATICODE_BRANCH_AWARE"]

#: Every env surface on this host. The first four are the ones that actually
#: reach the MCP server -- it launches from an agent session, so it inherits
#: whatever `scripts/load-env.sh` put in that shell plus the two `env` blocks
#: Claude Code applies. `deploy/watcher.service` cannot reach it and is here as
#: defence in depth, because a variable set there is still wrong.
#:
#: VM-local files are skipped loudly rather than passing vacuously in CI.
ENV_SURFACE_FILES = [
    Path("/etc/watcher/.env"),
    REPO_ROOT / ".env",
    SETTINGS,
    REPO_ROOT / ".claude" / "settings.local.json",
    REPO_ROOT / "deploy" / "watcher.service",
]


def _surface_id(path: Path) -> str:
    """Disambiguate the two `.env` files.

    `path.name` collides -- /etc/watcher/.env and the repo's .env both render as
    `.env`, so pytest disambiguates them as `.env0`/`.env1` and the failure says
    which ordinal, not which file. This test's whole job is to name the file
    holding a variable that splits the cohort namespace, and telling the
    production env file from the repo one is the most important distinction it
    draws. VM-local paths keep their absolute form; tracked ones go repo-relative.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


@pytest.fixture(scope="module")
def config() -> dict:
    return json.loads(CONFIG.read_text())


@pytest.fixture(scope="module")
def settings_env() -> dict:
    return json.loads(SETTINGS.read_text()).get("env", {})


def test_config_exists_and_parses():
    """A malformed file is ignored by upstream, not reported.

    loadSocratiCodeConfig catches every parse error and returns null, so a typo
    here degrades to the path-hash id in silence - the same silence class as the
    linked-project skip.
    """
    assert CONFIG.exists(), f"{CONFIG.name} is missing"
    json.loads(CONFIG.read_text())


def test_project_id_is_the_repo_name(config):
    """`watcher`, so the collections read as codebase_watcher in a shared store."""
    assert config["projectId"] == "watcher"


def test_project_id_is_qdrant_safe(config):
    """Upstream throws on anything outside [a-zA-Z0-9_-]+ rather than sanitizing."""
    assert set(config["projectId"]) <= PROJECT_ID_CHARS


def test_linked_projects_are_relative(config):
    """Absolute paths are the defect this design replaces (notifier#57).

    Relative entries resolve against the repo root, so the same committed file
    works on every cohort VM. This repo previously linked notifier by absolute
    path through `SOCRATICODE_LINKED_PROJECTS`, from when both services shared
    one box - see test_linked_projects_env_var_is_not_set.
    """
    linked = config["linkedProjects"]
    assert linked, "linkedProjects is empty"
    for entry in linked:
        assert not Path(entry).is_absolute(), f"{entry} is absolute"
        assert entry.startswith("../"), f"{entry} does not name a sibling checkout"


def test_linked_projects_name_the_cohort(config):
    assert set(config["linkedProjects"]) == COHORT_SIBLINGS


def test_linked_projects_exclude_this_repo(config):
    """Upstream drops a self-link, but a self-link in the file is still a mistake."""
    assert f"../{REPO_ROOT.name}" not in config["linkedProjects"]


@pytest.mark.parametrize("path", ENV_SURFACE_FILES, ids=_surface_id)
def test_linked_projects_env_var_is_not_set(path: Path):
    """`SOCRATICODE_LINKED_PROJECTS` unions with the file; it does not shadow it.

    loadLinkedProjects reads `.socraticode.json` *and* the env var into one Set,
    so the variable cannot override a committed entry - which is why the stale
    `/home/exedev/notifier` this repo carried was not a conflict but a no-op:
    the path does not exist here and `fs.existsSync` drops it without a word
    (trap 1). Once the sibling stubs exist it becomes a duplicate of `../notifier`
    instead. Neither state is wrong; both are an absolute path asserting on one
    host what the committed relative entry already says on every host, which is
    the drift the shared design exists to remove.
    """
    if not path.exists():
        pytest.skip(f"{path} not present on this machine")
    declares = "SOCRATICODE_LINKED_PROJECTS" in path.read_text()
    assert not declares, f"{_surface_id(path)} sets SOCRATICODE_LINKED_PROJECTS"


@pytest.mark.parametrize("entry", sorted(COHORT_SIBLINGS))
def test_a_resolvable_sibling_declares_the_right_project_id(entry: str):
    """Resolution is the floor, not the proof — the gap #300 measured.

    `loadLinkedProjects` keeps an entry when the *directory* exists, and
    `searchMultipleCollections` swallows a per-collection miss, so a sibling can
    resolve, be searched, and contribute nothing while `health-check` reports
    `4 of 4 resolved` and the tool result says no more. That is exactly what
    `../archiver` did: a clone predating archiver#226 carried no
    `.socraticode.json`, fell through to the SHA-256 of its path
    (`codebase_7a9d625938ee`, indexed by nobody), and returned zero hits on a
    query aimed squarely at its own domain.

    So this asserts the one thing resolution does not: that a directory which
    *is* there names the collection its basename implies. It is host state, not
    repo state, hence the skip — CI links no siblings, and an absent sibling is
    a legitimate configuration that upstream drops silently by design.
    """
    path = (REPO_ROOT / entry).resolve()
    if not path.is_dir():
        pytest.skip(f"{entry} is not present on this machine")

    config = path / ".socraticode.json"
    expected = path.name
    assert config.is_file(), (
        f"{path} exists but has no .socraticode.json, so it resolves to a path "
        f"hash instead of `{expected}` — searched, skipped, and silent. Either "
        f"pull the checkout (if it is a clone whose repo committed one) or write "
        f'a stub: {{ "projectId": "{expected}" }}'
    )
    declared = json.loads(config.read_text()).get("projectId")
    assert declared == expected, (
        f"{path} declares projectId {declared!r}, so cross-repo hits would come "
        f"from collection codebase_{declared} rather than codebase_{expected}"
    )


@pytest.mark.parametrize("variable", sorted(EXPECTED_ENV))
def test_client_variables_are_committed(variable: str, settings_env: dict):
    """Non-secret and self-documenting, so they travel with the checkout.

    Only `QDRANT_API_KEY` is per-host, and it lives in the gitignored
    settings.local.json - see test_settings_local_is_git_ignored.
    """
    assert settings_env.get(variable) == EXPECTED_ENV[variable]


def test_qdrant_is_addressed_by_url_not_host(settings_env: dict):
    """Trap 3: `QDRANT_HOST` is a trap, not a synonym.

    QDRANT_MODE=external refuses to start without a URL, and the fallback it
    would otherwise build is `${KEY ? https : http}://${QDRANT_HOST}:${QDRANT_PORT}`
    with QDRANT_PORT defaulting to 16333, not 6333. A key with no URL therefore
    assumes https against the wrong port and the error reads like a network fault.
    """
    assert "QDRANT_HOST" not in settings_env
    assert "QDRANT_PORT" not in settings_env


def test_qdrant_url_uses_the_full_magicdns_name(settings_env: dict):
    """Trap 4: the short name is not in the certificate's SAN.

    Qdrant serves TLS because SocratiCode refuses to send QDRANT_API_KEY over a
    non-TLS, non-localhost connection (notifier#57 D14). `https://index:6333`
    therefore fails the handshake; only the full MagicDNS name verifies.
    """
    url = settings_env["QDRANT_URL"]
    assert url.startswith("https://"), "the API key is refused over plain http"
    assert url.split("://", 1)[1].startswith("index.taild0fb76.ts.net:"), (
        "use the full MagicDNS name, not the short one"
    )


def test_no_api_key_in_the_committed_settings(settings_env: dict):
    """The tracked settings file must not carry the key.

    Qdrant holds a single global service.api_key - no key list, no per-client
    identity - so every cohort VM holds the same secret and a leak anywhere is a
    rotation everywhere, with no overlap window.

    Scope is this one dict, and the name says so: a key pasted into a doc or a
    tracked .env would pass here. Tree-wide protection is
    test_settings_local_is_not_tracked plus .gitignore, not a content scan - a
    64-hex search over every tracked file is a false-positive engine, and a name
    promising more than it checks is how broker's assertion read as true.
    """
    assert "QDRANT_API_KEY" not in settings_env


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


def test_settings_local_is_not_tracked():
    """Tracked beats ignored: a file already in the index is committed regardless.

    `git check-ignore` alone cannot see this. Asked *without* --no-index it
    reports a tracked path as not-ignored, and asked *with* it reports the
    matching rule and exits 0 even for a path that `git add -f` has already
    staged - so the flag turns the one assertion protecting the cohort's shared
    key blind to the exact state it exists to catch. Hence two tests.
    """
    tracked = _git("ls-files", "--error-unmatch", SETTINGS_LOCAL_NAME).returncode == 0
    assert not tracked, (
        f"{SETTINGS_LOCAL_NAME} is TRACKED by git - it holds this host's "
        "QDRANT_API_KEY and would be committed. Run: git rm --cached " + SETTINGS_LOCAL_NAME
    )


def test_settings_local_is_git_ignored():
    """Asks git, rather than reading .gitignore and assuming the answer.

    A rule can also come from .git/info/exclude or a global core.excludesfile,
    and `git check-ignore` is the only thing that sees all three. Four of the
    five cohort repos carried the rule, which is exactly what made the assertion
    read as true in the fifth - broker, which is public (notifier#68, broker#18).
    """
    ignored = _git("check-ignore", "-q", SETTINGS_LOCAL_NAME).returncode == 0
    assert ignored, f"{SETTINGS_LOCAL_NAME} is not git-ignored: it would be committed"


@pytest.mark.parametrize("path", ENV_SURFACE_FILES, ids=_surface_id)
@pytest.mark.parametrize("variable", NAMESPACE_GUARD_VARS)
def test_namespace_guards_are_not_set_anywhere(variable: str, path: Path):
    """VM-local where the file is VM-local; skips loudly rather than passing vacuously.

    The membership test is reduced to a bool *before* the assert on purpose. One
    of these paths is /etc/watcher/.env, and `assert variable not in
    path.read_text()` puts the whole file in the assertion expression - which
    pytest prints in full under -vv, DATABASE_URL password included. The repo's
    -v default truncates it, so the disclosure is one flag away rather than
    routine. Asserting on the bool leaves nothing to print.
    """
    if not path.exists():
        pytest.skip(f"{path} not present on this machine")
    declares = variable in path.read_text()
    assert not declares, f"{_surface_id(path)} sets {variable}"
