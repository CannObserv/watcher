"""Tests for the one-off job-history backlog prune (#296 D7).

The script deletes rows, so its first obligation is the one every launch path
here carries: refuse a production database unless the operator opts in, with
the same guard the service uses (``src.core.db_safety``, #233).
"""

from unittest.mock import AsyncMock, MagicMock

from src.ops import prune_job_history
from src.workers.retention import FAILED_RETENTION_HOURS

PRODUCTION_URL = "postgresql+asyncpg://watcher_app:pw@localhost:5432/watcher"
DEV_URL = "postgresql+asyncpg://watcher:pw@localhost:5432/watcher_dev"


def test_an_entry_point_configures_logging(monkeypatch) -> None:
    """procrastinate logs as the prune runs; unconfigured, its records are not
    the JSON every other entry point writes (AGENTS.md → Logging)."""
    configure = MagicMock()
    monkeypatch.setattr(prune_job_history, "configure_logging", configure)
    prune_job_history.main(["--dry-run"], environ={})
    configure.assert_called_once_with()


def test_the_worker_dsn_rule_is_the_one_used(monkeypatch, capsys) -> None:
    """The worker's own rule (``get_conninfo``), not a copy of it: the copy
    passed any scheme through where the worker refuses."""
    prune = AsyncMock()
    monkeypatch.setattr(prune_job_history, "_prune", prune)
    assert prune_job_history.main(["--dry-run"], environ={"DATABASE_URL": DEV_URL}) == 0
    assert prune.await_args.args[0] == "postgresql://watcher:pw@localhost:5432/watcher_dev"
    policy = capsys.readouterr().out.splitlines()[0]
    assert f"{FAILED_RETENTION_HOURS // 24} days" in policy
    assert "cancelled" in policy and "aborted" in policy


def test_a_url_the_worker_would_refuse_is_refused(monkeypatch, capsys) -> None:
    prune = AsyncMock()
    monkeypatch.setattr(prune_job_history, "_prune", prune)
    environ = {"DATABASE_URL": "mysql://u:p@localhost/watcher_dev"}
    assert prune_job_history.main(["--dry-run"], environ=environ) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
    prune.assert_not_awaited()


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
