"""Regression guards for the Alembic migration chain.

These tests need no database — they introspect the version scripts and render
the chain in Alembic *offline* mode. They run in the default (non-integration)
suite so a broken or coupled migration is caught locally and in CI's `test`
job, complementing the `migrations` CI job that applies the chain against a
live Postgres (#234).

The invariants encoded here are the ones the #234 squash established:
- exactly one head, and it is a true base (`down_revision is None`);
- the chain references no Archiver-owned ``information`` schema — the
  cross-schema coupling the squash removed must never come back;
- the chain renders end-to-end and creates every model-backed table.
"""

import configparser
import io
import os
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
from src.core.database import MIGRATION_DATABASE_URL_ENV
from src.core.models import Base

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VERSIONS_DIR = _REPO_ROOT / "alembic" / "versions"

# DDL references to the Archiver-owned `information` schema — the exact coupling
# #234 removed. Matches `referent_schema="information"`, `schema='information'`,
# and schema-qualified refs like `"information.info_items"`. The second branch
# requires an identifier char after the dot (`information\.\w`) so a quoted
# sentence ending in "information." can't false-positive — only a real
# `information.<table>` reference matches. Prose/backtick mentions (e.g. the
# genesis docstring explaining why the coupling is gone) never match.
_INFORMATION_COUPLING = re.compile(
    r"""(?:referent_schema|schema)\s*=\s*["']information["']"""
    r"""|["']information\.\w""",
)

# Schema-level DDL against `information`, in either spelling a migration can
# use: raw SQL (`op.execute("DROP SCHEMA information CASCADE")`) and
# SQLAlchemy's DDL constructs (`op.execute(sa.schema.DropSchema("information"))`),
# which are the idiomatic form and carry no space between keyword and noun.
# Deliberately not part of `_INFORMATION_COUPLING` — that one is about
# cross-schema *references*, this one about the schema's existence.
#
# The raw-SQL branch requires the keyword to *precede* the name, as in SQL, so
# prose like "the ``information`` schema" cannot match; the construct branch
# requires the name quoted as a call argument, so prose naming `DropSchema`
# cannot either.
_INFORMATION_SCHEMA_DDL = re.compile(
    r"\b(?:CREATE|DROP)\s+SCHEMA\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?[\"']?information\b"
    r"""|\b(?:Create|Drop)Schema\(\s*["']information["']""",
    re.IGNORECASE,
)


def _versions_matching(pattern: re.Pattern[str]) -> list[str]:
    """Names of the version files whose source matches ``pattern``."""
    return [
        path.name
        for path in _VERSIONS_DIR.glob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    ]


@pytest.fixture(scope="module")
def script_directory() -> ScriptDirectory:
    """Alembic ScriptDirectory loaded from the repo's alembic.ini."""
    return ScriptDirectory.from_config(Config(str(_REPO_ROOT / "alembic.ini")))


def test_single_head(script_directory: ScriptDirectory) -> None:
    """The chain must have exactly one head (no branch/merge divergence)."""
    heads = script_directory.get_heads()
    assert len(heads) == 1, f"expected a single head, found {heads}"


# The #234 squash rooted the chain at a single genesis baseline. Later
# migrations extend it, but the chain must keep exactly one base and it must
# stay this revision — a second base (or a pre-genesis revision) would mean the
# squash was undone or a branch was introduced.
_GENESIS_REVISION = "2addddea0b03"


def test_single_base_is_genesis(script_directory: ScriptDirectory) -> None:
    """Exactly one base, and it is the #234 genesis baseline."""
    assert script_directory.get_bases() == [_GENESIS_REVISION]
    assert script_directory.get_revision(_GENESIS_REVISION).down_revision is None


@pytest.mark.parametrize(
    "line",
    [
        'op.execute("DROP SCHEMA information CASCADE")',
        "op.execute('drop schema if exists information cascade')",
        'op.execute("CREATE SCHEMA information")',
        'op.execute("DROP  SCHEMA\n    information")',
        # SQLAlchemy's DDL constructs — the idiomatic spelling in an Alembic
        # migration, and the one the keyword-only pattern used to miss.
        'op.execute(sa.schema.DropSchema("information", cascade=True))',
        "op.execute(DropSchema('information'))",
        'op.execute(sa.schema.CreateSchema("information", if_not_exists=True))',
        'op.execute(CreateSchema("information"))',
    ],
)
def test_information_ddl_pattern_matches_known_forms(line: str) -> None:
    """The detector must recognise schema-level DDL, not just cross-schema refs.

    `_INFORMATION_COUPLING` only matches *references* (`schema="information"`,
    `"information.info_items"`), so a bare `op.execute("DROP SCHEMA
    information CASCADE")` slipped straight past it — the exact shape #271
    warns about.
    """
    assert _INFORMATION_SCHEMA_DDL.search(line), f"undetected information-schema DDL: {line!r}"


def test_information_ddl_pattern_ignores_prose() -> None:
    """Docstrings explaining the rule must not trip it (the genesis baseline does)."""
    prose = "the Archiver-owned ``information`` schema was dropped from production (#271)"
    assert _INFORMATION_SCHEMA_DDL.search(prose) is None


def test_no_migration_creates_or_drops_the_information_schema() -> None:
    """No migration may CREATE or DROP the `information` schema (#271).

    Production's dead copy was removed by an operator, deliberately not by a
    migration: `tests/conftest.py` builds a real `information` schema in
    `TEST_DATABASE_URL` from Archiver's alembic and keeps it alive between
    sessions (#150). A migration dropping it would break every test run and
    CI's `test` job; one creating it would put Watcher back in the business of
    mirroring Archiver's registry, which #234 ended.
    """
    offenders = _versions_matching(_INFORMATION_SCHEMA_DDL)
    assert not offenders, (
        f"migration(s) issue CREATE/DROP SCHEMA information: {offenders} — "
        "the schema is Archiver's; production's dead copy is an operator drop, "
        "see docs/MIGRATIONS.md"
    )


def test_no_information_schema_coupling() -> None:
    """No migration may create a cross-schema reference into `information` (#234).

    Watcher's database is standalone; Archiver owns `information` in a separate
    database. A migration reintroducing a cross-schema FK would break
    `alembic upgrade head` on a clean bootstrap — the regression #234 fixed.
    """
    offenders = _versions_matching(_INFORMATION_COUPLING)
    assert not offenders, (
        f"migration(s) reference the Archiver-owned `information` schema: {offenders}"
    )


def test_offline_upgrade_renders_all_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """`upgrade head` renders every model table offline, with no `information` DDL.

    Offline mode emits SQL without connecting, so this exercises the migration
    body (including the expression-index `literal_column` clauses) without a
    database or any privileges.
    """
    # env.py's get_url() reads DATABASE_URL first; a dummy Postgres URL fixes the
    # dialect for rendering and is never connected to in offline mode.
    monkeypatch.setenv("DATABASE_URL", "postgresql://watcher:watcher@localhost:5432/offline_render")
    config = Config(str(_REPO_ROOT / "alembic.ini"))

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    rendered = buffer.getvalue()

    expected_tables = {
        table.name for table in Base.metadata.tables.values() if table.schema in (None, "public")
    }
    assert expected_tables, "no model tables discovered — metadata import broken?"
    for table in expected_tables:
        assert re.search(rf"\bCREATE TABLE {table}\b", rendered), (
            f"offline upgrade did not render CREATE TABLE for {table}"
        )

    assert _INFORMATION_COUPLING.search(rendered) is None
    assert _INFORMATION_SCHEMA_DDL.search(rendered) is None


def test_ini_carries_no_database_url() -> None:
    """``alembic.ini`` must not name a database (#259).

    It shipped ``postgresql+asyncpg://watcher:watcher@localhost:5432/watcher``
    — the *production* database, with a guessable password — as the fallback
    ``get_url()`` reaches when ``DATABASE_URL`` is unset. A shell that forgot
    ``source scripts/load-env.sh`` therefore migrated production rather than
    failing. The value is now empty so the resolver raises instead.
    """
    parser = configparser.ConfigParser()
    parser.read(_REPO_ROOT / "alembic.ini")
    assert parser.get("alembic", "sqlalchemy.url", fallback="") == ""


@pytest.mark.integration
class TestMigrationCredentialPrecedence:
    """``alembic`` connects with the migration role, falling back to the app's.

    Exercised end-to-end against the live test database: ``alembic current``
    runs ``env.py`` online, so a case that passes proves the URL it *actually
    connected with*, not what a helper returned. The counter-URL names a
    closed port, so reading the wrong variable fails loudly rather than
    silently succeeding against the same database.
    """

    UNREACHABLE = "postgresql+asyncpg://watcher:watcher@127.0.0.1:1/nonexistent"

    @staticmethod
    def _config() -> Config:
        return Config(str(_REPO_ROOT / "alembic.ini"))

    def test_migration_url_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(MIGRATION_DATABASE_URL_ENV, os.environ["DATABASE_URL"])
        monkeypatch.setenv("DATABASE_URL", self.UNREACHABLE)
        command.current(self._config())

    def test_falls_back_to_database_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pre-#259 path: no migration credential configured anywhere."""
        monkeypatch.delenv(MIGRATION_DATABASE_URL_ENV, raising=False)
        command.current(self._config())

    def test_unreachable_url_really_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pins the counter-URL, without which the two cases above prove nothing."""
        monkeypatch.delenv(MIGRATION_DATABASE_URL_ENV, raising=False)
        monkeypatch.setenv("DATABASE_URL", self.UNREACHABLE)
        with pytest.raises(OSError):
            command.current(self._config())
