"""Tests for SQLAlchemy base and ULID column type."""

from datetime import date
from unittest.mock import MagicMock

import pytest
from ulid import ULID

from src.core.database import get_database_url, get_engine, reset_engine
from src.core.models.audit_log import AuditLog, EventType, audit
from src.core.models.base import ULIDType
from src.core.models.domain import Domain
from src.core.models.notification_template import (
    VISIBILITIES,
    VISIBILITY_DOMAIN,
    VISIBILITY_GLOBAL,
    VISIBILITY_WATCHED_ITEM,
    NotificationTemplate,
)
from src.core.models.temporal_profile import PostAction, ProfileType, TemporalProfile
from src.core.models.watched_item import ContentType, WatchedItem, WatchHealthStatus


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


class TestWatchedItemContentType:
    """ContentType + health enums now live on WatchedItem (#191 collapse)."""

    def test_default_content_type(self):
        wi = WatchedItem(name="Test", default_content_type=ContentType.HTML)
        assert wi.default_content_type == ContentType.HTML

    def test_content_type_enum_values(self):
        assert ContentType.HTML.value == "html"
        assert ContentType.PDF.value == "pdf"
        assert ContentType.FILE.value == "file"

    def test_default_content_type_coerces_string(self):
        wi = WatchedItem(name="Coerce Test", default_content_type="pdf")
        assert wi.default_content_type is ContentType.PDF

    def test_default_content_type_rejects_invalid(self):
        with pytest.raises(ValueError, match="Invalid default_content_type"):
            WatchedItem(name="Bad Type", default_content_type="invalid")

    def test_health_status_default(self):
        wi = WatchedItem(name="T")
        assert wi.health_status == WatchHealthStatus.UNKNOWN

    def test_health_status_coercion_from_string(self):
        wi = WatchedItem(name="T", health_status="ok")
        assert wi.health_status == WatchHealthStatus.OK


class TestAuditHelper:
    """Tests for the audit() helper function."""

    def test_audit_creates_entry_with_correct_fields(self):
        mock_session = MagicMock()
        wi_id = str(ULID())

        entry = audit(
            mock_session,
            EventType.WATCHED_ITEM_CREATED,
            watched_item_id=wi_id,
            name="Test Item",
        )

        assert isinstance(entry, AuditLog)
        assert entry.event_type == EventType.WATCHED_ITEM_CREATED
        assert entry.payload == {"watched_item_id": wi_id, "name": "Test Item"}
        mock_session.add.assert_called_once_with(entry)

    def test_audit_minimal_payload(self):
        mock_session = MagicMock()
        entry = audit(mock_session, EventType.CHECK_FETCH_FAILED, status_code=500)

        assert entry.payload == {"status_code": 500}
        mock_session.add.assert_called_once_with(entry)

    def test_audit_adds_to_session(self):
        mock_session = MagicMock()
        entry = audit(mock_session, EventType.WATCHED_ITEM_ARCHIVED)
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
        assert EventType.PROFILE_CREATED == "profile.created"
        assert EventType.PROFILE_UPDATED == "profile.updated"
        assert EventType.PROFILE_DELETED == "profile.deleted"

    def test_all_constants_are_unique(self):
        values = [v for k, v in vars(EventType).items() if not k.startswith("_")]
        assert len(values) == len(set(values)), "EventType constants must be unique"

    def test_notification_template_constants_have_expected_values(self):
        # #200: legacy watch_nc.* / domain_nc_default.* events folded into the
        # unified notification_template.* set (every target is a NotificationTemplate).
        assert EventType.NOTIFICATION_TEMPLATE_CREATED == "notification_template.created"
        assert EventType.NOTIFICATION_TEMPLATE_UPDATED == "notification_template.updated"
        assert EventType.NOTIFICATION_TEMPLATE_DELETED == "notification_template.deleted"
        assert EventType.NOTIFICATION_TEMPLATE_TESTED == "notification_template.tested"


class TestAuditLogModel:
    def test_create_audit_log_entry(self):
        entry = AuditLog(
            event_type=EventType.WATCHED_ITEM_CREATED,
            payload={"name": "Test Item"},
        )
        assert entry.event_type == EventType.WATCHED_ITEM_CREATED
        assert entry.payload == {"name": "Test Item"}

    def test_create_audit_log_with_watched_item_id(self):
        wi_id = str(ULID())
        entry = AuditLog(
            event_type="check.started",
            payload={"watched_item_id": wi_id, "url": "https://example.com"},
        )
        assert entry.payload["watched_item_id"] == wi_id


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


class TestTemporalProfileModel:
    def test_create_event_profile(self):
        profile = TemporalProfile(
            watched_item_id=ULID(),
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
            watched_item_id=ULID(),
            profile_type=ProfileType.SEASONAL,
            date_range_start=date(2026, 1, 15),
            date_range_end=date(2026, 6, 30),
            rules=[{"days_before": 0, "interval": "1h"}],
            post_action=PostAction.REDUCE_FREQUENCY,
        )
        assert profile.date_range_start == date(2026, 1, 15)

    def test_create_deadline_profile(self):
        profile = TemporalProfile(
            watched_item_id=ULID(),
            profile_type=ProfileType.DEADLINE,
            reference_date=date(2026, 5, 1),
            rules=[{"days_before": 14, "interval": "12h"}],
            post_action=PostAction.DEACTIVATE,
        )
        assert profile.post_action == PostAction.DEACTIVATE

    def test_defaults(self):
        profile = TemporalProfile(
            watched_item_id=ULID(),
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


class TestNotificationTemplateModel:
    """Unified NotificationTemplate replaces the five legacy dispatch sources (#200).

    One scoped table; the row's ``visibility`` decides where it fires. The
    visibility/ref CHECK constraint is exercised against a real session in
    ``test_watched_item.py``; these are ORM-construction unit checks.
    """

    def test_create_global_template(self):
        tmpl = NotificationTemplate(
            title="Global Alert",
            channel_hint="slack",
            events=["change_detected"],
            remote_channel_id=str(ULID()),
            visibility=VISIBILITY_GLOBAL,
        )
        assert tmpl.visibility == VISIBILITY_GLOBAL
        assert tmpl.title == "Global Alert"
        assert tmpl.channel_hint == "slack"
        assert tmpl.domain_name is None
        assert tmpl.watched_item_id is None

    def test_create_domain_template(self):
        tmpl = NotificationTemplate(
            title="Domain Alert",
            channel_hint="mailto",
            events=["change_detected"],
            remote_channel_id=str(ULID()),
            visibility=VISIBILITY_DOMAIN,
            domain_name="example.com",
        )
        assert tmpl.visibility == VISIBILITY_DOMAIN
        assert tmpl.domain_name == "example.com"
        assert tmpl.watched_item_id is None

    def test_create_watched_item_template(self):
        wi_id = ULID()
        tmpl = NotificationTemplate(
            title="Item Alert",
            channel_hint="slack",
            events=["change_detected"],
            remote_channel_id=str(ULID()),
            visibility=VISIBILITY_WATCHED_ITEM,
            watched_item_id=wi_id,
        )
        assert tmpl.visibility == VISIBILITY_WATCHED_ITEM
        assert tmpl.watched_item_id == wi_id
        assert tmpl.domain_name is None

    def test_custom_events(self):
        tmpl = NotificationTemplate(
            title="Multi-event",
            channel_hint="slack",
            events=["change_detected", "watch_error"],
            visibility=VISIBILITY_GLOBAL,
        )
        assert tmpl.events == ["change_detected", "watch_error"]

    def test_content_config_defaults_none(self):
        tmpl = NotificationTemplate(
            title="No config",
            channel_hint="mailto",
            events=["change_detected"],
            visibility=VISIBILITY_GLOBAL,
        )
        assert tmpl.content_config is None

    def test_visibility_constants(self):
        assert VISIBILITY_GLOBAL == "global"
        assert VISIBILITY_DOMAIN == "domain"
        assert VISIBILITY_WATCHED_ITEM == "watched_item"
        assert VISIBILITIES == (
            VISIBILITY_GLOBAL,
            VISIBILITY_DOMAIN,
            VISIBILITY_WATCHED_ITEM,
        )
