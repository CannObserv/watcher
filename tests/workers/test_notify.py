"""Tests for dispatch_event_notifications — 4-source dispatch via notifier.

After Phase 5 (#137), every candidate goes through the notifier service —
the local Apprise dispatcher is gone. After Phase 5 Stage 9, Snapshot/Change
tables are gone — unified diff is always empty. These tests assert the
dispatcher queries all four sources, dedupes templates, threads `content_config`
through, and never raises.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from notifier_client.generated.models.dispatch_out_status import DispatchOutStatus
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications


@pytest.fixture(autouse=True)
def notifier_env(monkeypatch):
    monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
    monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")


def make_event(event_type=WatchEventType.CHANGE_DETECTED, watch_id=None):
    return WatchEvent(
        event_type=event_type,
        watch_id=watch_id or str(ULID()),
        watch_name="Test Watch",
        watch_url="https://example.com",
        occurred_at=datetime(2026, 4, 4, tzinfo=UTC),
        metadata={"added": ["s1"], "modified": [], "removed": []},
    )


def _watch_meta_result(value, *, watched_item_id=None):
    """Mock the row result for the dispatcher's
    `select(WatchedItem.domain_name, Watch.watched_item_id)` lookup."""
    r = MagicMock()
    r.one_or_none.return_value = None if value is None else (value, watched_item_id)
    return r


def _empty_result():
    r = MagicMock()
    r.scalars.return_value.all.return_value = []
    return r


def _result_with(*items):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(items)
    return r


def _fake_template(tid=None, *, remote_channel_id=None):
    t = MagicMock()
    t.id = tid or str(ULID())
    t.content_config = None
    t.remote_channel_id = remote_channel_id or str(ULID())
    return t


def _fake_local(cid=None, *, remote_channel_id=None):
    from src.core.models.notification_config import WatchNotificationConfig

    c = MagicMock(spec=WatchNotificationConfig)
    c.id = cid or ULID()
    c.events = ["change_detected"]
    c.content_config = None
    c.remote_channel_id = remote_channel_id or str(ULID())
    return c


def _make_dispatch_out(status="succeeded"):
    out = MagicMock()
    out.id = str(ULID())
    out.status = DispatchOutStatus.SUCCEEDED if status == "succeeded" else DispatchOutStatus.FAILED
    out.attempts = []
    return out


def _mock_notifier_client(dispatch_return=None):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.dispatch = AsyncMock(return_value=dispatch_return or _make_dispatch_out())
    return client


class TestNoDispatch:
    async def test_no_candidates_is_noop(self):
        """No sources → no dispatch, no audit."""
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _empty_result(),
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
                _watch_meta_result(None),
                _result_with(tpl),
                _empty_result(),
                _empty_result(),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())

        client.dispatch.assert_called_once()
        session.add.assert_called_once()  # audit

    async def test_global_source_label_in_audit(self):
        """Audit log records source as 'global'."""
        tpl = _fake_template(tid="GLOBALID")
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _result_with(tpl),
                _empty_result(),
                _empty_result(),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())

        call_kwargs = session.add.call_args[0][0]
        assert call_kwargs.payload["results"][0]["source"] == "global"


class TestDomainDispatch:
    async def test_domain_template_fires_for_watch_in_domain(self):
        """Domain templates dispatch when watch has a matching domain_name."""
        tpl = _fake_template()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result("example.com"),
                _empty_result(),
                _result_with(tpl),
                _empty_result(),
                _empty_result(),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())

        session.add.assert_called_once()

    async def test_domain_template_not_queried_when_no_domain(self):
        """Domain query is skipped entirely when domain_name is None."""
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _empty_result(),
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
                _watch_meta_result("example.com"),
                _empty_result(),
                _result_with(tpl),
                _empty_result(),
                _empty_result(),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
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
                _watch_meta_result(None),
                _empty_result(),
                _result_with(tpl),
                _empty_result(),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())

        session.add.assert_called_once()

    async def test_watch_template_source_label(self):
        tpl = _fake_template()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _result_with(tpl),
                _empty_result(),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())

        call_kwargs = session.add.call_args[0][0]
        assert call_kwargs.payload["results"][0]["source"] == "watch_template"


class TestLocalDispatch:
    async def test_local_config_fires(self):
        local = _fake_local()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(local),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())

        session.add.assert_called_once()

    async def test_local_source_label(self):
        local = _fake_local()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(local),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
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
                _watch_meta_result(None),
                _result_with(tpl_global),
                _result_with(tpl_watch),
                _empty_result(),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())

        assert client.dispatch.call_count == 1

    async def test_template_in_domain_and_watch_nc_fires_once(self):
        """Domain template also in WatchNcRef dispatches once."""
        shared_id = str(ULID())
        tpl_domain = _fake_template(tid=shared_id)
        tpl_watch = _fake_template(tid=shared_id)

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result("example.com"),
                _empty_result(),
                _result_with(tpl_domain),
                _result_with(tpl_watch),
                _empty_result(),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())

        assert client.dispatch.call_count == 1

    async def test_all_four_sources_distinct_fire_four_times(self):
        """Four distinct candidates (one per source) → 4 dispatches."""
        tpl_global = _fake_template()
        tpl_domain = _fake_template()
        tpl_watch = _fake_template()
        local = _fake_local()

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result("example.com"),
                _result_with(tpl_global),
                _result_with(tpl_domain),
                _result_with(tpl_watch),
                _result_with(local),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())

        assert client.dispatch.call_count == 4


class TestContentConfig:
    @pytest.mark.asyncio
    async def test_content_config_body_used_in_dispatch(self):
        """When a config has content_config, build_body is called and the result forwarded."""
        event = make_event(WatchEventType.CHANGE_DETECTED)

        content_cfg = ContentConfig(default=ContentOptions(include_domain=True))
        content_cfg_dict = content_cfg.model_dump()

        mock_config = MagicMock()
        mock_config.content_config = content_cfg_dict
        mock_config.remote_channel_id = str(ULID())

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(mock_config),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, event)

        client.dispatch.assert_called_once()
        body = client.dispatch.call_args.kwargs["body_template"]
        # include_domain=True but no effective_domain in metadata → no domain section added
        assert "example.com" in body
        assert "Domain:" not in body

    @pytest.mark.asyncio
    async def test_null_content_config_renders_default_body(self):
        """content_config=None — body is rendered from the default template."""
        event = make_event(WatchEventType.CHANGE_DETECTED)

        mock_config = MagicMock()
        mock_config.content_config = None
        mock_config.remote_channel_id = str(ULID())

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(mock_config),
            ]
        )

        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, event)

        client.dispatch.assert_called_once()
        body = client.dispatch.call_args.kwargs["body_template"]
        assert body
        assert "example.com" in body


class TestErrorHandling:
    async def test_dispatch_failure_does_not_raise(self):
        """An exception inside dispatch_via_notifier is caught and recorded."""
        tpl = _fake_template()
        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _result_with(tpl),
                _empty_result(),
                _empty_result(),
            ]
        )

        client = _mock_notifier_client()
        client.dispatch = AsyncMock(side_effect=Exception("boom"))

        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())
        # Did not raise.
