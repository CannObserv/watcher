"""Tests for ``scripts/pre-ship.sh``, watcher's wrapper around the vendored ship gate.

The vendored gate deselects integration-marked tests, and watcher merges to
``main`` locally and restarts the service from it, so CI reports only after the
code is live. The wrapper therefore runs ``pytest -m integration`` itself once
the vendored gate passes (#353), and fails on a skip as well as a failure
(``scripts/check_no_skips.py``).

Hermetic: the wrapper runs from a scratch git repo holding copies of it and
``load-env.sh``, a fake vendored gate and a fake ``uv`` that log their argv,
so neither the real gate nor the real suite runs (and nothing recurses).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "pre-ship.sh"
LOAD_ENV = REPO_ROOT / "scripts" / "load-env.sh"
DELEGATE = Path("skills/shipping-work-python-fastapi/scripts/pre-ship.sh")

FAKE_DELEGATE = """#!/usr/bin/env bash
echo "delegate probe=${PRE_SHIP_PROBE:-unset} args=$*" >> "$PRE_SHIP_LOG"
exit "${FAKE_DELEGATE_RC:-0}"
"""

FAKE_UV = """#!/usr/bin/env bash
echo "uv probe=${PRE_SHIP_PROBE:-unset} args=$*" >> "$PRE_SHIP_LOG"
case "$*" in
  *"pytest -m integration"*) exit "${FAKE_PYTEST_RC:-0}" ;;
  *check_no_skips.py*) exit "${FAKE_CHECK_RC:-0}" ;;
esac
exit 0
"""


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(WRAPPER, repo / "scripts" / "pre-ship.sh")
    shutil.copy(LOAD_ENV, repo / "scripts" / "load-env.sh")
    delegate = repo / DELEGATE
    delegate.parent.mkdir(parents=True)
    delegate.write_text(FAKE_DELEGATE, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(FAKE_UV, encoding="utf-8")
    uv.chmod(0o755)
    (tmp_path / "project.env").write_text("PRE_SHIP_PROBE=loaded\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return tmp_path


def run(sandbox: Path, *args: str, **rcs: int) -> tuple[subprocess.CompletedProcess[str], list]:
    log = sandbox / "calls.log"
    env = {
        "PATH": f"{sandbox / 'bin'}:/usr/bin:/bin",
        "HOME": str(sandbox),
        "PRE_SHIP_LOG": str(log),
        "WATCHER_SYSTEM_ENV_FILE": str(sandbox / "absent.env"),
        "WATCHER_PROJECT_ENV_FILE": str(sandbox / "project.env"),
        **{f"FAKE_{k.upper()}_RC": str(v) for k, v in rcs.items()},
    }
    result = subprocess.run(
        ["bash", "scripts/pre-ship.sh", *args],
        cwd=sandbox / "repo",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def _junit_paths(calls: list[str]) -> tuple[str, str]:
    pytest_call = next(c for c in calls if "pytest -m integration" in c)
    check_call = next(c for c in calls if "check_no_skips.py" in c)
    written = pytest_call.split("--junitxml=", 1)[1].split()[0]
    checked = check_call.split("check_no_skips.py", 1)[1].split()[0]
    return written, checked


def test_a_passing_gate_is_followed_by_the_integration_run(sandbox: Path) -> None:
    result, calls = run(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [c.split()[0] for c in calls] == ["delegate", "uv", "uv"]
    assert "args=run pytest -m integration" in calls[1]
    assert "args=run python scripts/check_no_skips.py" in calls[2]


def test_the_skip_check_reads_the_report_pytest_wrote(sandbox: Path) -> None:
    _, calls = run(sandbox)
    written, checked = _junit_paths(calls)
    assert written == checked
    assert not os.path.exists(written), "the JUnit tempfile outlives the run"


@pytest.mark.parametrize("rc", [1, 2])
def test_a_failing_gate_propagates_and_skips_integration(sandbox: Path, rc: int) -> None:
    result, calls = run(sandbox, delegate=rc)
    assert result.returncode == rc
    assert [c.split()[0] for c in calls] == ["delegate"]


def test_an_integration_failure_fails_the_wrapper(sandbox: Path) -> None:
    result, calls = run(sandbox, pytest=1)
    assert result.returncode == 1
    assert not any("check_no_skips.py" in c for c in calls)


def test_a_skipped_integration_test_fails_the_wrapper(sandbox: Path) -> None:
    result, _ = run(sandbox, check=1)
    assert result.returncode == 1


def test_secrets_reach_the_gate_and_the_integration_run(sandbox: Path) -> None:
    _, calls = run(sandbox)
    assert calls and all("probe=loaded" in c for c in calls), calls


def test_help_reaches_the_gate_and_runs_nothing(sandbox: Path) -> None:
    result, calls = run(sandbox, "--help")
    assert result.returncode == 0
    assert len(calls) == 1 and calls[0].startswith("delegate ")
    assert calls[0].endswith("args=--help")
    assert "-m integration" in result.stdout


def test_a_missing_delegate_is_a_tooling_error(sandbox: Path) -> None:
    (sandbox / "repo" / DELEGATE).unlink()
    result, calls = run(sandbox)
    assert result.returncode == 2
    assert calls == []


def test_uv_args_file_stops_the_wrapper_before_anything_runs(sandbox: Path) -> None:
    """CR 3: the vendored gate reads ``.skills/pre-ship-uv-args``; the wrapper's
    integration run does not. Rather than let the two environments drift
    silently, the file's arrival stops the gate until the wrapper honours it."""
    (sandbox / "repo" / ".skills").mkdir()
    (sandbox / "repo" / ".skills" / "pre-ship-uv-args").write_text("--group seed\n")
    result, calls = run(sandbox)
    assert result.returncode == 2
    assert calls == []
    assert ".skills/pre-ship-uv-args" in result.stderr
