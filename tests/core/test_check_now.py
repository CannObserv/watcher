"""Check-now's pre-flight as a core service (#371).

The route and the ``info.watch-check`` consumer share it, so the consumer
imports nothing from ``src.api`` (#378 deletes ``/api/v1``). Each refusal
mirrors a short-circuit in ``check_watched_item`` or its issue path: a check
that could not run is refused up front rather than accepted over a no-op.
"""

from datetime import UTC, datetime

import pytest

from src.core.check_now import CheckRefusal, preflight_check
from src.core.fetch_commands import create_fetch_command
from src.core.models.fetch_command import FetchCommandStatus
from tests.conftest import make_watched_item

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


async def test_a_runnable_item_is_not_refused(db_session):
    wi = await make_watched_item(db_session)

    verdict = await preflight_check(db_session, wi)

    assert verdict.refusal is None
    assert verdict.open_command is None


@pytest.mark.parametrize(
    ("fields", "refusal"),
    [
        ({"archived_at": NOW, "is_active": False}, CheckRefusal.ARCHIVED),
        ({"is_active": False}, CheckRefusal.PAUSED),
        ({"domain_suspended": True}, CheckRefusal.DOMAIN_SUSPENDED),
    ],
)
async def test_each_state_the_task_skips_is_refused(db_session, fields, refusal):
    wi = await make_watched_item(db_session, **fields)

    assert (await preflight_check(db_session, wi)).refusal is refusal


async def test_no_url_is_refused(db_session):
    wi = await make_watched_item(db_session)
    wi.effective_url = ""

    assert (await preflight_check(db_session, wi)).refusal is CheckRefusal.NO_URL


async def test_archived_wins_over_paused(db_session):
    """An archived item is inactive too; the route has always said "archived"."""
    wi = await make_watched_item(
        db_session, archived_at=NOW, is_active=False, domain_suspended=True
    )

    assert (await preflight_check(db_session, wi)).refusal is CheckRefusal.ARCHIVED


@pytest.mark.parametrize(
    "status",
    [
        FetchCommandStatus.PENDING_PUBLISH,
        FetchCommandStatus.IN_FLIGHT,
        FetchCommandStatus.PROCESSING,
    ],
)
async def test_an_open_command_is_refused_and_returned(db_session, status):
    """The row comes back so the route can quote its age."""
    wi = await make_watched_item(db_session)
    row = await create_fetch_command(db_session, wi, now=NOW)
    row.status = status
    await db_session.commit()

    verdict = await preflight_check(db_session, wi)

    assert verdict.refusal is CheckRefusal.FETCH_OPEN
    assert verdict.open_command.command_id == row.command_id


async def test_a_settled_command_does_not_refuse(db_session):
    wi = await make_watched_item(db_session)
    row = await create_fetch_command(db_session, wi, now=NOW)
    row.status = FetchCommandStatus.SUCCEEDED
    await db_session.commit()

    assert (await preflight_check(db_session, wi)).refusal is None
