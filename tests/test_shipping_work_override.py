"""The shipping-work override resolves every script its later steps run (#320).

Upstream fences Step 1's resolution block as
`<!-- skill:required id=skill-scripts -->` and resolves each of its six
scripts on its own (gregoryfoster/skills#301). Watcher is the layout that
needs it: `scripts/pre-ship.sh` exists here as the env-loading wrapper, so a
block resolving one directory for all six stops at `scripts/` and sends
`doc-check.sh` and the four after it to exit 127 — none of them exist there.

`.skills/doctor.sh` reports a dropped fragment, but only as advice. This runs
the block itself from the project root, the cwd an agent actually has, and
holds the two halves together: every `<name.sh>` placeholder a later step
invokes is one the block prints, and every path it prints opens a file.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = REPO_ROOT / "skills" / "shipping-work-python-fastapi" / "SKILL.md"
VENDOR_REPO = REPO_ROOT / "skills-vendor" / "gregoryfoster-skills"
FENCE_RE = re.compile(
    r"<!--\s*skill:required\s+id=skill-scripts\s*-->\s*```bash\n(.*?)```", re.DOTALL
)
RESOLVED_RE = re.compile(r"^<([\w.-]+\.sh)>=(.+)$", re.MULTILINE)
PLACEHOLDER_RE = re.compile(r'^bash "<([\w.-]+\.sh)>"', re.MULTILINE)


def _skill_scripts_block() -> str:
    match = FENCE_RE.search(SKILL_MD.read_text(encoding="utf-8"))
    assert match, (
        f"{SKILL_MD.relative_to(REPO_ROOT)} carries no "
        "'<!-- skill:required id=skill-scripts -->' bash block — the fragment "
        "upstream fences because dropping it sends later steps to exit 127"
    )
    return match.group(1)


def _resolve() -> dict[str, str]:
    """Run the block with its doctor preflight and its final `bash` removed.

    The doctor heals symlinks and the last line runs the ship gate; neither is
    resolution, and running the gate from inside the suite would recurse.
    """
    lines = [
        line
        for line in _skill_scripts_block().splitlines()
        if ".skills/doctor.sh" not in line and not line.startswith("bash ")
    ]
    result = subprocess.run(
        ["bash", "-c", "\n".join(lines)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return dict(RESOLVED_RE.findall(result.stdout))


@pytest.fixture(scope="module")
def resolved() -> dict[str, str]:
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    # CI's pytest job checks out without submodules: every skill-directory
    # candidate is then a dangling symlink and the block stops at `${SD:?}`.
    if not (VENDOR_REPO / ".git").exists():
        pytest.skip("gregoryfoster-skills submodule not initialized")
    return _resolve()


def test_every_placeholder_a_step_invokes_is_resolved(resolved: dict[str, str]) -> None:
    invoked = set(PLACEHOLDER_RE.findall(SKILL_MD.read_text(encoding="utf-8")))
    assert invoked, 'no bash "<name.sh>" invocations found — the pattern is stale'
    assert invoked <= resolved.keys(), (
        f"steps invoke {sorted(invoked - resolved.keys())} but Step 1 prints no path for them"
    )


def test_every_resolved_path_opens_a_file(resolved: dict[str, str]) -> None:
    missing = {name: path for name, path in resolved.items() if not (REPO_ROOT / path).is_file()}
    assert not missing, f"resolved to paths that do not exist (exit 127 at run time): {missing}"


def test_pre_ship_resolves_to_the_env_loading_wrapper(resolved: dict[str, str]) -> None:
    """The one script watcher shadows must still win, and only for itself."""
    assert resolved.get("pre-ship.sh") == "scripts/pre-ship.sh"
    shadowed = {
        n: p for n, p in resolved.items() if n != "pre-ship.sh" and p.startswith("scripts/")
    }
    assert not shadowed, f"only pre-ship.sh has a watcher copy under scripts/: {shadowed}"
