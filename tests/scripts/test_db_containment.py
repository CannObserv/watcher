"""The test role reaches no production database (#388).

``db_safety`` checks the *name* a URL carries. That says nothing about the
credential: until #388 the suite logged in as ``watcher``, the production
schema owner, so one misedited URL or a regressed name check was all that stood
between pytest's ``DROP SCHEMA … CASCADE`` and production. This checks the
role's *capability* instead, from inside the cluster.

The integration tests here run against ``TEST_DATABASE_URL``; the first one is
the issue's done-when check, red for as long as that URL logs in as a role
that can connect to ``watcher``.
"""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from scripts import db_containment
from src.core import db_safety
from tests.conftest import TEST_DATABASE_URL


@pytest.fixture
async def conn():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_the_test_role_reaches_no_production_database(conn) -> None:
    """The done-when check: no database outside ``_test``/``_dev`` and the
    maintenance set is one this role can connect to or owns."""
    assert await db_containment.reachable_production_databases(conn) == []


@pytest.mark.integration
async def test_a_database_without_the_suffix_is_reported(conn) -> None:
    """Detection works: read with a suffix nothing carries, the database this
    role is connected to — which it can certainly reach — is reported."""
    reached = await db_containment.reachable_production_databases(conn, suffixes=("_nomatch",))
    assert (await conn.exec_driver_sql("SELECT current_database()")).scalar() in reached


@pytest.mark.integration
async def test_the_maintenance_databases_are_exempt_by_name(conn) -> None:
    """``postgres`` grants PUBLIC CONNECT by default, as CI's container does to
    its superuser: without the exemption no ordinary role would pass."""
    assert "postgres" in await db_containment.reachable_production_databases(conn, exempt=())
    assert set(db_containment.EXEMPT_DATABASES) == {"postgres", "template0", "template1"}


@pytest.mark.integration
async def test_assert_contained_names_the_role_and_the_databases(conn) -> None:
    with pytest.raises(db_containment.RoleReachesProduction) as caught:
        await db_containment.assert_contained(conn, suffixes=("_nomatch",))
    role = (await conn.exec_driver_sql("SELECT current_user")).scalar()
    assert repr(role) in str(caught.value)
    assert "#388" in str(caught.value)


def test_the_suffixes_are_db_safetys() -> None:
    """One rule for the name and the capability: a suffix added to one is in both."""
    assert db_containment.NON_PRODUCTION_SUFFIXES is db_safety.NON_PRODUCTION_SUFFIXES


class TestMain:
    """The CLI ``scripts/dev_server.sh`` runs before it touches the database."""

    def test_no_url_is_a_usage_error(self, capsys, monkeypatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert db_containment.main([]) == 2
        assert "DATABASE_URL" in capsys.readouterr().err

    def test_a_contained_role_passes(self, monkeypatch) -> None:
        monkeypatch.setattr(db_containment, "assert_url_contained", _refusing(None))
        assert db_containment.main(["postgresql://u:secret@h/x_dev"]) == 0

    def test_a_reaching_role_is_refused_without_printing_the_url(self, capsys, monkeypatch) -> None:
        refusal = db_containment.RoleReachesProduction("role 'u' reaches ['watcher']")
        monkeypatch.setattr(db_containment, "assert_url_contained", _refusing(refusal))
        assert db_containment.main(["postgresql://u:secret@h/x_dev"]) == 1
        err = capsys.readouterr().err
        assert "['watcher']" in err
        assert "secret" not in err

    def test_the_url_defaults_to_database_url(self, monkeypatch) -> None:
        seen = []
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/x_dev")
        monkeypatch.setattr(db_containment, "assert_url_contained", _refusing(None, seen))
        assert db_containment.main([]) == 0
        assert seen == ["postgresql://u:p@h/x_dev"]


def _refusing(refusal: Exception | None, seen: list[str] | None = None):
    async def assert_url_contained(url: str) -> None:
        if seen is not None:
            seen.append(url)
        if refusal is not None:
            raise refusal

    return assert_url_contained
