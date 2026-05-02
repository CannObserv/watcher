"""Tests for dispatch_event_notifications — 4-source live dispatch."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
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
def set_test_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APPRISE_SECRET_KEY", key)


@pytest.fixture(autouse=True)
def no_remote_notify(monkeypatch):
    monkeypatch.delenv("USE_REMOTE_NOTIFY", raising=False)


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
    `select(Watch.effective_domain, Watch.content_type)` lookup.

    `value` is the effective_domain (str or None). `content_type` is the
    Watch.content_type (defaults to None → non-HTML branch). Returns a
    MagicMock whose `.one_or_none()` yields either `None` (no row) or
    the 2-tuple `(domain, content_type)`.
    """
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


def _fake_template(tid=None):
    t = MagicMock()
    t.id = tid or str(ULID())
    t.apprise_url = "json://hooks.example.com/notify"
    t.content_config = None
    t.remote_channel_id = None
    return t


def _fake_local(cid=None):
    from src.core.crypto import encrypt_apprise_url
    from src.core.models.notification_config import WatchNotificationConfig

    c = MagicMock(spec=WatchNotificationConfig)
    c.id = cid or ULID()
    c.apprise_url = encrypt_apprise_url("json://local.example.com/notify")
    c.events = ["change_detected"]
    c.content_config = None
    c.remote_channel_id = None
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
                _watch_meta_result(None),  # domain lookup → None (no domain query)
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
                _watch_meta_result(None),  # no domain
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
                _watch_meta_result(None),
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
                _watch_meta_result("example.com"),  # domain lookup
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
                _watch_meta_result(None),  # no domain → skip domain query
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
                _watch_meta_result("example.com"),
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
                _watch_meta_result(None),
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
                _watch_meta_result(None),
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
                _watch_meta_result(None),
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
                _watch_meta_result(None),
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
                _watch_meta_result(None),
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
                _watch_meta_result("example.com"),
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
                _watch_meta_result("example.com"),
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


class TestContentConfig:
    @pytest.mark.asyncio
    async def test_content_config_body_used_in_dispatch(self, set_test_key):
        """When a config has content_config, build_body is called and the result forwarded."""
        event = make_event(WatchEventType.CHANGE_DETECTED)

        content_cfg = ContentConfig(default=ContentOptions(include_domain=True))
        content_cfg_dict = content_cfg.model_dump()

        mock_config = MagicMock()
        mock_config.apprise_url = "encrypted_url"
        mock_config.content_config = content_cfg_dict

        dispatched_bodies = []

        async def fake_dispatch(ev, url, *, body, title):
            dispatched_bodies.append(body)
            return MagicMock(success=True, reason="ok")

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),  # domain lookup → None (no domain query)
                _empty_result(),  # global templates
                _empty_result(),  # watch templates
                _result_with(mock_config),  # local configs
            ]
        )

        with patch("src.core.notifications.notify.dispatch_event", fake_dispatch):
            await dispatch_event_notifications(session, event)

        assert len(dispatched_bodies) == 1
        # include_domain=True but no effective_domain in metadata → no domain section added
        # body should match the default change_detected body (just url + summary)
        assert "example.com" in dispatched_bodies[0]
        assert "Domain:" not in dispatched_bodies[0]

    @pytest.mark.asyncio
    async def test_null_content_config_renders_default_body(self, set_test_key):
        """content_config=None — dispatch_event called with the default-template body."""
        event = make_event(WatchEventType.CHANGE_DETECTED)

        mock_config = MagicMock()
        mock_config.apprise_url = "encrypted_url"
        mock_config.content_config = None

        dispatched_bodies = []

        async def fake_dispatch(ev, url, *, body, title):
            dispatched_bodies.append(body)
            return MagicMock(success=True, reason="ok")

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),  # domain lookup → None (no domain query)
                _empty_result(),  # global templates
                _empty_result(),  # watch templates
                _result_with(mock_config),  # local configs
            ]
        )

        with patch("src.core.notifications.notify.dispatch_event", fake_dispatch):
            await dispatch_event_notifications(session, event)

        assert len(dispatched_bodies) == 1
        # Default change_detected body: "{{ watch_url }} — {{ change_summary }}"
        assert dispatched_bodies[0] is not None
        assert "example.com" in dispatched_bodies[0]


class TestErrorHandling:
    async def test_dispatch_failure_does_not_raise(self):
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

        with patch(
            "src.core.notifications.notify.dispatch_event",
            new_callable=AsyncMock,
            side_effect=Exception("boom"),
        ):
            await dispatch_event_notifications(session, make_event())
        # Did not raise


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
        # gets: Change, prev Snapshot (None), curr Snapshot — Watch fetch never reached
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
        # Loaded text_path, not storage_path
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
        # Single-line HTML → must be pretty-printed across multiple lines
        # before diffing. We verify both the content of the changed line
        # and the multi-line shape of the diff so a regression that drops
        # prettification (e.g. switching to raw HTML) would fail this test.
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
        # Prettified diff has the changed paragraph as a discrete `-`/`+`
        # line, not embedded in a one-line raw-HTML dump.
        diff_lines = result.splitlines()
        assert any(line.startswith("-") and "<p>beta</p>" in line for line in diff_lines)
        assert any(line.startswith("+") and "<p>beta-changed</p>" in line for line in diff_lines)
        # Lower bound on line count proves prettification expanded the input
        # (raw single-line HTML diff would be ~4 lines: 2 headers + @@ + content).
        assert len(diff_lines) >= 8
        # Loaded storage_path (raw HTML), not text_path
        assert storage.load_calls == ["snapshots/w/prev.html", "snapshots/w/curr.html"]

    @pytest.mark.asyncio
    async def test_html_watch_normalize_html_failure_falls_back_to_raw(self, monkeypatch):
        """When `normalize_html` raises, the function falls back to diffing
        the un-prettified raw HTML rather than returning empty (matches the
        dashboard's `_maybe_prettify_html` graceful-degrade)."""
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
        # Fallback path: raw-HTML diff is non-empty and contains the change.
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
        """HTML watch missing either side's storage_path → no fallback
        (text_path is the chunk-joined extracted text, which is exactly
        what we're trying to avoid)."""
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
        Watch row — `dispatch_event_notifications` already loaded it
        alongside `effective_domain`."""
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock(text_path="snapshots/w/prev.txt", storage_path="snapshots/w/prev.html")
        curr = MagicMock(text_path="snapshots/w/curr.txt", storage_path="snapshots/w/curr.html")
        session = AsyncMock(spec=AsyncSession)
        # Only 3 gets — Change, prev Snapshot, curr Snapshot. No Watch fetch.
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
        session.get = AsyncMock(side_effect=[change, prev, curr, None])  # watch missing
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
    async def test_no_diff_load_when_no_candidate_needs_it(self, set_test_key):
        """Default ContentOptions (toggles off, no body_template) → no
        snapshot text load. The dispatcher must not call _load_event_unified_diff."""
        event = make_event(WatchEventType.CHANGE_DETECTED)
        event.metadata["change_id"] = str(ULID())

        mock_config = MagicMock()
        mock_config.apprise_url = "encrypted_url"
        mock_config.content_config = None  # all defaults — no diff needed

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),  # domain lookup
                _empty_result(),  # global templates
                _empty_result(),  # watch templates (domain templates query skipped: no domain)
                _result_with(mock_config),  # local configs
            ]
        )

        async def fake_dispatch(ev, url, *, body, title):
            return MagicMock(success=True, reason="ok")

        with (
            patch("src.core.notifications.notify.dispatch_event", fake_dispatch),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_diff_load_once_when_toggle_on(self, set_test_key):
        """include_diff_snippet=True → diff is loaded exactly once and shared
        across N candidates. Memoization is essential — N configs must not
        cause N storage round-trips."""
        event = make_event(WatchEventType.CHANGE_DETECTED)
        event.metadata["change_id"] = str(ULID())

        cfg = ContentConfig(default=ContentOptions(include_diff_snippet=True)).model_dump()
        configs = []
        for _ in range(3):
            c = MagicMock()
            c.apprise_url = "encrypted_url"
            c.content_config = cfg
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

        captured_bodies = []

        async def fake_dispatch(ev, url, *, body, title):
            captured_bodies.append(body)
            return MagicMock(success=True, reason="ok")

        with (
            patch("src.core.notifications.notify.dispatch_event", fake_dispatch),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
                return_value="--- content\n+++ content\n@@ -1,1 +1,1 @@\n-old\n+new\n",
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_called_once()
        assert len(captured_bodies) == 3
        # All three bodies share the same fenced diff block.
        for body in captured_bodies:
            assert "```diff" in body
            assert "+new" in body

    @pytest.mark.asyncio
    async def test_dispatcher_threads_content_type_to_load(self, set_test_key):
        """The dispatcher must unpack content_type from the watch_meta row
        and pass it through to `_load_event_unified_diff` so the inner
        function takes the perf shortcut and skips a redundant Watch fetch."""
        event = make_event(WatchEventType.CHANGE_DETECTED)

        cfg = ContentConfig(default=ContentOptions(include_diff_snippet=True)).model_dump()
        c = MagicMock()
        c.apprise_url = "encrypted_url"
        c.content_config = cfg

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

        async def fake_dispatch(ev, url, *, body, title):
            return MagicMock(success=True, reason="ok")

        with (
            patch("src.core.notifications.notify.dispatch_event", fake_dispatch),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
                return_value="--- content\n+++ content\n@@ -1,1 +1,1 @@\n-old\n+new\n",
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_called_once()
        # The kwarg must carry the dispatcher's row-unpacked content_type so
        # the inner function takes the perf shortcut. A regression that drops
        # `content_type=` would leave it absent or default-None here.
        assert load_mock.call_args.kwargs.get("content_type") == ContentType.HTML

    @pytest.mark.asyncio
    async def test_diff_load_when_body_template_references_diff_snippet(self, set_test_key):
        """body_template referencing {{ diff_snippet }} also triggers the load
        even when the include_diff_* toggles are off, since toggles are
        bypassed on the body_template code path."""
        event = make_event(WatchEventType.CHANGE_DETECTED)
        event.metadata["change_id"] = str(ULID())

        cfg = ContentConfig(
            default=ContentOptions(body_template="diff: {{ diff_snippet }}")
        ).model_dump()
        c = MagicMock()
        c.apprise_url = "encrypted_url"
        c.content_config = cfg

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(c),
            ]
        )

        async def fake_dispatch(ev, url, *, body, title):
            return MagicMock(success=True, reason="ok")

        with (
            patch("src.core.notifications.notify.dispatch_event", fake_dispatch),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
                return_value="--- content\n+++ content\n@@ -1,1 +1,1 @@\n-x\n+y\n",
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_diff_load_when_template_mentions_var_outside_jinja_delimiters(
        self, set_test_key
    ):
        """A body_template that mentions "diff_snippet" only as literal text
        (not inside {{ ... }} or {% ... %}) must not trigger the lazy-load.
        Verifies the Jinja-aware regex in _DIFF_VAR_RE (CR #4)."""
        event = make_event(WatchEventType.CHANGE_DETECTED)
        event.metadata["change_id"] = str(ULID())

        cfg = ContentConfig(
            default=ContentOptions(body_template="See diff_snippet docs at example.com")
        ).model_dump()
        c = MagicMock()
        c.apprise_url = "encrypted_url"
        c.content_config = cfg

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            side_effect=[
                _watch_meta_result(None),
                _empty_result(),
                _empty_result(),
                _result_with(c),
            ]
        )

        async def fake_dispatch(ev, url, *, body, title):
            return MagicMock(success=True, reason="ok")

        with (
            patch("src.core.notifications.notify.dispatch_event", fake_dispatch),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_diff_load_for_non_change_event(self, set_test_key):
        """watch_error and other non-change events never trigger the diff load
        even if the resolved options have diff toggles on."""
        event = make_event(WatchEventType.WATCH_ERROR)

        cfg = ContentConfig(default=ContentOptions(include_diff_snippet=True)).model_dump()
        c = MagicMock()
        c.apprise_url = "encrypted_url"
        c.content_config = cfg
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

        async def fake_dispatch(ev, url, *, body, title):
            return MagicMock(success=True, reason="ok")

        with (
            patch("src.core.notifications.notify.dispatch_event", fake_dispatch),
            patch(
                "src.core.notifications.notify._load_event_unified_diff",
                new_callable=AsyncMock,
            ) as load_mock,
        ):
            await dispatch_event_notifications(session, event)

        load_mock.assert_not_called()
