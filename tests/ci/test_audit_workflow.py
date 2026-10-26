"""Pin ``.github/workflows/audit.yml``, the report-only lock audit (#397).

It runs ``scripts/audit_lock.sh`` (``tests/scripts/test_audit_lock.py``) on a
schedule, on demand, and on a push or PR that touches the lock. Each decision
below is one a later edit could quietly undo:

* **Its own workflow, never a ``ci.yml`` job.** The epic gate reads ``ci.yml``'s
  jobs by name; an advisory disclosed upstream must not turn ``main`` red.
  Report-only is the script's exit 0, not ``continue-on-error``, and the job's
  name says so.
* **Weekly as well as on change.** Disclosures arrive without commits.
* **Push as well as PR, path-filtered.** Watcher lands by a local merge and a
  push; only Dependabot opens PRs, and those move action pins. The filter is the
  files that change what the audit reads, plus the audit's own two.
* **No secret, no OIDC token, no wheelhouse.** ``uv audit --frozen`` reads the
  lock alone (measured from a bare ``git archive``, #397), so the job needs none
  of ``ci.yml``'s WIF steps, and ``contents: read`` is all it holds.
* **No cache.** ``--frozen`` downloads no package.
* **ci.yml's runner.** The job needs only the uv binary; one runner policy per
  repo keeps #365's image check whole.

Pure file reads — no network, no database.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "audit.yml"
CI = WORKFLOWS / "ci.yml"

# What changes the audit's input or the audit itself.
LOCK_PATHS = {"uv.lock", "pyproject.toml", "scripts/audit_lock.sh", ".github/workflows/audit.yml"}


def _load(path: Path) -> dict:
    """The parsed workflow, or an empty document while the file is missing.

    Tolerating absence keeps collection alive, so the failure surfaces as
    ``test_workflow_exists``'s message rather than a collection error.
    """
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8") if WORKFLOW.is_file() else ""


def _triggers() -> dict:
    """The ``on:`` block. PyYAML reads the bare key ``on`` as the boolean True."""
    doc = _load(WORKFLOW)
    return doc.get("on", doc.get(True)) or {}


@pytest.fixture
def job() -> dict:
    jobs = _load(WORKFLOW).get("jobs", {})
    assert len(jobs) == 1, f"expected one audit job, found {sorted(jobs)}"
    return next(iter(jobs.values()))


def _steps_using(job: dict, action: str) -> list[dict]:
    return [s for s in job.get("steps", []) if s.get("uses", "").split("@")[0] == action]


def test_workflow_exists() -> None:
    assert WORKFLOW.is_file(), "no .github/workflows/audit.yml: nothing audits uv.lock (#397)"


def test_runs_weekly_and_on_demand() -> None:
    triggers = _triggers()
    crons = [entry.get("cron") for entry in triggers.get("schedule") or []]
    assert len(crons) == 1 and crons[0], f"schedule {crons}: disclosures arrive without commits"
    assert "workflow_dispatch" in triggers, "a deliberate run needs workflow_dispatch"


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_runs_on_a_lock_touching_change_to_main(event: str) -> None:
    trigger = _triggers().get(event) or {}
    assert trigger.get("branches") == ["main"], f"{event}: branches {trigger.get('branches')}"
    assert set(trigger.get("paths") or []) == LOCK_PATHS, (
        f"{event}: paths {trigger.get('paths')}. Unfiltered it re-audits every push; "
        f"missing one of {sorted(LOCK_PATHS)}, a change to it goes unaudited until the cron"
    )


def test_job_name_says_report_only(job: dict) -> None:
    """A reader of the run's checks can tell it is not the gate."""
    assert "(report-only)" in job.get("name", ""), f"job name {job.get('name')!r}"


def test_runs_the_script_and_masks_nothing(job: dict) -> None:
    runs = [s["run"].strip() for s in job.get("steps", []) if "run" in s]
    assert runs == ["bash scripts/audit_lock.sh"], f"run steps {runs}"
    assert "continue-on-error" not in _text(), "report-only is the script's exit, not a mask"


def test_holds_no_secret_and_no_oidc_token(job: dict) -> None:
    text = _text()
    assert _load(WORKFLOW).get("permissions") == {"contents": "read"}
    assert "permissions" not in job, "the job widens nothing"
    assert "secrets." not in text
    assert "id-token" not in text
    assert "sync_wheelhouse" not in text, "--frozen reads the lock without the wheelhouse"
    assert not _steps_using(job, "google-github-actions/auth")


def test_setup_uv_keeps_no_cache(job: dict) -> None:
    steps = _steps_using(job, "astral-sh/setup-uv")
    assert len(steps) == 1, "the job installs uv with setup-uv"
    assert steps[0].get("with", {}).get("enable-cache") is False, (
        "--frozen downloads no package; setup-uv's default caches on GitHub's runners"
    )


def test_bounded_runtime(job: dict) -> None:
    assert isinstance(job.get("timeout-minutes"), int)


def test_runs_on_ci_yml_s_runner(job: dict) -> None:
    runners = {j.get("runs-on") for j in _load(CI).get("jobs", {}).values()}
    assert runners, "found no ci.yml jobs — the derivation broke"
    assert job.get("runs-on") in runners, f"{job.get('runs-on')!r} is none of ci.yml's {runners}"


def test_ci_yml_never_runs_the_audit() -> None:
    """The gate is ci.yml's jobs; the audit reports beside it, never in it."""
    text = CI.read_text(encoding="utf-8")
    assert not re.search(r"audit_lock|uv audit", text)
