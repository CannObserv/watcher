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

import io
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from alembic import command
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


@pytest.fixture(scope="module")
def script_directory() -> ScriptDirectory:
    """Alembic ScriptDirectory loaded from the repo's alembic.ini."""
    return ScriptDirectory.from_config(Config(str(_REPO_ROOT / "alembic.ini")))


def test_single_head(script_directory: ScriptDirectory) -> None:
    """The chain must have exactly one head (no branch/merge divergence)."""
    heads = script_directory.get_heads()
    assert len(heads) == 1, f"expected a single head, found {heads}"


def test_head_is_a_true_base(script_directory: ScriptDirectory) -> None:
    """After the #234 squash the head is also the base (down_revision is None)."""
    (head,) = script_directory.get_heads()
    assert script_directory.get_revision(head).down_revision is None
    assert script_directory.get_bases() == [head]


def test_no_information_schema_coupling() -> None:
    """No migration may create a cross-schema reference into `information` (#234).

    Watcher's database is standalone; Archiver owns `information` in a separate
    database. A migration reintroducing a cross-schema FK would break
    `alembic upgrade head` on a clean bootstrap — the regression #234 fixed.
    """
    offenders = [
        path.name
        for path in _VERSIONS_DIR.glob("*.py")
        if _INFORMATION_COUPLING.search(path.read_text())
    ]
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
