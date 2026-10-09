"""Tests for the notification preview mock-event fixtures."""

import hashlib
import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from src.api.schemas.content_config import ContentConfig, ContentOptions
from src.core.notifications import diff_loader as loader_mod
from src.core.notifications.content import build_body, resolve_options
from src.core.notifications.diff import compute_change_diff
from src.core.notifications.diff_loader import StoredText
from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifications.notify import dispatch_event_notifications
from src.core.notifications.preview_fixtures import (
    MOCK_EVENT_FIXTURES,
    PREVIEW_CURRENT_TEXT,
    PREVIEW_PREVIOUS_TEXT,
    build_preview_event,
    preview_diff,
)
from src.core.notifications.renotify import error_renotify_metadata
from src.core.utils import format_utc_iso, watched_item_event_base_metadata


class _FakeWatchedItem:
    """Minimal stand-in matching the attributes watched_item_event_base_metadata reads."""

    def __init__(self):
        self.domain_name = "example.com"
        self.default_schedule_config = {"interval": "1h"}
        self.last_changed_at = datetime(2026, 4, 15, 3, 22, 0, tzinfo=UTC)
        self.default_tags = ["regulatory"]
        self.description = "desc"


def _real_change_detected_keys() -> set[str]:
    """The metadata keys a real change_detected event actually carries.

    Base (watched_item_event_base_metadata) + the keys pipeline.py layers on
    for a detected change.

    The base half is derived from live code, but the per-event additions below
    are hard-coded — keep them synced with the ``change_meta`` dict built in
    ``src/workers/pipeline.py`` (``process_watched_item``). Because the parity
    assertion is a subset check (permissive), a new key added to ``change_meta``
    but omitted here would NOT fail this test; it would only weaken the guard.
    """
    base = set(watched_item_event_base_metadata(_FakeWatchedItem()).keys())
    return base | {
        "change_revision_id",
        "previous_fingerprint",
        "current_fingerprint",
        "extraction_changed",
        "previous_changed_at",
    }


def _real_watch_error_keys() -> set[str]:
    """The metadata keys a real watch_error can carry: the base, the failure
    paths' ``error_metadata`` (``reason`` / ``status_code`` / ``error``), and
    the #71 repeat keys — derived from the emitter's own helper."""
    base = set(watched_item_event_base_metadata(_FakeWatchedItem()).keys())
    repeat = error_renotify_metadata(repeat=True, previously_notified_at=datetime.now(UTC))
    return base | {"reason", "status_code", "error"} | set(repeat)


_REPEAT_ERROR_METADATA = error_renotify_metadata(
    repeat=True, previously_notified_at=datetime(2026, 4, 14, 12, 0, 0, tzinfo=UTC)
)


class TestMockEventFixtures:
    def test_entry_for_every_event_type(self):
        for et in WatchEventType:
            assert et.value in MOCK_EVENT_FIXTURES, f"MOCK_EVENT_FIXTURES missing {et.value}"

    def test_no_fixture_for_a_non_event(self):
        """#166: a fixture for a value that cannot fire lets the preview vouch for it."""
        assert set(MOCK_EVENT_FIXTURES) == {et.value for et in WatchEventType}

    def test_change_detected_matches_pipeline_metadata(self):
        """#221 fidelity invariant: the fixture must not advertise keys a real
        change email never carries. Its keys must be a subset of what
        pipeline.py actually emits."""
        fx_keys = set(MOCK_EVENT_FIXTURES["change_detected"].keys())
        real_keys = _real_change_detected_keys()
        assert fx_keys <= real_keys, f"fixture has phantom keys: {fx_keys - real_keys}"
        # And the change-identity key is present so change_url renders.
        assert "change_revision_id" in fx_keys

    def test_change_detected_has_no_phantom_keys(self):
        """The retired chunk/significance keys stay retired (#222 D8), and the
        diff itself is computed at dispatch, never carried as metadata."""
        fx = MOCK_EVENT_FIXTURES["change_detected"]
        for phantom in (
            "added",
            "modified",
            "removed",
            "significance",
            "change_id",
            "unified_diff",
        ):
            assert phantom not in fx

    def test_fixture_fingerprints_address_the_canned_texts(self):
        fx = MOCK_EVENT_FIXTURES["change_detected"]
        prev = hashlib.sha256(PREVIEW_PREVIOUS_TEXT).hexdigest()
        curr = hashlib.sha256(PREVIEW_CURRENT_TEXT).hexdigest()
        assert fx["previous_fingerprint"] == f"sha256:{prev}"
        assert fx["current_fingerprint"] == f"sha256:{curr}"

    def test_change_detected_dates_are_a_real_changes_dates(self):
        """CR 4: on a real change ``last_changed_at`` *is* this change (the
        pipeline sets it before building the event), and
        ``previous_changed_at`` is earlier."""
        event = build_preview_event("change_detected")
        assert event.metadata["last_changed_at"] == format_utc_iso(event.occurred_at)
        assert event.metadata["previous_changed_at"] < event.metadata["last_changed_at"]

    def test_watch_error_has_status_code(self):
        fx = MOCK_EVENT_FIXTURES["watch_error"]
        assert "status_code" in fx

    def test_watch_error_matches_emitted_metadata(self):
        """#221's fidelity invariant, for the event #71 added keys to."""
        fx_keys = set(MOCK_EVENT_FIXTURES["watch_error"])
        real_keys = _real_watch_error_keys()
        assert fx_keys <= real_keys, f"fixture has phantom keys: {fx_keys - real_keys}"

    def test_watch_error_previews_the_first_notification(self):
        """Every emitted watch_error carries both repeat keys; the preview shows
        the common case, so a template using them previews the first form."""
        fx = MOCK_EVENT_FIXTURES["watch_error"]
        assert fx["renotify"] is False
        assert fx["previously_notified_at"] == ""

    def test_an_unguarded_repeat_variable_previews_as_it_dispatches(self):
        """CR 8: the strict preview must not reject a template that dispatches
        fine — the variable is documented as empty on a first alert."""
        event = build_preview_event("watch_error")
        options = ContentOptions(body_template="last told: {{ previously_notified_at }}")
        assert build_body(event, options, strict=True) == "last told: "


class TestBuildPreviewEvent:
    def test_returns_watchevent_for_every_event_type(self):
        for et in WatchEventType:
            ev = build_preview_event(et.value)
            assert isinstance(ev, WatchEvent)
            assert ev.event_type == et

    def test_event_has_item_name_and_url(self):
        ev = build_preview_event("change_detected")
        assert ev.item_name
        assert ev.item_url.startswith("http")

    def test_event_has_occurred_at(self):
        ev = build_preview_event("change_detected")
        assert isinstance(ev.occurred_at, datetime)

    def test_metadata_includes_fixture_fields(self):
        ev = build_preview_event("change_detected")
        assert "domain_name" in ev.metadata
        assert "change_revision_id" in ev.metadata

    def test_unknown_event_type_raises(self):
        with pytest.raises(KeyError):
            build_preview_event("not_a_real_event_type")


class TestPreviewDiff:
    def test_change_detected_diffs_the_canned_texts(self):
        assert preview_diff("change_detected") == compute_change_diff(
            PREVIEW_PREVIOUS_TEXT, PREVIEW_CURRENT_TEXT
        )

    def test_the_canned_texts_hold_an_unpunctuated_list_run(self):
        """#349: the common live shape — a schedule or list page with no
        sentence end — is the one #222's segmentation realigned on. The preview
        carries one, so a regression shows in the preview too."""
        run = max(PREVIEW_PREVIOUS_TEXT.split(b"\n"), key=len)
        assert len(run) > 800
        assert not re.search(rb"[.!?]\s", run)

    def test_an_early_insertion_in_the_run_marks_none_of_its_tail(self):
        """Everything after April is the run's tail: none of it is a change."""
        changed = [
            line
            for hunk in preview_diff("change_detected").hunks
            for line in hunk
            if line[:1] in "+-"
        ]
        assert "+ Recording: April 6 hearing video" in changed
        assert not any("May" in line for line in changed)

    def test_other_events_have_none(self):
        assert preview_diff("watch_error") is None


class TestPreviewDispatchParity:
    """#222 acceptance: the preview renders the diff the dispatcher would send.

    The dispatcher is driven end to end over the preview event — template
    query, ``load_change_diff`` (locate, read, hash check, ``difflib``) and
    render — with a store serving the canned texts. Its body must equal the
    preview's, byte for byte, for every diff toggle shape.
    """

    @pytest.mark.parametrize(
        "content_config",
        [
            None,
            {"default": {"include_diff_full": True}},
            {"default": {"diff_snippet_lines": 6}},
            {"default": {"include_diff_snippet": False, "body_template": "{{ diff_snippet }}"}},
        ],
    )
    async def test_dispatched_body_equals_the_preview(self, monkeypatch, content_config):
        monkeypatch.setenv("WATCHER_NOTIFIER_BASE_URL", "http://notifier.invalid:9000")
        monkeypatch.setenv("WATCHER_NOTIFIER_API_KEY", "nk_test")
        event = build_preview_event("change_detected")
        by_fp = {
            event.metadata["previous_fingerprint"]: PREVIEW_PREVIOUS_TEXT,
            event.metadata["current_fingerprint"]: PREVIEW_CURRENT_TEXT,
        }

        async def locate(_session, fp):
            return StoredText(uri=fp, size_bytes=len(by_fp[fp])) if fp in by_fp else None

        template = MagicMock()
        template.id = ULID()
        template.visibility = "global"
        template.content_config = content_config
        template.remote_channel_id = str(ULID())
        result = MagicMock()
        result.scalars.return_value.all.return_value = [template]
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(return_value=MagicMock(domain_name=None))
        session.execute = AsyncMock(return_value=result)
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(loader_mod, "stored_text_location", side_effect=locate),
            patch.object(loader_mod, "aread_blob", side_effect=lambda uri: by_fp[uri]),
            patch("src.core.notifications.notify.get_notifier_client", return_value=client),
            patch("src.core.notifications.notify.audit"),
        ):
            await dispatch_event_notifications(session=session, event=event)

        cfg = ContentConfig.model_validate(content_config) if content_config else None
        options = resolve_options(cfg, "change_detected")
        preview = build_body(event, options, strict=True, diff=preview_diff("change_detected"))
        dispatched = client.dispatch.call_args.kwargs["body_template"]
        assert dispatched == preview
        assert "```diff" in dispatched


class TestWatchErrorPreviewDispatchParity:
    """#71: the watch_error preview renders what the dispatcher sends, for the
    first notification and for a repeat, under the default body and a user
    template that branches on the repeat variables."""

    @pytest.mark.parametrize("extra", [{}, _REPEAT_ERROR_METADATA], ids=["first", "repeat"])
    @pytest.mark.parametrize(
        "content_config",
        [
            None,
            {
                "default": {
                    "body_template": (
                        "{{ item_name }}{% if renotify %} (still failing){% endif %}"
                        " last told: {{ previously_notified_at }}"
                    )
                }
            },
        ],
        ids=["default", "user-template"],
    )
    async def test_dispatched_body_equals_the_preview(self, monkeypatch, extra, content_config):
        monkeypatch.setenv("WATCHER_NOTIFIER_BASE_URL", "http://notifier.invalid:9000")
        monkeypatch.setenv("WATCHER_NOTIFIER_API_KEY", "nk_test")
        preview_event = build_preview_event("watch_error")
        event = WatchEvent(
            event_type=preview_event.event_type,
            watched_item_id=preview_event.watched_item_id,
            item_name=preview_event.item_name,
            item_url=preview_event.item_url,
            occurred_at=preview_event.occurred_at,
            metadata={**preview_event.metadata, **extra},
        )

        template = MagicMock()
        template.id = ULID()
        template.visibility = "global"
        template.content_config = content_config
        template.remote_channel_id = str(ULID())
        result = MagicMock()
        result.scalars.return_value.all.return_value = [template]
        session = AsyncMock(spec=AsyncSession)
        session.get = AsyncMock(return_value=MagicMock(domain_name=None))
        session.execute = AsyncMock(return_value=result)
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.core.notifications.notify.get_notifier_client", return_value=client),
            patch("src.core.notifications.notify.audit"),
        ):
            await dispatch_event_notifications(session=session, event=event)

        cfg = ContentConfig.model_validate(content_config) if content_config else None
        preview = build_body(event, resolve_options(cfg, "watch_error"), strict=True)
        dispatched = client.dispatch.call_args.kwargs["body_template"]
        assert dispatched == preview
        repeat_said = "previously notified" in dispatched or "(still failing)" in dispatched
        assert repeat_said is bool(extra)
