"""Tests for SQLAlchemy base and ULID column type."""

from unittest.mock import MagicMock

import pytest
from ulid import ULID

from src.core.database import get_database_url, get_engine, reset_engine
from src.core.models.audit_log import AuditLog, EventType, audit
from src.core.models.base import ULIDType
from src.core.models.change_revision import ChangeRevision
from src.core.models.domain import Domain
from src.core.models.pending_archiver_sync import PendingArchiverSync
from src.core.models.watched_item import WatchedItem, WatchHealthStatus


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
    """content_media_type is free-form raw MIME — no enum, no validator (#168)."""

    def test_content_media_type_accepts_raw_mime(self):
        wi = WatchedItem(
            archiver_info_source_id=str(ULID()),
            content_media_type="text/html; charset=utf-8",
        )
        assert wi.content_media_type == "text/html; charset=utf-8"

    def test_content_media_type_defaults_none(self):
        wi = WatchedItem(archiver_info_source_id=str(ULID()))
        assert wi.content_media_type is None

    def test_content_media_type_accepts_arbitrary_string(self):
        # No enum membership check — any origin-reported value is stored verbatim.
        wi = WatchedItem(
            archiver_info_source_id=str(ULID()),
            content_media_type="application/vnd.custom+json",
        )
        assert wi.content_media_type == "application/vnd.custom+json"

    def test_health_status_default(self):
        wi = WatchedItem(archiver_info_source_id=str(ULID()))
        assert wi.health_status == WatchHealthStatus.UNKNOWN

    def test_health_status_coercion_from_string(self):
        wi = WatchedItem(archiver_info_source_id=str(ULID()), health_status="ok")
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
        entry = audit(mock_session, EventType.WATCHED_ITEM_CREATED)
        mock_session.add.assert_called_once_with(entry)


class TestEventType:
    """Tests for EventType string constants."""

    def test_constants_have_expected_values(self):
        assert EventType.WATCHED_ITEM_CREATED == "watched_item.created"
        assert EventType.CHECK_SNAPSHOT_CREATED == "check.snapshot_created"
        assert EventType.CHECK_NO_CHANGE == "check.no_change"
        assert EventType.CHECK_FETCH_FAILED == "check.fetch_failed"
        assert EventType.DOMAIN_ACTIVATED == "domain.activated"
        assert EventType.DOMAIN_DEACTIVATED == "domain.deactivated"

    def test_all_constants_are_unique(self):
        values = [v for k, v in vars(EventType).items() if not k.startswith("_")]
        assert len(values) == len(set(values)), "EventType constants must be unique"


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


class TestDomainModel:
    def test_create_domain_with_defaults(self):
        d = Domain(name="example.com")
        assert d.name == "example.com"
        assert d.is_active is True
        assert d.is_suspended is False

    def test_a_deactivated_domain_is_suspended(self):
        assert Domain(name="slow.gov", is_active=False).is_suspended is True

    def test_rate_limiter_columns_are_gone(self):
        # #272: the four columns the retired in-process limiter (#241 step 5)
        # left behind are dropped, not merely inert.
        for column in ("max_concurrency", "current_interval", "decay_window", "last_request_at"):
            assert not hasattr(Domain, column)


class TestRetiredTransportColumns:
    """#261: the three columns the content.revisions cutover left behind are gone.

    They were released, not dropped, in `32140463c26c` because no single deploy
    order makes dropping a NOT NULL column safe. The publisher is live, so the
    contract step landed — and nothing may reintroduce them: the scratch cache
    they described does not exist, and Archiver re-derives its revision id from
    `(info_source_id, content_fingerprint)`, its own uniqueness constraint.
    """

    def test_pending_archiver_sync_has_no_scratch_cache_columns(self):
        columns = set(PendingArchiverSync.__table__.columns.keys())
        assert "content_cache_uri" not in columns
        assert "content_cache_expires_at" not in columns

    def test_change_revision_has_no_archiver_revision_id(self):
        assert "archiver_revision_id" not in ChangeRevision.__table__.columns.keys()


class TestOutboxDuePredicateIsIndexed:
    """#291 CR 12: the outbox's two hot queries both filter `next_attempt_at`
    beside `dead_lettered_at IS NULL`, and neither had an index.

    `select_due` scans every minute and `clear_backoffs` adds a second
    predicate on the same column. Free while the table is near-empty — which is
    its normal state, since a row is deleted on success — but the case worth
    indexing for is the one #291 exists for: an outage has grown the backlog
    into the thousands and every tick scans it twice.

    The partial predicate matches both queries, so a dead-lettered row costs
    nothing to skip and nothing to store.
    """

    def _due_index(self):
        for index in PendingArchiverSync.__table__.indexes:
            if index.name == "ix_pending_archiver_sync_due":
                return index
        return None

    def test_next_attempt_at_carries_an_index(self):
        assert self._due_index() is not None, (
            "select_due and clear_backoffs both scan next_attempt_at"
        )

    def test_the_index_covers_the_order_by_select_due_uses(self):
        """Both key columns, in `select_due`'s order. The tie case is not
        hypothetical — a bulk clear stamps one timestamp across the whole
        backlog, so `next_attempt_at` alone leaves a sort over every backed-off
        row to return one batch, on the pass right after a recovery (CR 14).
        Measured at 5 000 tied rows: index scan of all 5 000 plus an
        Incremental Sort, against a 100-row index scan with the id.
        """
        index = self._due_index()
        assert index is not None
        assert [c.name for c in index.columns] == ["next_attempt_at", "id"]

    def test_the_index_is_partial_on_the_live_rows(self):
        index = self._due_index()
        assert index is not None
        where = index.dialect_options["postgresql"]["where"]
        assert "dead_lettered_at" in str(where)
