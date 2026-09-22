"""The test database has production's shape — no Archiver ``information`` schema.

Production's ``watcher`` database carries no ``information`` schema: #271 dropped
the dead copy, and the genesis baseline (#234) never creates one.
``watched_items.archiver_info_item_id`` / ``archiver_info_source_id`` are bare
ULIDs naming rows in *Archiver's* database, with no foreign key behind them.

Until #311 the suite built that schema anyway, by subprocess-running the sibling
checkout's alembic, and its factories wrote ``information.*`` rows only to mint
the two ULIDs. That coupled every integration run to someone else's checkout,
``uv`` environment and wheelhouse, and handed tests a table production does not
have — a query against ``information.*`` would pass here and fail live.

``test_engine`` now drops any leftover copy (the #150 cache kept it alive
between sessions by design), so this also proves an existing test database
self-heals.
"""

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def test_test_database_carries_no_information_schema(db_session):
    found = (
        await db_session.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = 'information'"))
    ).scalar_one_or_none()
    assert found is None, (
        "the test database carries an `information` schema that production does "
        "not have (#271) — nothing in watcher may build or depend on it (#311)"
    )
