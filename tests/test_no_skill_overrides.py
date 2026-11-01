"""Every vendored skill is linked whole, never forked (#413).

A fork of a vendored `SKILL.md` falls out of sync with its upstream: every
vendor rewrite means re-applying its deltas by hand, and until someone does,
it stops receiving fixes without saying so (#396, #409). So each
`skills/<name>` is a whole-directory symlink into `skills-vendor/`, and what an
override used to carry lives elsewhere: a `.skills/` knob, a repo-root wrapper
such as `scripts/pre-ship.sh`, or the docs (`docs/SKILLS.md`).

A delta that fits none of those is a knob request upstream. Until that knob
ships, the override it forces is named in `ALLOWED_OVERRIDES` with the
request's URL, so the reason it exists, and the event that retires it, are
both on record. An allowed override is still a fork, so it still records the
vendor commit it branched from (#281):

    <!-- forked from gregoryfoster-skills@a727638 -->

and that SHA must resolve in the vendor's submodule and be an ancestor of the
pinned commit — a stamp ahead of the pin names history this repo does not
carry. Falling *behind* the pin is ordinary sync debt and is not checked.

`ALLOWED_OVERRIDES` is empty, so the marker test passes over nothing rather
than collecting as a skip.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
VENDOR_DIR = REPO_ROOT / "skills-vendor"
MARKER_RE = re.compile(r"<!--\s*forked from\s+([A-Za-z0-9._-]+)@([0-9a-f]{7,40})\b")
KNOB_REQUEST_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/issues/\d+$")

# skills/<name> -> the upstream knob request that will retire the override.
ALLOWED_OVERRIDES: dict[str, str] = {}


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_every_skill_is_a_vendor_symlink() -> None:
    """Checks the link text, not its target, so CI without submodules agrees."""
    forks = {}
    for entry in sorted(SKILLS_DIR.iterdir()):
        if entry.name.startswith(".") or entry.name in ALLOWED_OVERRIDES:
            continue
        if not entry.is_symlink():
            forks[entry.name] = "a real directory"
        elif not os.readlink(entry).startswith("../skills-vendor/"):
            forks[entry.name] = f"a symlink to {os.readlink(entry)}"
    assert not forks, (
        f"skills/ entries that are not whole-directory vendor symlinks: {forks}. "
        "Move the delta into a .skills/ knob, a scripts/ wrapper or the docs; "
        "if none fits, file a knob request upstream and name it in ALLOWED_OVERRIDES."
    )


def test_every_allowed_override_exists_and_names_its_knob_request() -> None:
    stale = [name for name in ALLOWED_OVERRIDES if not (SKILLS_DIR / name).is_dir()]
    assert not stale, f"ALLOWED_OVERRIDES names skills/ entries that are gone: {stale}"
    unlinked = {n: url for n, url in ALLOWED_OVERRIDES.items() if not KNOB_REQUEST_RE.match(url)}
    assert not unlinked, f"each override must name its upstream knob request URL: {unlinked}"


def test_every_allowed_override_carries_a_resolvable_fork_marker() -> None:
    for name in ALLOWED_OVERRIDES:
        skill_md = SKILLS_DIR / name / "SKILL.md"
        match = MARKER_RE.search(skill_md.read_text(encoding="utf-8"))
        assert match, f"skills/{name}/SKILL.md has no '<!-- forked from <vendor>@<sha> -->' marker"
        vendor, sha = match.groups()
        vendor_repo = VENDOR_DIR / vendor
        if shutil.which("git") is None or not (vendor_repo / ".git").exists():
            pytest.skip(f"git or the {vendor} submodule is unavailable")

        resolved = _git(["cat-file", "-e", f"{sha}^{{commit}}"], vendor_repo)
        assert resolved.returncode == 0, (
            f"skills/{name}/SKILL.md is stamped {vendor}@{sha}, which is not a "
            f"commit in skills-vendor/{vendor}: {resolved.stderr.strip()}"
        )
        ancestor = _git(["merge-base", "--is-ancestor", sha, "HEAD"], vendor_repo)
        assert ancestor.returncode == 0, (
            f"skills/{name}/SKILL.md claims {vendor}@{sha}, which is not an "
            f"ancestor of the pinned skills-vendor/{vendor} commit"
        )
