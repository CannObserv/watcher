"""Tests for the cache-check helper that short-circuits Archiver alembic.

The session-scoped ``_apply_archiver_migrations`` fixture in
``tests/conftest.py`` subprocess-invokes Archiver's alembic to provision
the cross-schema ``information.*`` tables. That subprocess is ~1-2 s of
``uv run`` cold start; #150 adds a cheap pre-check that skips the
subprocess when ``information.alembic_version`` already matches HEAD.

This module exercises the helper functions directly with a mocked
subprocess and a mocked SQL probe — no real DB or Archiver invocation.
"""

from unittest.mock import patch

import pytest

from tests import conftest as conftest_mod


def test_archiver_alembic_head_returns_revision_id():
    """``_archiver_alembic_head`` parses revision ids from versions/*.py."""
    head = conftest_mod._archiver_alembic_head()
    # Current Archiver HEAD at #150 time: 938ebc034b82 (info_specs).
    # We don't pin the literal — just sanity-check shape.
    assert isinstance(head, str)
    assert len(head) >= 8  # alembic short revision hashes
    # Must be one of the rev ids on disk; if Archiver adds a new migration,
    # this still passes because the helper returns *the* head, not a fixed value.


def test_archiver_alembic_head_returns_none_when_versions_dir_missing(tmp_path, monkeypatch):
    """Missing migrations dir → returns None (caller falls through)."""
    monkeypatch.setattr(conftest_mod, "ARCHIVER_REPO_PATH", tmp_path)
    assert conftest_mod._archiver_alembic_head() is None


def test_apply_archiver_migrations_skips_subprocess_when_at_head(monkeypatch):
    """Cache hit: ``information.alembic_version`` matches HEAD → no subprocess."""
    monkeypatch.setattr(conftest_mod, "_archiver_alembic_head", lambda: "abc123")
    monkeypatch.setattr(
        conftest_mod, "_information_schema_at_revision", lambda url, rev: rev == "abc123"
    )
    with patch.object(conftest_mod.subprocess, "run") as mock_run:
        conftest_mod._apply_archiver_migrations("postgresql+asyncpg://x")
    mock_run.assert_not_called()


def test_apply_archiver_migrations_runs_subprocess_when_schema_missing(monkeypatch):
    """Cache miss: schema not at HEAD → subprocess fires."""
    monkeypatch.setattr(conftest_mod, "_archiver_alembic_head", lambda: "abc123")
    # Simulate "schema doesn't exist" or "version mismatch".
    monkeypatch.setattr(conftest_mod, "_information_schema_at_revision", lambda url, rev: False)

    with patch.object(conftest_mod.subprocess, "run") as mock_run:
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        conftest_mod._apply_archiver_migrations("postgresql+asyncpg://x")

    assert mock_run.called
    args, kwargs = mock_run.call_args
    assert args[0][:4] == ["uv", "run", "alembic", "upgrade"]


def test_apply_archiver_migrations_falls_through_when_head_unknown(monkeypatch):
    """HEAD detection failure → still runs subprocess (defensive)."""
    monkeypatch.setattr(conftest_mod, "_archiver_alembic_head", lambda: None)
    # Probe is irrelevant once head is unknown; assert it's not consulted at all
    # would be too strict — but the subprocess MUST fire.
    monkeypatch.setattr(conftest_mod, "_information_schema_at_revision", lambda url, rev: True)

    with patch.object(conftest_mod.subprocess, "run") as mock_run:
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        conftest_mod._apply_archiver_migrations("postgresql+asyncpg://x")

    assert mock_run.called


def test_information_schema_at_revision_missing_schema_returns_false():
    """Probe returns False when ``information.alembic_version`` doesn't exist."""

    class FakeResult:
        def fetchall(self):
            return []  # nothing — table missing or empty

    class FakeConn:
        def execute(self, *args, **kwargs):
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConn()

        def dispose(self):
            pass

    with patch.object(conftest_mod, "create_engine", return_value=FakeEngine()):
        assert (
            conftest_mod._information_schema_at_revision("postgresql+asyncpg://x", "abc123")
            is False
        )


def test_information_schema_at_revision_matching_revision_returns_true():
    """Probe returns True when alembic_version row matches HEAD."""

    class FakeResult:
        def fetchall(self):
            return [("abc123",)]

    class FakeConn:
        def execute(self, *args, **kwargs):
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConn()

        def dispose(self):
            pass

    with patch.object(conftest_mod, "create_engine", return_value=FakeEngine()):
        assert (
            conftest_mod._information_schema_at_revision("postgresql+asyncpg://x", "abc123") is True
        )


def test_information_schema_at_revision_mismatched_revision_returns_false():
    """Probe returns False when alembic_version row exists but is older."""

    class FakeResult:
        def fetchall(self):
            return [("oldrev",)]

    class FakeConn:
        def execute(self, *args, **kwargs):
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConn()

        def dispose(self):
            pass

    with patch.object(conftest_mod, "create_engine", return_value=FakeEngine()):
        assert (
            conftest_mod._information_schema_at_revision("postgresql+asyncpg://x", "abc123")
            is False
        )


def test_information_schema_at_revision_multiple_rows_returns_false():
    """Defensive: multiple rows in alembic_version (shouldn't happen) → cache miss."""

    class FakeResult:
        def fetchall(self):
            return [("abc123",), ("abc123",)]

    class FakeConn:
        def execute(self, *args, **kwargs):
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConn()

        def dispose(self):
            pass

    with patch.object(conftest_mod, "create_engine", return_value=FakeEngine()):
        assert (
            conftest_mod._information_schema_at_revision("postgresql+asyncpg://x", "abc123")
            is False
        )


def test_information_schema_at_revision_db_error_returns_false():
    """Any DB error during probe → False (fall through to subprocess)."""

    class FakeEngine:
        def connect(self):
            raise RuntimeError("connection refused")

        def dispose(self):
            pass

    with patch.object(conftest_mod, "create_engine", return_value=FakeEngine()):
        assert (
            conftest_mod._information_schema_at_revision("postgresql+asyncpg://x", "abc123")
            is False
        )


@pytest.mark.parametrize(
    "url_in",
    [
        "postgresql+asyncpg://u:p@h/d",
        "postgresql://u:p@h/d",
        "postgresql+psycopg://u:p@h/d",
    ],
)
def test_information_schema_at_revision_normalizes_async_driver(url_in):
    """The probe must work with async URLs; sync engine swap is internal."""
    captured = {}

    class FakeResult:
        def fetchall(self):
            return [("abc123",)]

    class FakeConn:
        def execute(self, *args, **kwargs):
            return FakeResult()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConn()

        def dispose(self):
            pass

    def fake_create_engine(url):
        captured["url"] = url
        return FakeEngine()

    with patch.object(conftest_mod, "create_engine", side_effect=fake_create_engine):
        conftest_mod._information_schema_at_revision(url_in, "abc123")

    # Sync driver — must not contain '+asyncpg'.
    assert "+asyncpg" not in captured["url"]
