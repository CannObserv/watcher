"""Static guards for ``scripts/setup-test-role.sql`` (#388).

The test suite's own role: it owns ``watcher_test`` and ``watcher_dev``, may
create the rehearsal's scratch databases, and reaches nothing else. Run as a
superuser on the cluster production lives on, so what is checkable without a
database is checked here — above all that it creates, never destroys or
reassigns, and that the role it leaves can do nothing beyond its own databases.

Text assertions, deliberately: whether the role is contained is asked of the
live cluster by ``tests/scripts/test_db_containment.py``, as that role.
"""

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-test-role.sql"


@pytest.fixture(scope="module")
def sql() -> str:
    return SCRIPT.read_text()


@pytest.fixture(scope="module")
def statements(sql: str) -> str:
    """The script with comment lines stripped, upper-cased for matching."""
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return body.upper()


def test_script_exists_and_is_not_executable(sql: str) -> None:
    assert sql.strip()
    assert not SCRIPT.stat().st_mode & 0o111


def test_stops_on_first_error_and_is_transactional(statements: str) -> None:
    assert "ON_ERROR_STOP" in statements
    assert "BEGIN;" in statements
    assert "COMMIT;" in statements


def test_the_password_comes_from_the_environment(sql: str) -> None:
    assert "\\getenv tester_password WATCHER_TESTER_PASSWORD" in sql
    assert re.search(r"PASSWORD\s+:'tester_password'", sql)
    assert not re.search(r"PASSWORD\s+'", sql, flags=re.IGNORECASE), "quoted literal password"


def test_a_missing_password_aborts_nonzero(sql: str) -> None:
    """``\\quit`` exits 0: "no password" must not read as "role configured"."""
    guard = sql.split("\\getenv tester_password", 1)[1].split("BEGIN;", 1)[0]
    assert "RAISE EXCEPTION" in guard


def test_creates_the_role_idempotently(statements: str) -> None:
    assert "CREATE ROLE" in statements
    assert "NOT EXISTS" in statements


def test_every_attribute_is_re_asserted(statements: str) -> None:
    """CREATEDB, because the backup rehearsal creates its scratch databases as
    this role (#383); nothing else. NOCREATEROLE keeps it from minting a role
    with more, NOINHERIT keeps a later membership from taking effect silently."""
    alter = re.search(r"ALTER ROLE[^;]*;", statements, flags=re.DOTALL)
    assert alter, "no ALTER ROLE re-asserting the attributes"
    for attribute in (
        "LOGIN",
        "NOSUPERUSER",
        "CREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
        "NOINHERIT",
    ):
        assert re.search(rf"\b{attribute}\b", alter.group(0)), attribute


def test_grants_no_membership(statements: str) -> None:
    """No predefined role either: ``pg_signal_backend`` is cluster-wide and
    production shares the cluster; #383's drop does without it."""
    assert not re.search(r"\bGRANT\b", statements)
    assert "PG_SIGNAL_BACKEND" not in statements


def test_destroys_and_reassigns_nothing(statements: str) -> None:
    """``REASSIGN OWNED`` also moves the *databases* a role owns — production's
    with them, whichever database it runs in. The one-time drop of the old
    databases is a reviewed host step (docs/MIGRATIONS.md), never this file."""
    for verb in ("DROP ", "REVOKE ", "REASSIGN ", "OWNER TO", "TRUNCATE ", "DELETE FROM"):
        assert verb not in statements, verb


def test_its_databases_are_created_owned_by_it(statements: str) -> None:
    assert "CREATE DATABASE %I OWNER %I" in statements
    for default in ("WATCHER_TEST", "WATCHER_DEV"):
        assert re.search(rf"\\SET\s+\w+_DB\s+{default}\b", statements), default


def test_a_database_held_by_another_role_stops_it(statements: str) -> None:
    """An existing ``watcher_test`` owned by ``watcher`` is the pre-#388 state:
    refused loudly, before any database is created, rather than adopted."""
    refusal = statements.index("RAISE EXCEPTION %L")
    assert refusal < statements.index("CREATE DATABASE %I OWNER %I")


def test_create_database_runs_outside_the_transaction(statements: str) -> None:
    """CREATE DATABASE cannot run inside a transaction block."""
    assert statements.index("COMMIT;") < statements.index("CREATE DATABASE %I OWNER %I")


def test_the_report_shows_what_the_operator_checks(sql: str) -> None:
    report = sql.split("COMMIT;", 1)[1]
    for column in ("rolcanlogin", "rolsuper", "rolcreatedb", "rolcreaterole", "rolinherit"):
        assert column in report, column
    assert "has_database_privilege" in report
    assert "pg_has_role" in report
