"""Tests for template-ref dispatch union in dispatch_event_notifications."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.notifications.events import WatchEvent, WatchEventType


def _make_event():
    return WatchEvent(
        event_type=WatchEventType.CHANGE_DETECTED,
        watch_id="01J000000000000000000000AA",
        watch_name="Test Watch",
        watch_url="https://example.com",
        occurred_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_dispatch_includes_template_refs():
    """Templates assigned via watch_nc_refs are dispatched even when no local configs."""
    from src.workers.notify import dispatch_event_notifications

    event = _make_event()

    mock_local = MagicMock()
    mock_local.scalars.return_value.all.return_value = []

    mock_template = MagicMock()
    fake_template = MagicMock()
    fake_template.id = "01J000000000000000000000BB"
    fake_template.apprise_url = "json://hooks.example.com/notify"
    mock_template.scalars.return_value.all.return_value = [fake_template]

    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(side_effect=[mock_local, mock_template])

    with patch("src.workers.notify.dispatch_event", new_callable=AsyncMock) as mock_dispatch:
        mock_dispatch.return_value = MagicMock(success=True, reason="ok")
        await dispatch_event_notifications(session, event)

    mock_dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_includes_both_local_and_template():
    """Both local configs and template refs are dispatched."""
    from src.workers.notify import dispatch_event_notifications

    event = _make_event()

    mock_local = MagicMock()
    fake_local = MagicMock()
    fake_local.id = "01J000000000000000000000CC"
    fake_local.apprise_url = "json://local.example.com/notify"
    mock_local.scalars.return_value.all.return_value = [fake_local]

    mock_template = MagicMock()
    fake_template = MagicMock()
    fake_template.id = "01J000000000000000000000DD"
    fake_template.apprise_url = "json://template.example.com/notify"
    mock_template.scalars.return_value.all.return_value = [fake_template]

    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(side_effect=[mock_local, mock_template])

    with patch("src.workers.notify.dispatch_event", new_callable=AsyncMock) as mock_dispatch:
        mock_dispatch.return_value = MagicMock(success=True, reason="ok")
        await dispatch_event_notifications(session, event)

    assert mock_dispatch.call_count == 2
