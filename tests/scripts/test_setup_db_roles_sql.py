"""Static guards for ``scripts/setup-db-roles.sql`` (#259).

The script is the operator half of the role split and the only artefact in this
repo that a human runs as a PostgreSQL superuser against the **live** database.
A wrong line there locks the running service out of its own schema, so what is
checkable without a database is checked here: no embedded credential, no grant
that would hand DDL rights back to the application role, and no statement that
destroys or reassigns anything.

These are text assertions, deliberately. They cannot prove the grants are
*correct* — only a rehearsal against a real cluster does that (see the runbook
in docs/MIGRATIONS.md). They exist to stop the shapes that make the script
dangerous rather than merely wrong.
"""

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-db-roles.sql"


@pytest.fixture(scope="module")
def sql() -> str:
    return SCRIPT.read_text()


@pytest.fixture(scope="module")
def statements(sql: str) -> str:
    """The script with comment lines stripped, upper-cased for matching.

    Every prohibition below is about what the script *executes*; the comments
    necessarily discuss the same statements in prose (``-- never GRANT
    CREATE``) and would otherwise trip the guards they explain.
    """
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return body.upper()


def test_script_exists_and_is_not_executable(sql: str) -> None:
    """Plain SQL, read before it is run — not a script that runs itself."""
    assert sql.strip()
    assert not SCRIPT.stat().st_mode & 0o111


def test_stops_on_first_error(statements: str) -> None:
    """Without ON_ERROR_STOP psql runs on past a failed grant and reports success."""
    assert "ON_ERROR_STOP" in statements


def test_is_transactional(statements: str) -> None:
    """All-or-nothing: a half-applied grant set is the state that breaks the app."""
    assert "BEGIN;" in statements
    assert "COMMIT;" in statements


def test_embeds_no_password(sql: str) -> None:
    """The application password comes from the environment, never the repo."""
    assert "\\getenv app_password WATCHER_APP_PASSWORD" in sql
    assert re.search(r"PASSWORD\s+:'app_password'", sql), "password is not the psql variable"
    assert not re.search(r"PASSWORD\s+'", sql, flags=re.IGNORECASE), "quoted literal password"


def test_missing_password_aborts_nonzero(sql: str) -> None:
    """A missing WATCHER_APP_PASSWORD must fail the run, not end it politely.

    Rehearsed and found wrong the first time: ``\\warn`` + ``\\quit`` printed a
    refusal and exited **0**, so an operator (or any wrapper checking ``$?``)
    read "no password supplied" as "roles configured". Only a real error gives
    psql a non-zero exit.
    """
    guard = sql.split("\\getenv app_password")[1].split("\\endif")[0]
    assert "RAISE EXCEPTION" in guard, "the missing-password branch exits 0"


def test_creates_the_role_idempotently(statements: str) -> None:
    """Re-running must be a no-op, not an error — operators re-run scripts."""
    assert "CREATE ROLE" in statements
    assert "NOT EXISTS" in statements


def test_application_role_gets_no_ddl_rights(statements: str) -> None:
    """The point of the exercise. CREATE on a schema is DDL by another name."""
    assert not re.search(r"GRANT\s+[^;]*\bCREATE\b[^;]*\bON\s+SCHEMA", statements)
    assert not re.search(r"GRANT\s+ALL\b", statements)
    for attribute in ("NOSUPERUSER", "NOCREATEDB", "NOCREATEROLE"):
        assert attribute in statements, f"application role is not pinned {attribute}"


def test_application_role_gets_the_dml_it_needs(statements: str) -> None:
    """SELECT/INSERT/UPDATE/DELETE on tables, plus sequence usage.

    The sequences are not optional: ``procrastinate_jobs`` and its three
    companions are ``bigserial``, so a missing ``USAGE`` breaks every enqueue
    while leaving reads working — a partial failure that looks like a bug in
    the scheduler.
    """
    assert re.search(r"SELECT,\s*INSERT,\s*UPDATE,\s*DELETE\s+ON\s+ALL\s+TABLES", statements)
    assert re.search(r"USAGE,\s*SELECT\s+ON\s+ALL\s+SEQUENCES", statements)
    assert re.search(r"GRANT\s+USAGE\s+ON\s+SCHEMA", statements)


def test_future_objects_are_covered(statements: str) -> None:
    """Without default privileges the first migration that adds a table ships a
    runtime failure — the app cannot read what the migration just created."""
    assert statements.count("ALTER DEFAULT PRIVILEGES") >= 2
    assert "FOR ROLE" in statements, "default privileges must name the creating role"


def test_destroys_nothing(statements: str) -> None:
    """No DROP, no ownership transfer, no REVOKE from the migration role.

    The script is purely additive by design: the incumbent role keeps owning
    the schema, so rollback is an env-file edit rather than a repair job.
    ``REVOKE`` on the *application* role is allowed — that is the tightening
    pass on ``alembic_version``.
    """
    assert "DROP " not in statements
    assert "REASSIGN OWNED" not in statements
    assert "OWNER TO" not in statements
    assert not re.search(r"REVOKE\s+[^;]*\bFROM\s+:\"MIGRATE_ROLE\"", statements)
