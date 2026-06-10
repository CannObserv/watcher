"""Integration tests for WatchedItem API endpoints."""

from unittest.mock import AsyncMock

import pytest
from archiver_client import NotFound, ServerError
from sqlalchemy import select

from src.core.models.audit_log import AuditLog, EventType
from tests.conftest import make_info_item

pytestmark = pytest.mark.integration


async def _make_watched_item(db_session, **overrides):
    """Helper: create a WatchedItem + parent InfoItem via the test fixtures."""
    from src.core.models.watched_item import WatchedItem

    item = await make_info_item(db_session)
    wi = WatchedItem(archiver_info_item_id=item.info_item_id, name=overrides.pop("name", "Test WI"))
    for k, v in overrides.items():
        setattr(wi, k, v)
    db_session.add(wi)
    await db_session.flush()
    await db_session.commit()
    return wi


class TestListWatchedItems:
    async def test_empty_list(self, client):
        response = await client.get("/api/v1/watched-items")
        assert response.status_code == 200
        assert response.json() == []

    async def test_list_returns_items(self, client, db_session):
        await _make_watched_item(db_session, name="Alpha")
        await _make_watched_item(db_session, name="Beta")
        response = await client.get("/api/v1/watched-items")
        assert response.status_code == 200
        names = [r["name"] for r in response.json()]
        assert {"Alpha", "Beta"} <= set(names)

    async def test_archived_excluded_by_default(self, client, db_session):
        from datetime import UTC, datetime

        await _make_watched_item(db_session, name="Active")
        await _make_watched_item(
            db_session, name="Archived", archived_at=datetime.now(UTC), is_active=False
        )
        response = await client.get("/api/v1/watched-items")
        names = [r["name"] for r in response.json()]
        assert "Active" in names
        assert "Archived" not in names

    async def test_archived_included_when_requested(self, client, db_session):
        from datetime import UTC, datetime

        await _make_watched_item(
            db_session, name="Archived", archived_at=datetime.now(UTC), is_active=False
        )
        response = await client.get("/api/v1/watched-items?include_archived=true")
        names = [r["name"] for r in response.json()]
        assert "Archived" in names


class TestGetWatchedItem:
    async def test_404_unknown(self, client):
        from ulid import ULID

        response = await client.get(f"/api/v1/watched-items/{ULID()}")
        assert response.status_code == 404

    async def test_returns_record(self, client, db_session):
        wi = await _make_watched_item(db_session, name="Single")
        response = await client.get(f"/api/v1/watched-items/{wi.id}")
        assert response.status_code == 200
        assert response.json()["name"] == "Single"

    async def test_returns_null_archiver_info_item_id_for_standalone(self, client, db_session):
        """Dashboard-created WatchedItems (archiver_info_item_id=None) must serialise cleanly.

        Regression for the ULIDStr BeforeValidator silently coercing None → "None".
        """
        from src.core.models.watched_item import WatchedItem

        wi = WatchedItem(name="Standalone", effective_url="https://example.com/s")
        db_session.add(wi)
        await db_session.flush()
        await db_session.commit()

        response = await client.get(f"/api/v1/watched-items/{wi.id}")
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "Standalone"
        assert body["archiver_info_item_id"] is None


class TestPatchWatchedItem:
    async def test_404_unknown(self, client):
        from ulid import ULID

        response = await client.patch(f"/api/v1/watched-items/{ULID()}", json={"name": "x"})
        assert response.status_code == 404

    async def test_rename(self, client, db_session):
        wi = await _make_watched_item(db_session, name="Old")
        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={"name": "New"})
        assert response.status_code == 200
        assert response.json()["name"] == "New"

    async def test_update_schedule(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_schedule_config": {"interval": "30m"}},
        )
        assert response.status_code == 200
        assert response.json()["default_schedule_config"] == {"interval": "30m"}

    async def test_update_tags(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}", json={"default_tags": ["a", "b"]}
        )
        assert response.json()["default_tags"] == ["a", "b"]

    async def test_empty_patch_is_noop(self, client, db_session):
        wi = await _make_watched_item(db_session, name="Stays")
        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={})
        assert response.status_code == 200
        assert response.json()["name"] == "Stays"

    async def test_invalid_content_type(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_content_type": "bogus"},
        )
        assert response.status_code == 422


class TestArchiveRestore:
    async def test_archive_marks_record(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.post(f"/api/v1/watched-items/{wi.id}/archive")
        assert response.status_code == 200
        data = response.json()
        assert data["archived_at"] is not None
        assert data["is_active"] is False

    async def test_archive_cascades_to_child_watches(self, client, db_session):
        from tests.conftest import make_watch

        wi = await _make_watched_item(db_session)
        w1 = await make_watch(db_session, name="C1", watched_item=wi)
        w2 = await make_watch(db_session, name="C2", watched_item=wi)
        await db_session.commit()
        response = await client.post(f"/api/v1/watched-items/{wi.id}/archive")
        assert response.status_code == 200
        # Reload children and confirm cascade
        await db_session.refresh(w1)
        await db_session.refresh(w2)
        assert w1.is_active is False and w1.is_archived is True
        assert w2.is_active is False and w2.is_archived is True

    async def test_restore_parent_only(self, client, db_session):
        from datetime import UTC, datetime

        from tests.conftest import make_watch

        wi = await _make_watched_item(db_session, archived_at=datetime.now(UTC), is_active=False)
        w = await make_watch(
            db_session,
            name="ChildArchived",
            watched_item=wi,
            is_active=False,
            is_archived=True,
        )
        await db_session.commit()
        response = await client.post(f"/api/v1/watched-items/{wi.id}/restore")
        assert response.status_code == 200
        assert response.json()["archived_at"] is None
        await db_session.refresh(w)
        # Restore is parent-only — children stay archived.
        assert w.is_archived is True

    async def test_archive_404(self, client):
        from ulid import ULID

        response = await client.post(f"/api/v1/watched-items/{ULID()}/archive")
        assert response.status_code == 404


class TestMarkReviewed:
    async def test_stamps_now(self, client, db_session):
        wi = await _make_watched_item(db_session)
        before = wi.last_reviewed_at
        response = await client.post(f"/api/v1/watched-items/{wi.id}/mark-reviewed")
        assert response.status_code == 200
        stamped = response.json()["last_reviewed_at"]
        assert stamped is not None
        assert before is None or stamped > before.isoformat()

    async def test_404(self, client):
        from ulid import ULID

        response = await client.post(f"/api/v1/watched-items/{ULID()}/mark-reviewed")
        assert response.status_code == 404


class TestTemplateCrud:
    async def test_list_empty(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.get(f"/api/v1/watched-items/{wi.id}/notification-templates")
        assert response.status_code == 200
        assert response.json() == []

    async def test_create_returns_record(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.post(
            f"/api/v1/watched-items/{wi.id}/notification-templates",
            json={
                "title": "Email Greg",
                "channel_hint": "mailto://x:y@z",
                "events": ["change_detected"],
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Email Greg"
        assert data["watched_item_id"] == str(wi.id)

    async def test_create_404_unknown_parent(self, client):
        from ulid import ULID

        response = await client.post(
            f"/api/v1/watched-items/{ULID()}/notification-templates",
            json={"channel_hint": "mailto://x:y@z"},
        )
        assert response.status_code == 404

    async def test_patch_updates(self, client, db_session):
        wi = await _make_watched_item(db_session)
        create = await client.post(
            f"/api/v1/watched-items/{wi.id}/notification-templates",
            json={"channel_hint": "mailto://x:y@z"},
        )
        tpl_id = create.json()["id"]
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}/notification-templates/{tpl_id}",
            json={"is_active": False, "title": "Renamed"},
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False
        assert response.json()["title"] == "Renamed"

    async def test_delete(self, client, db_session):
        wi = await _make_watched_item(db_session)
        create = await client.post(
            f"/api/v1/watched-items/{wi.id}/notification-templates",
            json={"channel_hint": "mailto://x:y@z"},
        )
        tpl_id = create.json()["id"]
        response = await client.delete(
            f"/api/v1/watched-items/{wi.id}/notification-templates/{tpl_id}"
        )
        assert response.status_code == 204
        # Verify gone
        listing = await client.get(f"/api/v1/watched-items/{wi.id}/notification-templates")
        assert listing.json() == []


class TestWatchedItemRevisions:
    async def test_empty_revisions(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.get(f"/api/v1/watched-items/{wi.id}/revisions")
        assert response.status_code == 200
        assert response.json() == []

    async def test_returns_revisions_newest_first(self, client, db_session):
        from datetime import UTC, datetime, timedelta

        from src.core.models.change_revision import ChangeRevision

        wi = await _make_watched_item(db_session)
        now = datetime.now(UTC)
        r1 = ChangeRevision(
            watched_item_id=wi.id,
            content_fingerprint="sha256:" + "a" * 64,
            captured_at=now - timedelta(hours=1),
            schema_version=1,
        )
        r2 = ChangeRevision(
            watched_item_id=wi.id,
            content_fingerprint="sha256:" + "b" * 64,
            captured_at=now,
            schema_version=1,
        )
        db_session.add_all([r1, r2])
        await db_session.commit()

        response = await client.get(f"/api/v1/watched-items/{wi.id}/revisions")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        # Newest first
        assert data[0]["content_fingerprint"] == r2.content_fingerprint
        assert data[1]["content_fingerprint"] == r1.content_fingerprint

    async def test_revision_response_fields(self, client, db_session):
        from datetime import UTC, datetime

        from src.core.models.change_revision import ChangeRevision

        wi = await _make_watched_item(db_session)
        rev = ChangeRevision(
            watched_item_id=wi.id,
            content_fingerprint="sha256:" + "c" * 64,
            captured_at=datetime.now(UTC),
            content_size_bytes=512,
            schema_version=1,
        )
        db_session.add(rev)
        await db_session.commit()

        response = await client.get(f"/api/v1/watched-items/{wi.id}/revisions")
        data = response.json()[0]
        assert "id" in data
        assert "watched_item_id" in data
        assert data["content_fingerprint"].startswith("sha256:")
        assert data["content_size_bytes"] == 512
        assert data["schema_version"] == 1
        assert data["archiver_revision_id"] is None

    async def test_revisions_404_unknown_watched_item(self, client):
        from ulid import ULID

        response = await client.get(f"/api/v1/watched-items/{ULID()}/revisions")
        assert response.status_code == 404


class TestCreateWatchedItem:
    async def test_creates_with_info_item_name_fallback(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="Source Item")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={"archiver_info_item_id": str(item.info_item_id)},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["archiver_info_item_id"] == str(item.info_item_id)
        # Name falls back to the InfoItem's name when not supplied.
        assert body["name"] == "Source Item"
        assert body["default_schedule_config"] is None
        assert body["archived_at"] is None

    async def test_uses_supplied_name(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="Source")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "archiver_info_item_id": str(item.info_item_id),
                "name": "Overridden",
                "default_schedule_config": {"interval": "10m"},
                "default_tags": ["regulatory"],
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Overridden"
        assert body["default_schedule_config"] == {"interval": "10m"}
        assert body["default_tags"] == ["regulatory"]

    async def test_duplicate_archiver_info_item_id_returns_409(
        self, client, db_session, info_client
    ):
        item = await make_info_item(db_session, name="X")
        await db_session.commit()
        r1 = await client.post(
            "/api/v1/watched-items", json={"archiver_info_item_id": str(item.info_item_id)}
        )
        assert r1.status_code == 201
        r2 = await client.post(
            "/api/v1/watched-items", json={"archiver_info_item_id": str(item.info_item_id)}
        )
        assert r2.status_code == 409
        assert "already" in r2.json()["detail"].lower()

    async def test_unknown_archiver_info_item_returns_422(self, client, info_client):
        info_client.get_info_item = AsyncMock(side_effect=NotFound("nope"))
        response = await client.post(
            "/api/v1/watched-items",
            json={"archiver_info_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ"},
        )
        assert response.status_code == 422

    async def test_archiver_server_error_returns_503_with_retry_after(self, client, info_client):
        info_client.get_info_item = AsyncMock(side_effect=ServerError("boom"))
        response = await client.post(
            "/api/v1/watched-items",
            json={"archiver_info_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ"},
        )
        assert response.status_code == 503
        assert response.headers.get("Retry-After") == "30"

    async def test_emits_audit_event(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="A")
        await db_session.commit()
        await client.post(
            "/api/v1/watched-items",
            json={"archiver_info_item_id": str(item.info_item_id)},
        )
        events = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_CREATED)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["source"] == "api"

    async def test_creates_with_url_and_source_specs(self, client, db_session, info_client):
        """url + source_specs set effective_url and source_specs on the WatchedItem."""

        item = await make_info_item(db_session, name="WithUrl")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "archiver_info_item_id": str(item.info_item_id),
                "url": "https://example.com/page",
                "source_specs": [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}],
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["effective_url"] == "https://example.com/page"
        assert body["source_specs"] == [
            {"schema_version": 1, "extraction": {"algorithm": "full_page"}}
        ]

    async def test_response_includes_effective_url_and_source_specs(
        self, client, db_session, info_client
    ):
        """WatchedItem response always includes effective_url and source_specs."""
        item = await make_info_item(db_session, name="RespFields")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={"archiver_info_item_id": str(item.info_item_id)},
        )
        assert response.status_code == 201
        body = response.json()
        assert "effective_url" in body
        assert "source_specs" in body
        # Default values when not supplied
        assert body["effective_url"] == ""
        assert body["source_specs"] == []

    async def test_creates_without_archiver_info_item_id_url_only(self, client, db_session):
        """URL-only creates a WatchedItem with archiver_info_item_id=null (#185 Phase A)."""
        response = await client.post(
            "/api/v1/watched-items",
            json={"url": "https://example.com/standalone", "name": "Standalone WI"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["archiver_info_item_id"] is None
        assert body["name"] == "Standalone WI"
        assert body["effective_url"] == "https://example.com/standalone"
        assert body["domain_name"] == "example.com"

    async def test_url_only_name_falls_back_to_domain(self, client, db_session):
        """Name defaults to the probed domain when not supplied."""
        response = await client.post(
            "/api/v1/watched-items",
            json={"url": "https://lcb.wa.gov/page"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["name"] == "lcb.wa.gov"

    async def test_neither_archiver_info_item_id_nor_url_returns_422(self, client):
        """At least one of archiver_info_item_id or url is required."""
        response = await client.post("/api/v1/watched-items", json={"name": "Missing anchor"})
        assert response.status_code == 422

    async def test_url_only_create_stores_archiver_info_source_id(self, client, db_session):
        """#1 fix: archiver_info_source_id must be persisted on the URL-only path."""
        src_id = "01ABCDEFGHJKMNPQRSTVWXYZ00"
        response = await client.post(
            "/api/v1/watched-items",
            json={"url": "https://example.com/srcid", "archiver_info_source_id": src_id},
        )
        assert response.status_code == 201, response.text
        assert response.json()["archiver_info_source_id"] == src_id

    async def test_create_stores_archiver_info_source_id(self, client, db_session, info_client):
        """archiver_info_source_id is persisted when supplied on create."""
        item = await make_info_item(db_session, name="SrcId")
        await db_session.commit()
        src_id = "01ABCDEFGHJKMNPQRSTVWXYZ00"
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "archiver_info_item_id": str(item.info_item_id),
                "archiver_info_source_id": src_id,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["archiver_info_source_id"] == src_id

    async def test_response_includes_health_status_last_checked_at(self, client, db_session):
        """health_status and last_checked_at appear in every WatchedItem response."""
        wi = await _make_watched_item(db_session, name="HealthFields")
        response = await client.get(f"/api/v1/watched-items/{wi.id}")
        assert response.status_code == 200
        body = response.json()
        assert "health_status" in body
        assert "last_checked_at" in body
        assert body["health_status"] == "unknown"
        assert body["last_checked_at"] is None


class TestListFilterByArchiverInfoItemId:
    async def test_filter_returns_matching_item(self, client, db_session):
        """?archiver_info_item_id= returns only the WatchedItem with that ULID."""
        from src.core.models.watched_item import WatchedItem

        item = await make_info_item(db_session, name="Filtered")
        wi = WatchedItem(archiver_info_item_id=item.info_item_id, name="Match")
        db_session.add(wi)
        await _make_watched_item(db_session, name="Other")
        await db_session.commit()

        response = await client.get(
            f"/api/v1/watched-items?archiver_info_item_id={item.info_item_id}"
        )
        assert response.status_code == 200
        names = [r["name"] for r in response.json()]
        assert names == ["Match"]

    async def test_filter_empty_result(self, client, db_session):
        """?archiver_info_item_id= for a ULID with no matching item returns []."""
        from ulid import ULID

        await _make_watched_item(db_session, name="Unrelated")
        await db_session.commit()

        response = await client.get(f"/api/v1/watched-items?archiver_info_item_id={ULID()}")
        assert response.status_code == 200
        assert response.json() == []

    async def test_filter_bad_ulid_returns_400(self, client):
        """?archiver_info_item_id=bad returns 400, not 404 or 500."""
        response = await client.get("/api/v1/watched-items?archiver_info_item_id=not-a-ulid")
        assert response.status_code == 400


class TestPatchArchiverInfoSourceId:
    async def test_patch_updates_archiver_info_source_id(self, client, db_session):
        """PATCH sets archiver_info_source_id on the WatchedItem."""
        wi = await _make_watched_item(db_session)
        src_id = "01ABCDEFGHJKMNPQRSTVWXYZ00"
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"archiver_info_source_id": src_id},
        )
        assert response.status_code == 200
        assert response.json()["archiver_info_source_id"] == src_id

    async def test_patch_clears_archiver_info_source_id(self, client, db_session):
        """PATCH with null clears archiver_info_source_id."""
        wi = await _make_watched_item(db_session)
        wi.archiver_info_source_id = "01ABCDEFGHJKMNPQRSTVWXYZ00"
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"archiver_info_source_id": None},
        )
        assert response.status_code == 200
        assert response.json()["archiver_info_source_id"] is None


class TestPatchEffectiveUrlAndSourceSpecs:
    """#187 — PATCH must persist effective_url and source_specs through the full route."""

    async def test_patch_updates_effective_url(self, client, db_session):
        """PATCH sets effective_url on the WatchedItem."""
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"effective_url": "https://example.com/new-path"},
        )
        assert response.status_code == 200
        assert response.json()["effective_url"] == "https://example.com/new-path"

    async def test_patch_updates_source_specs(self, client, db_session):
        """PATCH sets source_specs on the WatchedItem."""
        wi = await _make_watched_item(db_session)
        specs = [
            {"schema_version": 1, "extraction": {"algorithm": "css_selector", "selector": "main"}}
        ]
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"source_specs": specs},
        )
        assert response.status_code == 200
        assert response.json()["source_specs"] == specs

    async def test_patch_rejects_null_effective_url(self, client, db_session):
        """PATCH with null effective_url returns 422."""
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"effective_url": None},
        )
        assert response.status_code == 422

    async def test_patch_invalid_url_returns_422(self, client, db_session):
        """PATCH with a non-URL string returns 422."""
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"effective_url": "not-a-url"},
        )
        assert response.status_code == 422


class TestCheckNow:
    async def test_202_enqueues_task(self, client, db_session):
        """POST /check-now returns 202 with WatchedItem body and defers a task."""
        from unittest.mock import AsyncMock, patch

        wi = await _make_watched_item(db_session, name="CheckNow")
        wi.effective_url = "https://example.com"
        await db_session.commit()

        with patch("src.api.routes.watched_items.check_watched_item") as mock_task:
            mock_task.configure.return_value.defer_async = AsyncMock()
            response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")

        assert response.status_code == 202
        body = response.json()
        assert body["id"] == str(wi.id)
        mock_task.configure.return_value.defer_async.assert_awaited_once_with(
            watched_item_id=str(wi.id)
        )

    async def test_404_unknown(self, client):
        from ulid import ULID

        response = await client.post(f"/api/v1/watched-items/{ULID()}/check-now")
        assert response.status_code == 404

    async def test_409_when_archived(self, client, db_session):
        from datetime import UTC, datetime

        wi = await _make_watched_item(db_session, archived_at=datetime.now(UTC), is_active=False)
        response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")
        assert response.status_code == 409
        assert "archived" in response.json()["detail"].lower()

    async def test_422_when_no_effective_url(self, client, db_session):
        from src.core.models.watched_item import WatchedItem

        wi = WatchedItem(name="NoUrl", effective_url="")
        db_session.add(wi)
        await db_session.commit()

        response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")
        assert response.status_code == 422
        assert "url" in response.json()["detail"].lower()

    async def test_202_when_no_active_watches(self, client, db_session):
        """#187: check-now must succeed even with zero active Watch subscriptions."""
        from unittest.mock import AsyncMock, patch

        wi = await _make_watched_item(db_session, name="NoActiveWatches")
        wi.effective_url = "https://example.com"
        await db_session.commit()

        with patch("src.api.routes.watched_items.check_watched_item") as mock_task:
            mock_task.configure.return_value.defer_async = AsyncMock()
            response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")

        assert response.status_code == 202

    async def test_202_when_all_watches_archived(self, client, db_session):
        """#187: archived-only Watch subscriptions must not block check-now."""
        from unittest.mock import AsyncMock, patch

        from tests.conftest import make_watch

        wi = await _make_watched_item(db_session, name="AllArchivedWatches")
        wi.effective_url = "https://example.com"
        await make_watch(
            db_session, name="Archived", watched_item=wi, is_archived=True, is_active=False
        )
        await db_session.commit()

        with patch("src.api.routes.watched_items.check_watched_item") as mock_task:
            mock_task.configure.return_value.defer_async = AsyncMock()
            response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")

        assert response.status_code == 202

    async def test_202_emits_audit_log(self, client, db_session):
        """#3 fix: check-now must write a WATCHED_ITEM_CHECK_REQUESTED audit entry."""
        from unittest.mock import AsyncMock, patch

        from src.core.models.audit_log import AuditLog, EventType

        wi = await _make_watched_item(db_session, name="AuditCheckNow")
        wi.effective_url = "https://example.com"
        await db_session.commit()

        with patch("src.api.routes.watched_items.check_watched_item") as mock_task:
            mock_task.configure.return_value.defer_async = AsyncMock()
            await client.post(f"/api/v1/watched-items/{wi.id}/check-now")

        result = await db_session.execute(
            select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_CHECK_REQUESTED)
        )
        entries = result.scalars().all()
        assert len(entries) == 1
        assert entries[0].payload["watched_item_id"] == str(wi.id)
