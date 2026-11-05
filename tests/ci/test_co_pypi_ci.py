"""Pin how CI reaches the co-pypi index (#358).

co-core resolves from the co-pypi Artifact Registry index, which takes an access
token as a password. CI already holds a keyless identity: each Python job's WIF
step impersonates ``co-pypi-reader``, which cannobserv#554 granted
``roles/artifactregistry.reader``. ``token_format: access_token`` makes that step
also mint the token, as a step output the action masks.

What is pinned, because a credential in the wrong place leaks quietly:

* **Every job that runs ``uv sync`` authenticates first**, with
  ``token_format: access_token``, and only those jobs hold ``id-token: write``.
* **The pair reaches the ``uv sync`` step alone.** It is on that step's ``env``,
  never on a job's or the workflow's, and no ``run:`` names the token, so
  nothing ``echo``s it. Every later ``uv run`` finds the venv synced and
  downloads nothing.
* **No wheelhouse step is left.** Its sync was 76% of CI job time (#416).

Pure file reads: no network.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SA = "co-pypi-reader@co-gcs.iam.gserviceaccount.com"
USERNAME_VAR = "UV_INDEX_CO_PYPI_USERNAME"
PASSWORD_VAR = "UV_INDEX_CO_PYPI_PASSWORD"
TOKEN = "${{ steps.auth.outputs.access_token }}"


def _workflow() -> dict:
    return yaml.safe_load(CI.read_text())


def _jobs() -> dict:
    return _workflow()["jobs"]


def _syncing_jobs() -> list[str]:
    return [
        name
        for name, job in _jobs().items()
        if any(step.get("run", "").startswith("uv sync") for step in job["steps"])
    ]


def _auth_index(job: dict) -> int:
    found = [
        i
        for i, step in enumerate(job["steps"])
        if step.get("uses", "").startswith("google-github-actions/auth@")
    ]
    assert len(found) == 1, f"want one WIF auth step, found {len(found)}"
    return found[0]


def _sync_index(job: dict) -> int:
    found = [i for i, s in enumerate(job["steps"]) if s.get("run", "").startswith("uv sync")]
    assert len(found) == 1
    return found[0]


def test_the_sweep_finds_the_four_python_jobs():
    """Guard the guard: a renamed step would leave every check below vacuous."""
    assert sorted(_syncing_jobs()) == ["integration", "lint", "migrations", "test"]


@pytest.mark.parametrize("name", _syncing_jobs())
def test_auth_mints_an_access_token_before_the_sync(name: str):
    job = _jobs()[name]
    at = _auth_index(job)
    step = job["steps"][at]
    assert step.get("id") == "auth"
    assert step["with"]["token_format"] == "access_token"
    assert step["with"]["service_account"] == SA
    assert step["with"]["workload_identity_provider"] == "${{ vars.GCP_WIF_PROVIDER }}"
    assert at < _sync_index(job)
    assert job["permissions"] == {"contents": "read", "id-token": "write"}


@pytest.mark.parametrize("name", _syncing_jobs())
def test_the_pair_is_on_the_sync_step_alone(name: str):
    job = _jobs()[name]
    sync_at = _sync_index(job)
    assert job["steps"][sync_at]["env"] == {USERNAME_VAR: "oauth2accesstoken", PASSWORD_VAR: TOKEN}
    for i, step in enumerate(job["steps"]):
        if i != sync_at:
            assert not set(step.get("env", {})) & {USERNAME_VAR, PASSWORD_VAR}, step
    assert not set(job.get("env", {})) & {USERNAME_VAR, PASSWORD_VAR}


def test_no_workflow_level_pair_and_no_run_names_the_token():
    workflow = _workflow()
    assert not set(workflow.get("env", {})) & {USERNAME_VAR, PASSWORD_VAR}
    for name, job in _jobs().items():
        for step in job.get("steps", []):
            run = step.get("run", "")
            assert "access_token" not in run and PASSWORD_VAR not in run, (name, run)


def test_only_the_syncing_jobs_get_an_oidc_token():
    holders = [n for n, j in _jobs().items() if j.get("permissions", {}).get("id-token")]
    assert sorted(holders) == sorted(_syncing_jobs())


def test_no_wheelhouse_step_is_left():
    text = CI.read_text()
    assert "sync_wheelhouse" not in text
    assert "wheelhouse" not in text.lower()
