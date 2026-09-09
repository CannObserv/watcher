"""What Watcher does when the broker denies a write with ``NOPERM …`` (#290).

CannObserv/broker#1 D3 puts per-service ACL users in front of the broker
(CannObserv/broker#2). A rule that is mistyped or too narrow surfaces at a
publish site as ``redis.exceptions.NoPermissionError`` on a perfectly valid
row — an operator-resolvable outage, not poison, which is the same argument
#288 accepted for ``OutOfMemoryError``: the event publishes unchanged the
moment the rule is widened.

Unlike ``test_broker_oom.py``, nothing here was measured — the ACL users are
not deployed yet, so the message strings are the wire form redis-server
documents rather than a capture. What is being pinned is classification, not
the broker's wording.

``NoPermissionError`` is a ``ResponseError`` subclass, disjoint from the
connection errors, so a producer classifying by ``ConnectionError`` /
``TimeoutError`` sorts a denied publish into the permanent bucket. For
``content.revisions`` that costs no data — ``MAX_PUBLISH_ATTEMPTS`` is a
backstop, not a budget — but it does back the row off to the 1 h ceiling and
log ``"transient": false``, which is the field an operator greps during the
cutover to tell a config problem from a poison row.

The consumer side is already covered by the ``noperm`` parametrisations in
``tests/workers/test_bus_reconnect.py``; the drain's own retry and
ceiling-exemption behaviour by ``BROKER_REFUSALS`` in
``tests/workers/test_source_revisions_drain.py``. What is left, and what this
file holds, is the classification itself and the other three publish paths.

``integration`` is marked **per class**: the classification assertions need no
database, and they are the guards that most want to run in the default pass.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.asyncio import Redis
from redis.exceptions import NoPermissionError, ResponseError

import src.workers.fetch_policy as fetch_policy_mod
import src.workers.watch_status as watch_status_mod
from src.core.fetch_commands import create_fetch_command
from src.core.models.domain import Domain
from src.core.models.fetch_command import FetchCommandStatus
from src.workers.fetch_commands import publish_pending_fetch_commands
from src.workers.fetch_policy import publish_fetch_policy
from src.workers.source_revisions_drain import _TRANSIENT_PUBLISH_ERRORS
from src.workers.watch_status import publish_watch_status
from tests.conftest import make_watched_item

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)

# The wire form of a denied command. Not a capture: broker#2 is not deployed.
NOPERM_MESSAGE = "NOPERM this user has no permissions to run the 'xadd' command"


def _noperm_client() -> MagicMock:
    """A client whose every ``XADD`` is denied the way an ACL user denies it.

    ``spec=Redis`` so a test that grows a second bus call fails by naming the
    method it did not stub.
    """
    client = MagicMock(spec=Redis)
    client.xadd = AsyncMock(side_effect=NoPermissionError(NOPERM_MESSAGE))
    return client


def _mock_session_factory(db_session):
    @asynccontextmanager
    async def _ctx():
        yield db_session

    factory = MagicMock()
    factory.return_value = _ctx()
    return factory


def _wire_task_bus(module, db_session, monkeypatch) -> None:
    """Point a periodic publisher's env gate and shared client at the fake."""
    monkeypatch.setattr(module, "bus_disabled_reason", lambda: None)
    monkeypatch.setattr(module, "get_shared_bus_client", _noperm_client)
    monkeypatch.setattr(module, "get_session_factory", lambda: _mock_session_factory(db_session))


class TestTheDeniedPublishIsNotAConnectionError:
    """The trap: NOPERM arrives as a ``ResponseError``, so a producer whose
    transient set is ``(ConnectionError, TimeoutError)`` files an
    operator-resolvable outage as possibly-permanent."""

    def test_no_permission_is_a_response_error_not_a_connection_or_timeout_error(self):
        exc = NoPermissionError(NOPERM_MESSAGE)
        assert isinstance(exc, ResponseError)
        assert not isinstance(exc, (ConnectionError, TimeoutError))

    def test_the_revisions_drain_classifies_it_transient_anyway(self):
        assert NoPermissionError in _TRANSIENT_PUBLISH_ERRORS


@pytest.mark.integration
class TestTheOtherProducersUnderNoPerm:
    """#288's table says the other three publish paths do not classify at all,
    and that leaving the row pending or waiting for the next tick is already
    the right answer for a denied publish. #290 inferred that rather than
    checking it; these check it."""

    async def test_the_fetch_command_sweep_keeps_the_row_pending_publish(self, db_session):
        """``content.fetch`` is a command stream: a dropped publish is a fetch
        that never happens. No attempt counter, so no N at which a denied
        command is abandoned — ten sweeps stands in for indefinitely."""
        wi = await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        row = await create_fetch_command(db_session, wi, now=NOW)
        await db_session.flush()
        client = _noperm_client()

        for _ in range(10):
            result = await publish_pending_fetch_commands(session=db_session, bus_client=client)
            assert result == {"published": 0}

        assert row.status == FetchCommandStatus.PENDING_PUBLISH
        assert client.xadd.await_count == 10

    async def test_the_fetch_policy_task_lets_the_denial_escape(self, db_session, monkeypatch):
        """LWW full set, no outbox: the job must fail loudly so the next cron
        tick republishes everything. A swallowed denial reports a stale set as
        delivered."""
        db_session.add(Domain(name="lcb.wa.gov", min_interval=3.0))
        await db_session.flush()
        _wire_task_bus(fetch_policy_mod, db_session, monkeypatch)

        with pytest.raises(NoPermissionError):
            await publish_fetch_policy()

    async def test_the_watch_status_task_lets_the_denial_escape(self, db_session, monkeypatch):
        await make_watched_item(db_session, primary_url="https://lcb.wa.gov/notices")
        _wire_task_bus(watch_status_mod, db_session, monkeypatch)

        with pytest.raises(NoPermissionError):
            await publish_watch_status()
