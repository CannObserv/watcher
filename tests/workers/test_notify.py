"""Tests for dispatch_event_notifications — 4-source dispatch via notifier.

After Phase 5 (#137), every candidate goes through the notifier service —
the local Apprise dispatcher is gone. These tests assert the dispatcher
queries all four sources, dedupes templates, threads `content_config`
through, lazy-loads the unified diff once per event, and never raises.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from notifier_client.generated.models.dispatch_out_status import DispatchOutStatus
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.models.watch import ContentType
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import (
    _load_event_unified_diff,
    dispatch_event_notifications,
)


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


def _watch_meta_result(value, *, content_type=None):
    """Mock the row result for the dispatcher's
    `select(Watch.effective_domain, Watch.content_type)` lookup."""
    r = MagicMock()
    r.one_or_none.return_value = None if value is None else (value, content_type)
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
        """Domain templates dispatch when watch has a matching effective_domain."""
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
        """Domain query is skipped entirely when effective_domain is None."""
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


class _FakeStorage:
    """Minimal in-memory StorageBackend for _load_event_unified_diff tests."""

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        raise_on_load: Exception | None = None,
    ):
        self.files = files or {}
        self.raise_on_load = raise_on_load
        self.load_calls: list[str] = []

    def save(self, path, content):  # pragma: no cover — unused
        self.files[path] = content

    def load(self, path):
        self.load_calls.append(path)
        if self.raise_on_load is not None:
            raise self.raise_on_load
        return self.files[path]

    def exists(self, path):  # pragma: no cover — unused
        return path in self.files

    def size(self, path):  # pragma: no cover — unused
        return len(self.files[path])

    def snapshot_path(self, watch_id, snapshot_id, extension):  # pragma: no cover — unused
        return f"snapshots/{watch_id}/{snapshot_id}.{extension}"


def _change_event_with_id(change_id):
    event = make_event(WatchEventType.CHANGE_DETECTED)
    event.metadata["change_id"] = str(change_id)
    return event


class TestLoadEventUnifiedDiff:
    """Direct unit tests for _load_event_unified_diff covering each early-return
    branch and the storage-error fallback. The dispatcher invokes this function
    unguarded — every failure mode must return "" rather than raise."""

    @pytest.mark.asyncio
    async def test_no_change_id_returns_empty(self):
        event = make_event(WatchEventType.CHANGE_DETECTED)
        event.metadata.pop("change_id", None)
        session = AsyncMock(spec=AsyncSession)
        result = await _load_event_unified_diff(session, event)
        assert result == ""
        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_change_id_returns_empty(self):
        event = make_event(WatchEventType.CHANGE_DETECTED)
        event.metadata["change_id"] = "not-a-ulid"
        session = AsyncMock(spec=AsyncSession)
        result = await _load_event_unified_diff(session, event)
        assert result == ""
        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_change_row_returns_empty(self):
        event = _change_event_with_id(ULID())
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(return_value=None)
        result = await _load_event_unified_diff(session, event)
        assert result == ""

    @pytest.mark.asyncio
    async def test_missing_snapshot_returns_empty(self):
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, None, MagicMock()])
        result = await _load_event_unified_diff(session, event)
        assert result == ""

    @pytest.mark.asyncio
    async def test_missing_text_path_returns_empty_for_non_html(self):
        """Non-HTML watch with empty text_path → bail (no fallback to raw)."""
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock(text_path="", storage_path="snapshots/w/prev.pdf")
        curr = MagicMock(text_path="some/path", storage_path="snapshots/w/curr.pdf")
        watch = MagicMock(content_type=ContentType.PDF)
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, prev, curr, watch])
        result = await _load_event_unified_diff(session, event)
        assert result == ""

    @pytest.mark.asyncio
    async def test_storage_error_returns_empty_does_not_raise(self):
        """RuntimeError (a non-OSError) must be caught — verifies CR #2 broadened catch."""
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock(text_path="snapshots/w/prev.txt", storage_path="snapshots/w/prev.txt")
        curr = MagicMock(text_path="snapshots/w/curr.txt", storage_path="snapshots/w/curr.txt")
        watch = MagicMock(content_type=ContentType.PDF)
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, prev, curr, watch])
        storage = _FakeStorage(raise_on_load=RuntimeError("backend exploded"))
        result = await _load_event_unified_diff(session, event, storage=storage)
        assert result == ""

    @pytest.mark.asyncio
    async def test_happy_path_non_html_uses_text_path(self):
        """PDF/file watches: diff the stored extracted text via `text_path`."""
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock(text_path="snapshots/w/prev.txt", storage_path="snapshots/w/prev.pdf")
        curr = MagicMock(text_path="snapshots/w/curr.txt", storage_path="snapshots/w/curr.pdf")
        watch = MagicMock(content_type=ContentType.PDF)
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, prev, curr, watch])
        storage = _FakeStorage(
            files={
                "snapshots/w/prev.txt": b"alpha\nbeta\n",
                "snapshots/w/curr.txt": b"alpha\nbeta-changed\n",
            }
        )
        result = await _load_event_unified_diff(session, event, storage=storage)
        assert result.startswith("--- content")
        assert "+beta-changed" in result
        assert storage.load_calls == ["snapshots/w/prev.txt", "snapshots/w/curr.txt"]

    @pytest.mark.asyncio
    async def test_html_watch_diffs_prettified_raw_html(self):
        """HTML watches diff `storage_path` (raw HTML) prettified via
        `normalize_html` so notification output mirrors the dashboard's
        Raw-mode diff (#118) — no long unwrapped lines."""
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock(text_path="snapshots/w/prev.txt", storage_path="snapshots/w/prev.html")
        curr = MagicMock(text_path="snapshots/w/curr.txt", storage_path="snapshots/w/curr.html")
        watch = MagicMock(content_type=ContentType.HTML)
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, prev, curr, watch])
        prev_html = b"<html><body><p>alpha</p><p>beta</p></body></html>"
        curr_html = b"<html><body><p>alpha</p><p>beta-changed</p></body></html>"
        storage = _FakeStorage(
            files={
                "snapshots/w/prev.html": prev_html,
                "snapshots/w/curr.html": curr_html,
            }
        )
        result = await _load_event_unified_diff(session, event, storage=storage)
        assert result.startswith("--- content")
        diff_lines = result.splitlines()
        assert any(line.startswith("-") and "<p>beta</p>" in line for line in diff_lines)
        assert any(line.startswith("+") and "<p>beta-changed</p>" in line for line in diff_lines)
        assert len(diff_lines) >= 8
        assert storage.load_calls == ["snapshots/w/prev.html", "snapshots/w/curr.html"]

    @pytest.mark.asyncio
    async def test_html_watch_normalize_html_failure_falls_back_to_raw(self, monkeypatch):
        """When `normalize_html` raises, the function falls back to diffing
        the un-prettified raw HTML rather than returning empty."""
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock(text_path="snapshots/w/prev.txt", storage_path="snapshots/w/prev.html")
        curr = MagicMock(text_path="snapshots/w/curr.txt", storage_path="snapshots/w/curr.html")
        watch = MagicMock(content_type=ContentType.HTML)
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, prev, curr, watch])
        storage = _FakeStorage(
            files={
                "snapshots/w/prev.html": b"<html><body>alpha</body></html>",
                "snapshots/w/curr.html": b"<html><body>beta</body></html>",
            }
        )

        def boom(_text):
            raise RuntimeError("html parse exploded")

        monkeypatch.setattr("src.core.notifications.notify.normalize_html", boom)

        result = await _load_event_unified_diff(session, event, storage=storage)
        assert result.startswith("--- content")
        assert "-<html><body>alpha</body></html>" in result
        assert "+<html><body>beta</body></html>" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "prev_storage_path,curr_storage_path",
        [("", "snapshots/w/curr.html"), ("snapshots/w/prev.html", "")],
    )
    async def test_html_watch_with_missing_storage_path_returns_empty(
        self, prev_storage_path, curr_storage_path
    ):
        """HTML watch missing either side's storage_path → no fallback."""
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock(text_path="snapshots/w/prev.txt", storage_path=prev_storage_path)
        curr = MagicMock(text_path="snapshots/w/curr.txt", storage_path=curr_storage_path)
        watch = MagicMock(content_type=ContentType.HTML)
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, prev, curr, watch])
        result = await _load_event_unified_diff(session, event)
        assert result == ""

    @pytest.mark.asyncio
    async def test_content_type_kwarg_skips_watch_fetch(self):
        """When `content_type` is passed, the function must not fetch the
        Watch row — `dispatch_event_notifications` already loaded it."""
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock(text_path="snapshots/w/prev.txt", storage_path="snapshots/w/prev.html")
        curr = MagicMock(text_path="snapshots/w/curr.txt", storage_path="snapshots/w/curr.html")
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, prev, curr])
        storage = _FakeStorage(
            files={
                "snapshots/w/prev.html": b"<html><body>x</body></html>",
                "snapshots/w/curr.html": b"<html><body>y</body></html>",
            }
        )
        result = await _load_event_unified_diff(
            session, event, storage=storage, content_type=ContentType.HTML
        )
        assert result.startswith("--- content")
        assert session.get.call_count == 3  # no Watch fetch

    @pytest.mark.asyncio
    async def test_missing_watch_falls_back_to_text_path(self):
        """If the Watch row vanished (deleted between event and dispatch),
        skip the HTML branch and use the safe text_path path."""
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock(text_path="snapshots/w/prev.txt", storage_path="snapshots/w/prev.html")
        curr = MagicMock(text_path="snapshots/w/curr.txt", storage_path="snapshots/w/curr.html")
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, prev, curr, None])
        storage = _FakeStorage(
            files={
                "snapshots/w/prev.txt": b"alpha\nbeta\n",
                "snapshots/w/curr.txt": b"alpha\nbeta-changed\n",
            }
        )
        result = await _load_event_unified_diff(session, event, storage=storage)
        assert "+beta-changed" in result
        assert storage.load_calls == ["snapshots/w/prev.txt", "snapshots/w/curr.txt"]


class TestUnifiedDiffLazyLoad:
    """The dispatcher loads prev/curr extracted text lazily — only when at
    least one candidate's body actually consumes the unified diff. The result
    is memoized across all candidates for the same event (issue #116)."""

    @pytest.mark.asyncio
    async def test_no_diff_load_when_no_candidate_needs_it(self):
        """Default ContentOptions (toggles off, no body_template) → no
        snapshot text load."""
        event = make_event(WatchEventType.CHANGE_DETECTED)
        event.metadata["change_id"] = str(ULID())

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
        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=client),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_diff_load_once_when_toggle_on(self):
        """include_diff_snippet=True → diff is loaded exactly once and shared
        across N candidates."""
        event = make_event(WatchEventType.CHANGE_DETECTED)
        event.metadata["change_id"] = str(ULID())

        cfg = ContentConfig(default=ContentOptions(include_diff_snippet=True)).model_dump()
        configs = []
        for _ in range(3):
            c = MagicMock()
            c.content_config = cfg
            c.remote_channel_id = str(ULID())
            configs.append(c)

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(*configs),
            ]
        )

        client = _mock_notifier_client()
        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=client),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
                return_value="--- content\n+++ content\n@@ -1,1 +1,1 @@\n-old\n+new\n",
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_called_once()
        assert client.dispatch.call_count == 3
        for call in client.dispatch.call_args_list:
            body = call.kwargs["body_template"]
            assert "```diff" in body
            assert "+new" in body

    @pytest.mark.asyncio
    async def test_dispatcher_threads_content_type_to_load(self):
        """The dispatcher must thread `content_type` from the watch_meta row
        through to `_load_event_unified_diff` for the perf shortcut."""
        event = make_event(WatchEventType.CHANGE_DETECTED)

        cfg = ContentConfig(default=ContentOptions(include_diff_snippet=True)).model_dump()
        c = MagicMock()
        c.content_config = cfg
        c.remote_channel_id = str(ULID())

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result("example.com", content_type=ContentType.HTML),
                _empty_result(),
                _empty_result(),
                _empty_result(),
                _result_with(c),
            ]
        )

        client = _mock_notifier_client()
        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=client),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
                return_value="--- content\n+++ content\n@@ -1,1 +1,1 @@\n-old\n+new\n",
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_called_once()
        assert load_mock.call_args.kwargs.get("content_type") == ContentType.HTML

    @pytest.mark.asyncio
    async def test_diff_load_when_body_template_references_diff_snippet(self):
        """body_template referencing {{ diff_snippet }} also triggers the load."""
        event = make_event(WatchEventType.CHANGE_DETECTED)
        event.metadata["change_id"] = str(ULID())

        cfg = ContentConfig(
            default=ContentOptions(body_template="diff: {{ diff_snippet }}")
        ).model_dump()
        c = MagicMock()
        c.content_config = cfg
        c.remote_channel_id = str(ULID())

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(c),
            ]
        )

        client = _mock_notifier_client()
        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=client),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
                return_value="--- content\n+++ content\n@@ -1,1 +1,1 @@\n-x\n+y\n",
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_diff_load_when_template_mentions_var_outside_jinja_delimiters(self):
        """A body_template that mentions "diff_snippet" only as literal text
        (not inside {{ ... }} or {% ... %}) must not trigger the lazy-load."""
        event = make_event(WatchEventType.CHANGE_DETECTED)
        event.metadata["change_id"] = str(ULID())

        cfg = ContentConfig(
            default=ContentOptions(body_template="See diff_snippet docs at example.com")
        ).model_dump()
        c = MagicMock()
        c.content_config = cfg
        c.remote_channel_id = str(ULID())

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(c),
            ]
        )

        client = _mock_notifier_client()
        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=client),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_diff_load_for_non_change_event(self):
        """watch_error and other non-change events never trigger the diff load."""
        event = make_event(WatchEventType.WATCH_ERROR)

        cfg = ContentConfig(default=ContentOptions(include_diff_snippet=True)).model_dump()
        c = MagicMock()
        c.content_config = cfg
        c.remote_channel_id = str(ULID())
        c.events = ["watch_error"]

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(c),
            ]
        )

        client = _mock_notifier_client()
        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=client),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_not_called()
