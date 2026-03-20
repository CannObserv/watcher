"""Tests for SQLAlchemy base and ULID column type."""

from ulid import ULID

from src.core.models.audit_log import AuditLog
from src.core.models.base import ULIDType
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


class TestAuditLogModel:
    def test_create_audit_log_entry(self):
        entry = AuditLog(
            event_type="watch.created",
            payload={"watch_name": "Test Watch"},
        )
        assert entry.event_type == "watch.created"
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
