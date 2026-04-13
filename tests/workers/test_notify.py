"""Tests for dispatch_event_notifications — 4-source live dispatch."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications


@pytest.fixture(autouse=True)
def set_test_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APPRISE_SECRET_KEY", key)


def make_event(event_type=WatchEventType.CHANGE_DETECTED, watch_id=None):
    return WatchEvent(
        event_type=event_type,
        watch_id=watch_id or str(ULID()),
        watch_name="Test Watch",
        watch_url="https://example.com",
        occurred_at=datetime(2026, 4, 4, tzinfo=UTC),
        metadata={"added": ["s1"], "modified": [], "removed": []},
    )


def _scalar_result(value):
    """Mock a scalar result (for domain lookup)."""
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _empty_result():
    r = MagicMock()
    r.scalars.return_value.all.return_value = []
    return r


def _result_with(*items):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(items)
    return r


def _fake_template(tid=None):
    t = MagicMock()
    t.id = tid or str(ULID())
    t.apprise_url = "json://hooks.example.com/notify"
    return t


def _fake_local(cid=None):
    from src.core.crypto import encrypt_apprise_url
    from src.core.models.notification_config import WatchNotificationConfig

    c = MagicMock(spec=WatchNotificationConfig)
    c.id = cid or ULID()
    c.apprise_url = encrypt_apprise_url("json://local.example.com/notify")
    c.events = ["change_detected"]
    return c


def _ok_result():
    from src.core.notifications.dispatcher import DispatchResult

    return DispatchResult(success=True, reason="ok")


class TestNoDispatch:
    async def test_no_candidates_is_noop(self):
        """No sources → no dispatch, no audit."""
        session = AsyncMock(spec=AsyncSession)
        # domain lookup + global + domain + watch + local = 5 calls (domain is None so 4)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(None),  # domain lookup → None (no domain query)
                _empty_result(),  # global
                _empty_result(),  # watch templates
                _empty_result(),  # local
            ]
        )
        await dispatch_event_notifications(session, make_event())
        session.add.assert_not_called()


class TestGlobalDispatch:
    async def test_global_template_fires_for_any_watch(self):
        """Global templates dispatch to all watches regardless of WatchNcRef."""
        tpl = _fake_template()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(None),  # no domain
                _result_with(tpl),  # global templates
                _empty_result(),  # watch templates
                _empty_result(),  # local
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ):
            await dispatch_event_notifications(session, make_event())

        session.add.assert_called_once()  # audit

    async def test_global_source_label_in_audit(self):
        """Audit log records source as 'global'."""
        tpl = _fake_template(tid="GLOBALID")
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(None),
                _result_with(tpl),
                _empty_result(),
                _empty_result(),
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ):
            await dispatch_event_notifications(session, make_event())

        call_kwargs = session.add.call_args[0][0]
        assert call_kwargs.payload["results"][0]["source"] == "global"


class TestDomainDispatch:
    async def test_domain_template_fires_for_watch_in_domain(self):
        """Domain templates dispatch when watch has a matching effective_domain."""
        tpl = _fake_template()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result("example.com"),  # domain lookup
                _empty_result(),  # global
                _result_with(tpl),  # domain templates
                _empty_result(),  # watch templates
                _empty_result(),  # local
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ):
            await dispatch_event_notifications(session, make_event())

        session.add.assert_called_once()

    async def test_domain_template_not_queried_when_no_domain(self):
        """Domain query is skipped entirely when effective_domain is None."""
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(None),  # no domain → skip domain query
                _empty_result(),  # global
                _empty_result(),  # watch templates
                _empty_result(),  # local
            ]
        )
        await dispatch_event_notifications(session, make_event())
        # Only 4 execute calls, not 5
        assert session.execute.call_count == 4

    async def test_domain_source_label_in_audit(self):
        tpl = _fake_template()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result("example.com"),
                _empty_result(),
                _result_with(tpl),
                _empty_result(),
                _empty_result(),
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ):
            await dispatch_event_notifications(session, make_event())

        call_kwargs = session.add.call_args[0][0]
        assert call_kwargs.payload["results"][0]["source"] == "domain"


class TestWatchTemplateDispatch:
    async def test_watch_assigned_template_fires(self):
        """WatchNcRef-assigned templates dispatch for the specific watch."""
        tpl = _fake_template()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(None),
                _empty_result(),  # global
                _result_with(tpl),  # watch templates
                _empty_result(),  # local
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ):
            await dispatch_event_notifications(session, make_event())

        session.add.assert_called_once()

    async def test_watch_template_source_label(self):
        tpl = _fake_template()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(None),
                _empty_result(),
                _result_with(tpl),
                _empty_result(),
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ):
            await dispatch_event_notifications(session, make_event())

        call_kwargs = session.add.call_args[0][0]
        assert call_kwargs.payload["results"][0]["source"] == "watch_template"


class TestLocalDispatch:
    async def test_local_config_fires(self):
        local = _fake_local()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(local),
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ):
            await dispatch_event_notifications(session, make_event())

        session.add.assert_called_once()

    async def test_local_source_label(self):
        local = _fake_local()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(local),
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ):
            await dispatch_event_notifications(session, make_event())

        call_kwargs = session.add.call_args[0][0]
        assert call_kwargs.payload["results"][0]["source"] == "local"


class TestDeduplication:
    async def test_template_in_global_and_watch_nc_fires_once(self):
        """A template appearing in both global and WatchNcRef dispatches exactly once."""
        shared_id = str(ULID())
        tpl_global = _fake_template(tid=shared_id)
        tpl_watch = _fake_template(tid=shared_id)

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(None),
                _result_with(tpl_global),  # global
                _result_with(tpl_watch),  # watch templates (same id)
                _empty_result(),  # local
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ) as mock_dispatch:
            await dispatch_event_notifications(session, make_event())

        assert mock_dispatch.call_count == 1

    async def test_template_in_domain_and_watch_nc_fires_once(self):
        """Domain template also in WatchNcRef dispatches once."""
        shared_id = str(ULID())
        tpl_domain = _fake_template(tid=shared_id)
        tpl_watch = _fake_template(tid=shared_id)

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result("example.com"),
                _empty_result(),  # global
                _result_with(tpl_domain),  # domain
                _result_with(tpl_watch),  # watch (same id)
                _empty_result(),  # local
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ) as mock_dispatch:
            await dispatch_event_notifications(session, make_event())

        assert mock_dispatch.call_count == 1

    async def test_all_four_sources_distinct_fire_four_times(self):
        """Four distinct candidates (one per source) → 4 dispatches."""
        tpl_global = _fake_template()
        tpl_domain = _fake_template()
        tpl_watch = _fake_template()
        local = _fake_local()

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result("example.com"),
                _result_with(tpl_global),
                _result_with(tpl_domain),
                _result_with(tpl_watch),
                _result_with(local),
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            return_value=_ok_result(),
        ) as mock_dispatch:
            await dispatch_event_notifications(session, make_event())

        assert mock_dispatch.call_count == 4


class TestErrorHandling:
    async def test_dispatch_failure_does_not_raise(self):
        tpl = _fake_template()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _scalar_result(None),
                _result_with(tpl),
                _empty_result(),
                _empty_result(),
            ]
        )

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            await dispatch_event_notifications(session, make_event())
        # Did not raise
