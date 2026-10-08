"""Pin what the ``integration`` and ``live`` marks mean and where each runs (#353).

``integration`` used to be documented as "hits live external services" and was
excluded from every gate, so about 40% of the suite ran only when someone typed
``-m integration`` by hand. Run with nothing but ``TEST_DATABASE_URL`` and no
outbound network, every integration test but one passed: the mark means
*needs the test database*. The one exception reads real GCS, so it carries
``live`` instead — excluded by default and from CI.

So: CI runs ``integration`` in its own job against a ``postgres:16`` service,
with the same setup as the ``test`` job, and fails on a skip as well as a
failure (``scripts/check_no_skips.py``); no CI job selects ``live``. Pure file
reads — no CI, no database.
"""

import shlex
import tomllib
from pathlib import Path

import pytest
import yaml

# The module, not the class: a Test* name imported here would be collected twice.
from tests.core import test_blobs

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"]


def _pytest_ini() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]


def _marker_expression(argv: list[str]) -> str:
    return argv[argv.index("-m") + 1]


def _pytest_step(job: dict) -> tuple[int, list[str]]:
    """Return the index and argv of the job's single ``pytest`` step."""
    found = [
        (i, shlex.split(step["run"]))
        for i, step in enumerate(job["steps"])
        if "pytest" in shlex.split(step.get("run", ""))
    ]
    assert len(found) == 1, f"expected one pytest step, found {len(found)}"
    return found[0]


class TestMarkers:
    def test_both_marks_are_registered(self) -> None:
        names = {m.split(":", 1)[0].strip() for m in _pytest_ini()["markers"]}
        assert {"integration", "live"} <= names

    def test_integration_no_longer_claims_live_services(self) -> None:
        (integration,) = [m for m in _pytest_ini()["markers"] if m.startswith("integration:")]
        assert "live external" not in integration
        assert "TEST_DATABASE_URL" in integration

    def test_the_default_run_excludes_both(self) -> None:
        argv = shlex.split(_pytest_ini()["addopts"])
        expression = _marker_expression(argv)
        assert "not integration" in expression and "not live" in expression

    def test_the_gcs_proof_is_live_not_integration(self) -> None:
        """It skips without ``GCS_BLOB_CREDENTIALS``: under ``integration`` it would
        either fail the no-skips gate in CI or need a cloud credential there."""
        names = {m.name for m in test_blobs.TestGcsLive.pytestmark}
        assert "live" in names
        assert "integration" not in names


class TestCiJobs:
    def test_the_test_job_excludes_integration_and_live(self, jobs: dict) -> None:
        _, argv = _pytest_step(jobs["test"])
        assert _marker_expression(argv) == "not integration and not live"

    def test_an_integration_job_exists_with_a_postgres_16_service(self, jobs: dict) -> None:
        job = jobs["integration"]
        assert job["services"]["postgres"]["image"] == "postgres:16"
        assert job["env"]["TEST_DATABASE_URL"].endswith("/watcher_test")
        assert job["services"]["postgres"]["env"]["POSTGRES_DB"] == "watcher_test"

    def test_the_integration_job_runs_the_integration_mark(self, jobs: dict) -> None:
        _, argv = _pytest_step(jobs["integration"])
        assert _marker_expression(argv) == "integration"

    def test_the_integration_job_sets_up_like_the_test_job(self, jobs: dict) -> None:
        """Same checkout, uv, Python, WIF auth, wheelhouse sync and ``uv sync``."""
        test_at, _ = _pytest_step(jobs["test"])
        integration_at, _ = _pytest_step(jobs["integration"])
        assert jobs["integration"]["steps"][:integration_at] == jobs["test"]["steps"][:test_at]
        assert jobs["integration"]["permissions"] == jobs["test"]["permissions"]

    def test_the_integration_job_fails_on_a_skip(self, jobs: dict) -> None:
        steps = jobs["integration"]["steps"]
        at, argv = _pytest_step(jobs["integration"])
        report = next(a.split("=", 1)[1] for a in argv if a.startswith("--junitxml="))
        checks = [
            shlex.split(s["run"])
            for s in steps[at + 1 :]
            if "scripts/check_no_skips.py" in s.get("run", "")
        ]
        assert checks, "no scripts/check_no_skips.py step after the integration pytest step"
        assert checks[0][-1] == report

    def test_no_job_selects_live(self, jobs: dict) -> None:
        for name, job in jobs.items():
            for step in job.get("steps", []):
                argv = shlex.split(step.get("run", ""))
                if "pytest" in argv and "-m" in argv:
                    expression = _marker_expression(argv)
                    assert expression == "integration" or "not live" in expression, name
