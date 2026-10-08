"""Tests for ``scripts/check_no_skips.py`` (#353).

The integration gates (CI's ``integration`` job, ``scripts/pre-ship.sh``) must
fail on a skip, not only on a failure: a test that skips itself — ``pg_dump``
missing or older than the server, a role it cannot create — reports green
while verifying nothing, so "runs in CI" would read true when it is not. The
script reads the JUnit XML pytest writes and fails on any skipped (or xfailed)
testcase, and on a run that executed nothing at all.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_no_skips.py"


def _junit(tmp_path: Path, *cases: str) -> Path:
    """Write a pytest-shaped JUnit report holding ``cases`` (raw ``<testcase>`` XML)."""
    path = tmp_path / "junit.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests"><testsuite name="pytest">'
        + "".join(cases)
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def _passed(name: str = "test_ok") -> str:
    return f'<testcase classname="tests.core.test_x.TestX" name="{name}" time="0.01" />'


def _skipped(name: str, message: str, kind: str = "pytest.skip") -> str:
    return (
        f'<testcase classname="tests.ops.test_y" name="{name}" time="0.00">'
        f'<skipped type="{kind}" message="{message}">reason</skipped></testcase>'
    )


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_all_passed_exits_zero(tmp_path: Path) -> None:
    result = run(str(_junit(tmp_path, _passed("test_a"), _passed("test_b"))))
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_skip_fails_and_names_the_test_and_its_reason(tmp_path: Path) -> None:
    report = _junit(
        tmp_path, _passed(), _skipped("test_rehearsal", "pg_dump is older than the server")
    )
    result = run(str(report))
    assert result.returncode == 1
    assert "tests.ops.test_y::test_rehearsal" in result.stdout
    assert "pg_dump is older than the server" in result.stdout


def test_every_skip_is_listed(tmp_path: Path) -> None:
    report = _junit(tmp_path, _skipped("test_one", "a"), _skipped("test_two", "b"))
    result = run(str(report))
    assert result.returncode == 1
    assert "test_one" in result.stdout and "test_two" in result.stdout


def test_an_xfail_counts_as_a_skip(tmp_path: Path) -> None:
    """JUnit records an xfail as ``<skipped type="pytest.xfail">``: it verified nothing."""
    result = run(str(_junit(tmp_path, _passed(), _skipped("test_x", "known", "pytest.xfail"))))
    assert result.returncode == 1
    assert "test_x" in result.stdout


def test_an_empty_run_fails(tmp_path: Path) -> None:
    """A marker expression that selects nothing must not pass as a clean gate."""
    result = run(str(_junit(tmp_path)))
    assert result.returncode == 1
    assert "no tests" in result.stdout.lower()


def test_a_missing_report_is_a_tooling_error(tmp_path: Path) -> None:
    result = run(str(tmp_path / "absent.xml"))
    assert result.returncode == 2
    assert "absent.xml" in result.stderr


def test_an_unparsable_report_is_a_tooling_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuites><testsuite", encoding="utf-8")
    result = run(str(bad))
    assert result.returncode == 2


def test_no_argument_is_a_usage_error() -> None:
    result = run()
    assert result.returncode == 2
