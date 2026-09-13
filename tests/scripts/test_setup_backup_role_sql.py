"""Static guards for ``scripts/setup-backup-role.sql`` (#297).

The backup job's database role: ``pg_read_all_data`` and nothing more, reached
only by peer auth from the unit's own dynamic user. Like
``scripts/setup-db-roles.sql`` it is run as a superuser against the live
database, so what is checkable without a database is checked here — above all
that the role can read and can do nothing else.

Text assertions, deliberately: whether a role with these rights takes a
complete dump was proven on the VM against production (docs/RECOVERY.md →
*Rehearsals*), since the suite's own role cannot grant a predefined role.
"""

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-backup-role.sql"


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


def test_creates_the_role_idempotently(statements: str) -> None:
    assert "CREATE ROLE" in statements
    assert "NOT EXISTS" in statements


def test_the_role_reads_everything_and_writes_nothing(statements: str) -> None:
    """``pg_read_all_data`` is SELECT on every table and sequence and USAGE on
    every schema — what ``pg_dump`` needs, and all of it."""
    assert re.search(r"GRANT\s+PG_READ_ALL_DATA\s+TO", statements)
    assert "PG_WRITE_ALL_DATA" not in statements
    assert not re.search(r"GRANT\s+(INSERT|UPDATE|DELETE|TRUNCATE|CREATE|ALL)\b", statements)


def test_the_membership_is_inherited_explicitly(statements: str) -> None:
    """On PostgreSQL 16 a re-grant that omits an option keeps the existing
    membership's value, and the role's INHERIT attribute only sets the default
    for *new* grants. Only an explicit ``WITH INHERIT TRUE`` repairs a
    membership left non-inheriting, whose rights would not reach the role."""
    assert re.search(
        r'GRANT\s+PG_READ_ALL_DATA\s+TO\s+:"BACKUP_ROLE"\s+WITH\s+INHERIT\s+TRUE\s*;', statements
    )


def test_every_attribute_is_re_asserted(statements: str) -> None:
    """A role that acquired an attribute by hand loses it on the next run.
    INHERIT is the one it needs: the predefined role's rights reach it only
    through inheritance. NOBYPASSRLS keeps a table under row security a loud
    ``pg_dump`` failure rather than a quietly different dump."""
    alter = re.search(r"ALTER ROLE[^;]*;", statements, flags=re.DOTALL)
    assert alter, "no ALTER ROLE re-asserting the attributes"
    for attribute in (
        "LOGIN",
        "NOSUPERUSER",
        "NOCREATEDB",
        "NOCREATEROLE",
        "NOREPLICATION",
        "NOBYPASSRLS",
        "INHERIT",
        "PASSWORD NULL",
    ):
        assert re.search(rf"\b{attribute}\b", alter.group(0)), attribute


def test_holds_no_password(sql: str, statements: str) -> None:
    """Peer auth only: with no password, no password rule in pg_hba can ever
    admit it, and nothing needs to hold one."""
    assert "\\GETENV" not in statements
    assert not re.search(r"PASSWORD\s+'", sql, flags=re.IGNORECASE)


def test_destroys_nothing(statements: str) -> None:
    for verb in ("DROP ", "REVOKE ", "REASSIGN ", "TRUNCATE ", "DELETE FROM"):
        assert verb not in statements, verb


def test_the_report_shows_every_attribute_the_operator_is_told_to_check(sql: str) -> None:
    """The runbook names the value every attribute column must read — t for
    login and inherit, f for the rest. A report that omits a column cannot
    show it either way."""
    report = sql.split("COMMIT;", 1)[1]
    for column in (
        "rolcanlogin",
        "rolinherit",
        "rolsuper",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        assert column in report, column
