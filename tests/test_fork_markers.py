"""A forked SKILL.md must name a base commit that actually exists (#281).

`skills/<name>/SKILL.md` overrides are forks of a vendored skill, and each
records the vendor commit it was last synced from:

    <!-- forked from gregoryfoster-skills@a727638 -->

`docs/SKILLS.md` calls that marker "what makes the next refresh a 3-way diff
instead of a guess", and it is the only record of where the fork branched.
Nothing checked it: `.skills/doctor.sh` compares the `version:` metadata, which
is a different claim, and the marker is typed by hand during a re-sync. A
mistyped or invented SHA reads exactly like a good one until someone attempts
the diff it exists to enable — possibly long after whoever typed it has gone.

Two properties, and deliberately not a third:

* the SHA resolves to a commit in that vendor's submodule;
* it is an ancestor of the pinned submodule commit, so the stamp cannot claim
  history this repo does not carry — a fork "synced from" a commit newer than
  the pin was synced from something else.

Being *behind* the pin is not checked, because that is ordinary sync debt: an
override falls behind on purpose and pays it down on its own schedule. Failing
on it would push the next reader toward deleting the marker (CR-20).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
VENDOR_DIR = REPO_ROOT / "skills-vendor"
MARKER_RE = re.compile(r"<!--\s*forked from\s+([A-Za-z0-9._-]+)@([0-9a-f]{7,40})\b")


def _forked_skills() -> list[tuple[Path, str, str]]:
    """Every committed SKILL.md carrying a fork marker, with vendor and SHA.

    A directory symlinked to a vendor tree is not a fork and has no marker;
    `is_symlink` on the skill directory keeps those out without reading them.
    """
    found = []
    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        if skill_md.parent.is_symlink():
            continue
        match = MARKER_RE.search(skill_md.read_text(encoding="utf-8"))
        if match:
            found.append((skill_md, match.group(1), match.group(2)))
    return found


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("skill_md", "vendor", "sha"),
    _forked_skills(),
    ids=lambda value: value.parent.name if isinstance(value, Path) else str(value),
)
def test_fork_marker_names_a_real_vendor_commit(skill_md: Path, vendor: str, sha: str) -> None:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    vendor_repo = VENDOR_DIR / vendor
    if not (vendor_repo / ".git").exists():
        pytest.skip(f"{vendor} submodule not initialized")

    resolved = _git(["cat-file", "-e", f"{sha}^{{commit}}"], vendor_repo)
    assert resolved.returncode == 0, (
        f"{skill_md.relative_to(REPO_ROOT)} is stamped {vendor}@{sha}, which is "
        f"not a commit in skills-vendor/{vendor}: {resolved.stderr.strip()}"
    )

    ancestor = _git(["merge-base", "--is-ancestor", sha, "HEAD"], vendor_repo)
    assert ancestor.returncode == 0, (
        f"{skill_md.relative_to(REPO_ROOT)} claims it was synced from "
        f"{vendor}@{sha}, which is not an ancestor of the pinned "
        f"skills-vendor/{vendor} commit. Being behind the pin is fine — sync "
        f"debt — but a stamp ahead of it names history this repo does not have."
    )


def test_every_forked_skill_carries_a_marker() -> None:
    """A fork with no marker is the same gap, arrived at by omission.

    `skills/<name>/` that is a real directory is an override by construction —
    the vendored ones are symlinks — so each must record where it branched.
    """
    unmarked = [
        skill_md.parent.name
        for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md"))
        if not skill_md.parent.is_symlink()
        and not MARKER_RE.search(skill_md.read_text(encoding="utf-8"))
    ]
    assert not unmarked, (
        f"forked skills with no '<!-- forked from <vendor>@<sha> -->' marker: {unmarked}"
    )
