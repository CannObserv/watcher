"""Pin a ``timeout-minutes`` on every ``.github/workflows/ci.yml`` job (#416).

A job without one runs until GitHub's 6-hour default: a ``services:`` health
wait or a stuck test holds the runner, and the epic gate's by-job read waits
on it. The measured maximum over the 2026-11-10..17 runs was 54 s
(``integration``), so a cap of minutes stops a hang without failing a slow run.
``audit.yml`` pins its own in ``tests/ci/test_audit_workflow.py``.

Pure file reads — no network, no database.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# GitHub's own default; a cap at or above it bounds nothing.
GITHUB_DEFAULT_MINUTES = 360


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8")).get("jobs") or {}


def _bounded(minutes: object) -> bool:
    """A literal whole number of minutes, above zero and under GitHub's default.

    ``bool`` is excluded explicitly: YAML's ``true`` loads as an ``int`` subclass.
    """
    return (
        isinstance(minutes, int)
        and not isinstance(minutes, bool)
        and 0 < minutes < GITHUB_DEFAULT_MINUTES
    )


def test_every_ci_job_has_a_positive_timeout() -> None:
    jobs = _jobs()
    assert jobs, "found no ci.yml jobs — the derivation broke"
    unbounded = {
        name: job.get("timeout-minutes")
        for name, job in jobs.items()
        if not _bounded(job.get("timeout-minutes"))
    }
    assert not unbounded, (
        f"ci.yml jobs without a bounded timeout-minutes: {unbounded}. "
        f"Each runs to GitHub's {GITHUB_DEFAULT_MINUTES}-minute default on a hang (#416)"
    )
