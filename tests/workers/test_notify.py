"""Tests for dispatch_event_notifications — single visibility-scoped dispatch (#200).

Post-#200 every notification target is one ``NotificationTemplate`` row with an
intrinsic ``visibility`` (global / domain / watched_item). The dispatcher
resolves the WatchedItem via ``session.get`` and runs a single query selecting
the active templates whose ``events`` include the event and whose visibility
matches. One query → each row fires once (id-dedup is automatic); multiple
templates may target the same channel and all fire (#200 F2). These tests assert
visibility resolution, source labels, content_config threading, and that
dispatch never raises.
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


def make_event(event_type=WatchEventType.CHANGE_DETECTED, watched_item_id=None):
    return WatchEvent(
        event_type=event_type,
        watched_item_id=watched_item_id or str(ULID()),
        item_name="Test Watch",
        item_url="https://example.com",
        occurred_at=datetime(2026, 4, 4, tzinfo=UTC),
        metadata={"added": ["s1"], "modified": [], "removed": []},
    )


def _result_with(*items):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(items)
    return r


def _wi(domain_name=None):
    """Mock the WatchedItem returned by ``session.get`` in the dispatcher."""
    wi = MagicMock()
    wi.domain_name = domain_name
    return wi


def _setup_session(*, domain=None, templates=()):
    """Build an AsyncMock session: ``session.get(WatchedItem)`` then one ``execute``."""
    session = AsyncMock(spec=AsyncSession)
    session.get = AsyncMock(return_value=_wi(domain))
    session.execute = AsyncMock(return_value=_result_with(*templates))
    return session


def _fake_template(visibility="global", *, tid=None, remote_channel_id=None, content_config=None):
    t = MagicMock()
    t.id = tid or str(ULID())
    t.visibility = visibility
    t.content_config = content_config
    t.remote_channel_id = remote_channel_id or str(ULID())
    return t


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
        """No matching templates → no dispatch, no audit."""
        session = _setup_session()
        await dispatch_event_notifications(session, make_event())
        session.add.assert_not_called()

    async def test_single_query_regardless_of_domain(self):
        """Exactly one execute() runs whether or not the WatchedItem has a domain."""
        session = _setup_session(domain="example.com")
        await dispatch_event_notifications(session, make_event())
        assert session.execute.call_count == 1


class TestVisibilityDispatch:
    async def test_global_template_fires(self):
        session = _setup_session(templates=[_fake_template("global")])
        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())
        client.dispatch.assert_called_once()
        session.add.assert_called_once()  # audit

    async def test_global_source_label_in_audit(self):
        session = _setup_session(templates=[_fake_template("global", tid="GLOBALID")])
        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())
        audit_row = session.add.call_args[0][0]
        assert audit_row.payload["results"][0]["source"] == "global"

    async def test_domain_template_fires_with_domain_source_label(self):
        session = _setup_session(domain="example.com", templates=[_fake_template("domain")])
        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())
        audit_row = session.add.call_args[0][0]
        assert audit_row.payload["results"][0]["source"] == "domain"

    async def test_watched_item_template_fires_with_watched_item_source_label(self):
        session = _setup_session(templates=[_fake_template("watched_item")])
        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())
        audit_row = session.add.call_args[0][0]
        assert audit_row.payload["results"][0]["source"] == "watched_item"


class TestMultipleTemplates:
    async def test_distinct_templates_each_fire(self):
        """Three matching templates across visibilities → three dispatches."""
        session = _setup_session(
            domain="example.com",
            templates=[
                _fake_template("global"),
                _fake_template("domain"),
                _fake_template("watched_item"),
            ],
        )
        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())
        assert client.dispatch.call_count == 3

    async def test_two_templates_same_channel_both_fire(self):
        """F2 ratified: two templates targeting one channel are NOT suppressed."""
        shared_channel = str(ULID())
        session = _setup_session(
            templates=[
                _fake_template("global", remote_channel_id=shared_channel),
                _fake_template("watched_item", remote_channel_id=shared_channel),
            ],
        )
        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())
        assert client.dispatch.call_count == 2
        channels = {c.kwargs["channel_ids"][0] for c in client.dispatch.call_args_list}
        assert channels == {shared_channel}


class TestContentConfig:
    async def test_content_config_body_used_in_dispatch(self):
        """When a template has content_config, build_body uses it and forwards the result."""
        content_cfg = ContentConfig(default=ContentOptions(include_domain=True))
        template = _fake_template("global", content_config=content_cfg.model_dump())
        session = _setup_session(templates=[template])
        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event(WatchEventType.CHANGE_DETECTED))
        client.dispatch.assert_called_once()
        body = client.dispatch.call_args.kwargs["body_template"]
        # include_domain=True but no domain_name in metadata → no domain section added
        assert "example.com" in body
        assert "Domain:" not in body

    async def test_null_content_config_renders_default_body(self):
        template = _fake_template("global", content_config=None)
        session = _setup_session(templates=[template])
        client = _mock_notifier_client()
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event(WatchEventType.CHANGE_DETECTED))
        client.dispatch.assert_called_once()
        body = client.dispatch.call_args.kwargs["body_template"]
        assert body
        assert "example.com" in body


class TestErrorHandling:
    async def test_dispatch_failure_does_not_raise(self):
        """An exception inside dispatch_via_notifier is caught and recorded."""
        session = _setup_session(templates=[_fake_template("global")])
        client = _mock_notifier_client()
        client.dispatch = AsyncMock(side_effect=Exception("boom"))
        with patch("src.core.notifications.notify.get_notifier_client", return_value=client):
            await dispatch_event_notifications(session, make_event())
