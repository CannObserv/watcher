"""Tests for the one-off job-history backlog prune (#296 D7).

The script deletes rows, so its first obligation is the one every launch path
here carries: refuse a production database unless the operator opts in, with
the same guard the service uses (``src.core.db_safety``, #233).
"""

from src.ops import prune_job_history

PRODUCTION_URL = "postgresql+asyncpg://watcher_app:pw@localhost:5432/watcher"


def test_refuses_a_production_database_without_the_opt_in(capsys) -> None:
    code = prune_job_history.main(["--dry-run"], environ={"DATABASE_URL": PRODUCTION_URL})
    assert code == 2
    assert "WATCHER_ALLOW_PRODUCTION_DB" in capsys.readouterr().err


def test_refuses_when_no_database_is_configured(capsys) -> None:
    code = prune_job_history.main(["--dry-run"], environ={})
    assert code == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_a_non_positive_step_is_refused(capsys) -> None:
    code = prune_job_history.main(
        ["--dry-run", "--step-days", "0"],
        environ={"DATABASE_URL": PRODUCTION_URL, "WATCHER_ALLOW_PRODUCTION_DB": "1"},
    )
    assert code == 2
