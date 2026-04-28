"""Tests for dispatch_event_notifications — 4-source live dispatch."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import (
    _load_event_unified_diff,
    dispatch_event_notifications,
)


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
    t.content_config = None
    return t


def _fake_local(cid=None):
    from src.core.crypto import encrypt_apprise_url
    from src.core.models.notification_config import WatchNotificationConfig

    c = MagicMock(spec=WatchNotificationConfig)
    c.id = cid or ULID()
    c.apprise_url = encrypt_apprise_url("json://local.example.com/notify")
    c.events = ["change_detected"]
    c.content_config = None
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


class TestContentConfig:
    @pytest.mark.asyncio
    async def test_content_config_body_used_in_dispatch(self, set_test_key):
        """When a config has content_config, build_body is called and the result forwarded."""
        from src.core.notifications.notify import dispatch_event_notifications

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
                _scalar_result(None),  # domain lookup → None (no domain query)
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
                _scalar_result(None),  # domain lookup → None (no domain query)
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
                _scalar_result(None),  # domain lookup
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
                _scalar_result(None),
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
                _scalar_result(None),
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
                _scalar_result(None),
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
                _scalar_result(None),
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
    async def test_missing_text_path_returns_empty(self):
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock()
        prev.text_path = ""
        curr = MagicMock()
        curr.text_path = "some/path"
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, prev, curr])
        result = await _load_event_unified_diff(session, event)
        assert result == ""

    @pytest.mark.asyncio
    async def test_storage_error_returns_empty_does_not_raise(self):
        """RuntimeError (a non-OSError) must be caught — verifies CR #2 broadened catch."""
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock(text_path="snapshots/w/prev.txt")
        curr = MagicMock(text_path="snapshots/w/curr.txt")
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, prev, curr])
        storage = _FakeStorage(raise_on_load=RuntimeError("backend exploded"))
        result = await _load_event_unified_diff(session, event, storage=storage)
        assert result == ""

    @pytest.mark.asyncio
    async def test_happy_path_returns_unified_diff(self):
        event = _change_event_with_id(ULID())
        change = MagicMock()
        change.previous_snapshot_id = ULID()
        change.current_snapshot_id = ULID()
        prev = MagicMock(text_path="snapshots/w/prev.txt")
        curr = MagicMock(text_path="snapshots/w/curr.txt")
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(side_effect=[change, prev, curr])
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
