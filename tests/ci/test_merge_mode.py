"""Work reaches ``main`` by squash PR, and the automated pushers know it (#445).

``.skills/merge_mode`` set to ``pr`` switches ``shipping-work``, the daily
skills refresh and the weekly ``context-cadence`` workflow from a direct push
to a pull request. The ruleset on ``main`` (no bypass) refuses every direct
push, so a knob that drifted back to ``direct``, or a cadence workflow rendered
for ``direct``, fails at its next run rather than here.

The parses of the tracked files run everywhere. The vendored readers ship in
the submodule, which CI's checkout does not initialise: off the host that is a
skip, on it a failure, as ``tests/deploy/test_patching_knob.py`` does.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOB = REPO_ROOT / ".skills" / "merge_mode"
CADENCE = REPO_ROOT / ".github" / "workflows" / "context-cadence.yml"
MERGE_MODE_SH = REPO_ROOT / "skills" / "shipping-work" / "scripts" / "merge-mode.sh"
INSTALL_CADENCE_SH = REPO_ROOT / "skills" / "curating-context" / "scripts" / "install-cadence.sh"
INSTALLED_UNIT = Path("/etc/systemd/system/watcher.service")


def _vendored(script: Path) -> Path:
    if not script.is_file():
        if INSTALLED_UNIT.exists():
            pytest.fail(f"{script} missing on the host: git submodule update --init")
        pytest.skip("skills submodule not initialised")
    return script


def test_the_knob_says_pr() -> None:
    """One word, no comments: the readers' grammar allows more, this repo needs none."""
    assert KNOB.read_text().split() == ["pr"]


def test_the_cadence_is_rendered_for_pr() -> None:
    """The ``pr`` render: a PR from ``context/weekly-measurement``, never a push to ``main``."""
    text = CADENCE.read_text()
    workflow = yaml.safe_load(text)
    assert workflow["permissions"] == {"contents": "write", "pull-requests": "write"}
    (job,) = workflow["jobs"].values()
    assert "Read the merge mode" in [step.get("name") for step in job["steps"]]
    assert "PR_BRANCH=context/weekly-measurement" in text


def test_the_vendored_reader_prints_pr() -> None:
    out = subprocess.run(
        ["bash", str(_vendored(MERGE_MODE_SH))], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "pr"


def test_the_cadence_install_checks_clean() -> None:
    """Upstream's own check: exit 0 means every guarantee holds, a ``pr`` knob included."""
    out = subprocess.run(
        ["bash", str(_vendored(INSTALL_CADENCE_SH)), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "merge mode:         pr" in out.stdout, out.stdout
