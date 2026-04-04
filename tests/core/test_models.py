"""Tests for SQLAlchemy base and ULID column type."""

from datetime import date
from unittest.mock import MagicMock

import pytest
from ulid import ULID

from src.core.database import get_database_url, get_engine, reset_engine
from src.core.models.audit_log import AuditLog, EventType, audit
from src.core.models.base import ULIDType
from src.core.models.change import Change
from src.core.models.domain import Domain
from src.core.models.notification_config import NotificationConfig
from src.core.models.snapshot import Snapshot, SnapshotChunk
from src.core.models.temporal_profile import PostAction, ProfileType, TemporalProfile
from src.core.models.watch import ContentType, Watch


class TestULIDType:
    def test_process_bind_param_converts_ulid_to_string(self):
        ulid_type = ULIDType()
        value = ULID()
        result = ulid_type.process_bind_param(value, dialect=None)
        assert isinstance(result, str)
        assert result == str(value)

    def test_process_bind_param_passes_none(self):
        ulid_type = ULIDType()
        result = ulid_type.process_bind_param(None, dialect=None)
        assert result is None

    def test_process_result_value_converts_string_to_ulid(self):
        ulid_type = ULIDType()
        original = ULID()
        result = ulid_type.process_result_value(str(original), dialect=None)
        assert isinstance(result, ULID)
        assert result == original

    def test_process_result_value_passes_none(self):
        ulid_type = ULIDType()
        result = ulid_type.process_result_value(None, dialect=None)
        assert result is None


class TestWatchModel:
    def test_create_watch_with_defaults(self):
        watch = Watch(
            name="Test Watch",
            url="https://example.com/agenda",
            content_type=ContentType.HTML,
        )
        assert watch.name == "Test Watch"
        assert watch.url == "https://example.com/agenda"
        assert watch.content_type == ContentType.HTML
        assert watch.is_active is True
        assert watch.fetch_config == {}
        assert watch.schedule_config == {}
        assert watch.last_checked_at is None

    def test_create_watch_with_all_fields(self):
        watch = Watch(
            name="PDF Watch",
            url="https://example.com/report.pdf",
            content_type=ContentType.PDF,
            fetch_config={"selectors": ["#content"]},
            schedule_config={"interval": "6h"},
            is_active=False,
        )
        assert watch.content_type == ContentType.PDF
        assert watch.fetch_config == {"selectors": ["#content"]}
        assert watch.schedule_config == {"interval": "6h"}
        assert watch.is_active is False

    def test_content_type_enum_values(self):
        assert ContentType.HTML.value == "html"
        assert ContentType.PDF.value == "pdf"
        assert ContentType.FILE.value == "file"

    def test_content_type_coerces_string(self):
        watch = Watch(
            name="Coerce Test",
            url="https://example.com",
            content_type="pdf",
        )
        assert watch.content_type is ContentType.PDF

    def test_content_type_rejects_invalid(self):
        with pytest.raises(ValueError, match="Invalid content_type"):
            Watch(
                name="Bad Type",
                url="https://example.com",
                content_type="invalid",
            )

    def test_watch_effective_fields_default_none(self):
        watch = Watch(
            name="Test",
            url="https://example.com",
            content_type=ContentType.HTML,
        )
        assert watch.effective_url is None
        assert watch.effective_domain is None

    def test_watch_is_archived_defaults_false(self):
        watch = Watch(
            name="Test",
            url="https://example.com",
            content_type=ContentType.HTML,
        )
        assert watch.is_archived is False

    def test_watch_can_set_is_archived_true(self):
        watch = Watch(
            name="Archived",
            url="https://example.com",
            content_type=ContentType.HTML,
            is_archived=True,
        )
        assert watch.is_archived is True


class TestAuditHelper:
    """Tests for the audit() helper function."""

    def test_audit_creates_entry_with_correct_fields(self):
        mock_session = MagicMock()
        watch_id = ULID()

        entry = audit(
            mock_session,
            EventType.WATCH_CREATED,
            watch_id=watch_id,
            name="Test Watch",
        )

        assert isinstance(entry, AuditLog)
        assert entry.event_type == EventType.WATCH_CREATED
        assert entry.watch_id == watch_id
        assert entry.payload == {"name": "Test Watch"}
        mock_session.add.assert_called_once_with(entry)

    def test_audit_without_watch_id(self):
        mock_session = MagicMock()
        entry = audit(mock_session, EventType.CHECK_FETCH_FAILED, status_code=500)

        assert entry.watch_id is None
        assert entry.payload == {"status_code": 500}
        mock_session.add.assert_called_once_with(entry)

    def test_audit_adds_to_session(self):
        mock_session = MagicMock()
        entry = audit(mock_session, EventType.WATCH_DELETED)
        mock_session.add.assert_called_once_with(entry)


class TestEventType:
    """Tests for EventType string constants."""

    def test_constants_have_expected_values(self):
        assert EventType.WATCH_CREATED == "watch.created"
        assert EventType.WATCH_UPDATED == "watch.updated"
        assert EventType.WATCH_DEACTIVATED == "watch.deactivated"
        assert EventType.WATCH_DELETED == "watch.deleted"
        assert EventType.CHECK_SNAPSHOT_CREATED == "check.snapshot_created"
        assert EventType.CHECK_NO_CHANGE == "check.no_change"
        assert EventType.CHECK_FETCH_FAILED == "check.fetch_failed"
        assert EventType.NOTIFICATION_DISPATCHED == "notification.dispatched"
        assert EventType.NOTIFICATION_CONFIG_CREATED == "notification_config.created"
        assert EventType.NOTIFICATION_CONFIG_DELETED == "notification_config.deleted"
        assert EventType.PROFILE_CREATED == "profile.created"
        assert EventType.PROFILE_UPDATED == "profile.updated"
        assert EventType.PROFILE_DELETED == "profile.deleted"

    def test_all_constants_are_unique(self):
        values = [v for k, v in vars(EventType).items() if not k.startswith("_")]
        assert len(values) == len(set(values)), "EventType constants must be unique"


class TestAuditLogModel:
    def test_create_audit_log_entry(self):
        entry = AuditLog(
            event_type=EventType.WATCH_CREATED,
            payload={"watch_name": "Test Watch"},
        )
        assert entry.event_type == EventType.WATCH_CREATED
        assert entry.payload == {"watch_name": "Test Watch"}
        assert entry.watch_id is None

    def test_create_audit_log_with_watch_id(self):
        watch_id = ULID()
        entry = AuditLog(
            event_type="check.started",
            watch_id=watch_id,
            payload={"url": "https://example.com"},
        )
        assert entry.watch_id == watch_id


class TestDatabase:
    def test_get_database_url_raises_without_env(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="DATABASE_URL environment variable is not set"):
            get_database_url()

    def test_get_database_url_from_env(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://custom:pass@db:5432/mydb")
        url = get_database_url()
        assert url == "postgresql+asyncpg://custom:pass@db:5432/mydb"

    def test_reset_engine_clears_singleton(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/test")
        reset_engine()
        engine = get_engine()
        assert engine is not None
        reset_engine()
        # After reset, a new engine should be created on next call
        engine2 = get_engine()
        assert engine2 is not engine


class TestSnapshotModel:
    def test_create_snapshot(self):
        snap = Snapshot(
            watch_id=ULID(),
            content_hash="abc123",
            simhash=42,
            storage_path="snapshots/w1/s1.html",
            text_path="snapshots/w1/s1.txt",
            storage_backend="local",
            chunk_count=3,
            text_bytes=1024,
            fetch_duration_ms=200,
            fetcher_used="http",
        )
        assert snap.content_hash == "abc123"
        assert snap.chunk_count == 3
        assert snap.storage_backend == "local"


class TestSnapshotChunkModel:
    def test_create_chunk(self):
        chunk = SnapshotChunk(
            snapshot_id=ULID(),
            chunk_index=0,
            chunk_type="page",
            chunk_label="Page 1",
            content_hash="def456",
            simhash=99,
            char_count=500,
            excerpt="First 500 chars...",
        )
        assert chunk.chunk_index == 0
        assert chunk.chunk_type == "page"


class TestChangeModel:
    def test_create_change(self):
        change = Change(
            watch_id=ULID(),
            previous_snapshot_id=ULID(),
            current_snapshot_id=ULID(),
            change_metadata={"modified": [{"index": 0, "label": "Page 1"}]},
        )
        assert change.change_metadata["modified"][0]["label"] == "Page 1"

    def test_visual_change_score_defaults_to_none(self):
        change = Change(
            watch_id=ULID(),
            previous_snapshot_id=ULID(),
            current_snapshot_id=ULID(),
        )
        assert change.visual_change_score is None

    def test_visual_change_score_can_be_set(self):
        change = Change(
            watch_id=ULID(),
            previous_snapshot_id=ULID(),
            current_snapshot_id=ULID(),
            visual_change_score=0.42,
        )
        assert change.visual_change_score == pytest.approx(0.42)


class TestTemporalProfileModel:
    def test_create_event_profile(self):
        profile = TemporalProfile(
            watch_id=ULID(),
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 4, 15),
            rules=[{"days_before": 30, "interval": "6h"}, {"days_before": 7, "interval": "1h"}],
            post_action=PostAction.REDUCE_FREQUENCY,
        )
        assert profile.profile_type == ProfileType.EVENT
        assert len(profile.rules) == 2
        assert profile.reference_date == date(2026, 4, 15)

    def test_create_seasonal_profile(self):
        profile = TemporalProfile(
            watch_id=ULID(),
            profile_type=ProfileType.SEASONAL,
            date_range_start=date(2026, 1, 15),
            date_range_end=date(2026, 6, 30),
            rules=[{"days_before": 0, "interval": "1h"}],
            post_action=PostAction.REDUCE_FREQUENCY,
        )
        assert profile.date_range_start == date(2026, 1, 15)

    def test_create_deadline_profile(self):
        profile = TemporalProfile(
            watch_id=ULID(),
            profile_type=ProfileType.DEADLINE,
            reference_date=date(2026, 5, 1),
            rules=[{"days_before": 14, "interval": "12h"}],
            post_action=PostAction.DEACTIVATE,
        )
        assert profile.post_action == PostAction.DEACTIVATE

    def test_defaults(self):
        profile = TemporalProfile(
            watch_id=ULID(),
            profile_type=ProfileType.EVENT,
            reference_date=date(2026, 4, 15),
            rules=[],
            post_action=PostAction.REDUCE_FREQUENCY,
        )
        assert profile.is_active is True
        assert profile.date_range_start is None


class TestDomainModel:
    def test_create_domain_with_defaults(self):
        d = Domain(name="example.com")
        assert d.name == "example.com"
        assert d.min_interval == 1.0
        assert d.max_concurrency == 2
        assert d.current_interval == 1.0
        assert d.last_request_at is None

    def test_create_domain_custom(self):
        d = Domain(name="slow.gov", min_interval=5.0, max_concurrency=1, current_interval=10.0)
        assert d.min_interval == 5.0
        assert d.max_concurrency == 1
        assert d.current_interval == 10.0

    def test_current_interval_defaults_to_min_interval(self):
        d = Domain(name="example.com", min_interval=3.0)
        assert d.current_interval == 3.0


class TestNotificationConfigModel:
    def test_create_apprise_config(self):
        config = NotificationConfig(
            watch_id=ULID(),
            apprise_url="slack://T/A/T/#ops",
            channel_hint="slack",
        )
        assert config.channel_hint == "slack"
        assert config.is_active is True

    def test_default_events(self):
        config = NotificationConfig(
            watch_id=ULID(),
            apprise_url="mailto://user:pass@example.com",
            channel_hint="mailto",
        )
        assert config.events == ["change_detected"]

    def test_custom_events(self):
        config = NotificationConfig(
            watch_id=ULID(),
            apprise_url="slack://T/A/T/#ops",
            channel_hint="slack",
            events=["change_detected", "watch_error"],
        )
        assert config.events == ["change_detected", "watch_error"]
