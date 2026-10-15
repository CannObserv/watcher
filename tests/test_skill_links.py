"""Every project skill is wired into both discovery paths (CR 1, #384).

`docs/SKILLS.md` requires a `skills/<name>` entry *and* a
`.claude/skills/<name>` symlink to `../../skills/<name>`. `.skills/doctor.sh`
only finds links that dangle: a `.claude/skills/<name>` that was never created
is not dangling, it is absent, and Claude Code simply never discovers the
skill. The auto-refresh hook bumps vendor pointers but creates no symlinks, so
vendoring a skill is a by-hand step whose most likely slip is the second half.

Routing through `skills/` rather than straight to `skills-vendor/` is what lets
a committed override shadow the vendor copy in both systems, so the target is
checked exactly.
"""

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
CLAUDE_SKILLS_DIR = REPO_ROOT / ".claude" / "skills"


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if not p.name.startswith("."))


@pytest.mark.parametrize("name", _names(SKILLS_DIR))
def test_skill_has_claude_discovery_link(name: str) -> None:
    """Each `skills/<name>` is reachable from `.claude/skills/<name>`."""
    link = CLAUDE_SKILLS_DIR / name
    assert link.is_symlink(), f".claude/skills/{name} missing: ln -s ../../skills/{name} {link}"
    assert os.readlink(link) == f"../../skills/{name}"


@pytest.mark.parametrize("name", _names(CLAUDE_SKILLS_DIR))
def test_claude_link_has_skills_entry(name: str) -> None:
    """No `.claude/skills/<name>` bypasses, or outlives, its `skills/` entry."""
    assert (SKILLS_DIR / name).is_symlink() or (SKILLS_DIR / name).is_dir(), (
        f".claude/skills/{name} has no skills/{name}"
    )
