"""Integration tests for WatchedItem API endpoints."""

from unittest.mock import AsyncMock

import pytest
from archiver_client import NotFound, ServerError
from sqlalchemy import select, text
from ulid import ULID

from src.core.models.audit_log import AuditLog, EventType
from tests.conftest import make_info_item

pytestmark = pytest.mark.integration


def _create_body(info_item_id, **overrides):
    """A minimal valid ``POST /api/v1/watched-items`` body.

    #251: the InfoItem link, its URL, and the InfoSource link are all required —
    there is no URL-only creation path.
    """
    body = {
        "archiver_info_item_id": str(info_item_id),
        "url": "https://example.com/page",
        "archiver_info_source_id": str(ULID()),
    }
    body.update(overrides)
    return body


async def _make_watched_item(db_session, **overrides):
    """Helper: create a WatchedItem + parent InfoItem via the test fixtures."""
    from src.core.models.watched_item import WatchedItem

    item = await make_info_item(db_session)
    wi = WatchedItem(
        archiver_info_source_id=str(ULID()),
        archiver_info_item_id=item.info_item_id,
        name=overrides.pop("name", "Test WI"),
    )
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

    async def test_update_content_media_type_override(self, client, db_session):
        # #168: content_media_type is free-form raw MIME (operator override path).
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"content_media_type": "application/pdf"},
        )
        assert response.status_code == 200
        assert response.json()["content_media_type"] == "application/pdf"


class TestArchiveRestore:
    async def test_archive_marks_record(self, client, db_session):
        wi = await _make_watched_item(db_session)
        response = await client.post(f"/api/v1/watched-items/{wi.id}/archive")
        assert response.status_code == 200
        data = response.json()
        assert data["archived_at"] is not None
        assert data["is_active"] is False

    async def test_archive_is_idempotent(self, client, db_session):
        """#191: WatchedItem is the single entity — archive sets archived_at + inactive."""
        wi = await _make_watched_item(db_session)
        await db_session.commit()
        first = await client.post(f"/api/v1/watched-items/{wi.id}/archive")
        assert first.status_code == 200
        stamped = first.json()["archived_at"]
        assert stamped is not None
        # Re-archiving leaves the original timestamp untouched.
        second = await client.post(f"/api/v1/watched-items/{wi.id}/archive")
        assert second.status_code == 200
        assert second.json()["archived_at"] == stamped

    async def test_restore_reactivates(self, client, db_session):
        from datetime import UTC, datetime

        wi = await _make_watched_item(db_session, archived_at=datetime.now(UTC), is_active=False)
        await db_session.commit()
        response = await client.post(f"/api/v1/watched-items/{wi.id}/restore")
        assert response.status_code == 200
        data = response.json()
        assert data["archived_at"] is None
        assert data["is_active"] is True

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


class TestDeleteWatchedItem:
    """Hard delete — archived-only, DB cascade, audited (#210)."""

    async def test_delete_archived_returns_204_and_removes_row(self, client, db_session):
        from datetime import UTC, datetime

        from src.core.models.watched_item import WatchedItem

        wi = await _make_watched_item(db_session, archived_at=datetime.now(UTC), is_active=False)
        wi_id = wi.id

        response = await client.delete(f"/api/v1/watched-items/{wi_id}")
        assert response.status_code == 204
        assert response.content == b""

        gone = (
            await db_session.execute(select(WatchedItem).where(WatchedItem.id == wi_id))
        ).scalar_one_or_none()
        assert gone is None

    async def test_delete_non_archived_returns_409_and_keeps_row(self, client, db_session):
        from src.core.models.watched_item import WatchedItem

        wi = await _make_watched_item(db_session)  # active, not archived
        wi_id = wi.id

        response = await client.delete(f"/api/v1/watched-items/{wi_id}")
        assert response.status_code == 409

        still = (
            await db_session.execute(select(WatchedItem).where(WatchedItem.id == wi_id))
        ).scalar_one_or_none()
        assert still is not None

    async def test_delete_unknown_returns_404(self, client):
        from ulid import ULID

        response = await client.delete(f"/api/v1/watched-items/{ULID()}")
        assert response.status_code == 404

    async def test_delete_malformed_id_returns_404(self, client):
        response = await client.delete("/api/v1/watched-items/not-a-ulid")
        assert response.status_code == 404

    async def test_delete_cascades_children(self, client, db_session):
        from datetime import UTC, datetime

        from src.core.models.change_revision import ChangeRevision
        from src.core.models.notification_template import (
            VISIBILITY_WATCHED_ITEM,
            NotificationTemplate,
        )
        from src.core.models.pending_archiver_sync import PendingArchiverSync
        from src.core.models.temporal_profile import (
            PostAction,
            ProfileType,
            TemporalProfile,
        )

        now = datetime.now(UTC)
        wi = await _make_watched_item(db_session, archived_at=now, is_active=False)
        wi_id = wi.id

        profile = TemporalProfile(
            watched_item_id=wi_id,
            profile_type=ProfileType.SEASONAL,
            rules=[{"days_before": 0, "interval": "1h"}],
            post_action=PostAction.REDUCE_FREQUENCY,
        )
        tmpl = NotificationTemplate(
            title="Item template",
            watched_item_id=wi_id,
            channel_hint="slack",
            visibility=VISIBILITY_WATCHED_ITEM,
        )
        revision = ChangeRevision(
            watched_item_id=wi_id,
            content_fingerprint="abc",
            captured_at=now,
            schema_version=1,
        )
        db_session.add_all([profile, tmpl, revision])
        await db_session.flush()
        sync = PendingArchiverSync(
            change_revision_id=revision.id,
            watched_item_id=wi_id,
            content_cache_uri="file:///tmp/x",
            content_cache_expires_at=now,
            next_attempt_at=now,
        )
        db_session.add(sync)
        await db_session.flush()
        await db_session.commit()

        response = await client.delete(f"/api/v1/watched-items/{wi_id}")
        assert response.status_code == 204

        for model in (TemporalProfile, NotificationTemplate, ChangeRevision, PendingArchiverSync):
            remaining = (
                (await db_session.execute(select(model).where(model.watched_item_id == wi_id)))
                .scalars()
                .all()
            )
            assert remaining == [], f"{model.__name__} rows survived the cascade"

    async def test_delete_writes_audit_that_survives(self, client, db_session):
        from datetime import UTC, datetime

        wi = await _make_watched_item(
            db_session, name="ToDelete", archived_at=datetime.now(UTC), is_active=False
        )
        wi_id = wi.id

        response = await client.delete(f"/api/v1/watched-items/{wi_id}")
        assert response.status_code == 204

        rows = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_DELETED)
                )
            )
            .scalars()
            .all()
        )
        matched = [r for r in rows if r.payload.get("watched_item_id") == str(wi_id)]
        assert len(matched) == 1
        payload = matched[0].payload
        # The trail must carry name + url (not just the id) so the deleted item is
        # identifiable after the row is gone.
        assert payload["name"] == "ToDelete"
        assert "url" in payload
        assert payload["source"] == "api"

    async def test_delete_frees_domain_delete_guard(self, client, db_session):
        """An archived item still pins its domain; deleting it unblocks domain delete (#209)."""
        from datetime import UTC, datetime

        from src.core.models.domain import Domain
        from src.core.models.watched_item import WatchedItem

        # The local _make_watched_item helper (unlike conftest's make_watched_item)
        # does not auto-create the Domain, and domain_name is an enforced FK — so the
        # Domain row must exist before the archived item can reference it.
        domain = Domain(name="delete-me.example.com")
        db_session.add(domain)
        await db_session.flush()
        wi = await _make_watched_item(
            db_session,
            name="LastRef",
            domain_name="delete-me.example.com",
            archived_at=datetime.now(UTC),
            is_active=False,
        )
        wi_id = wi.id

        blocked = await client.delete("/api/v1/domains/delete-me.example.com")
        assert blocked.status_code == 409

        assert (await client.delete(f"/api/v1/watched-items/{wi_id}")).status_code == 204

        freed = await client.delete("/api/v1/domains/delete-me.example.com")
        assert freed.status_code == 204
        gone = (
            await db_session.execute(select(WatchedItem).where(WatchedItem.id == wi_id))
        ).scalar_one_or_none()
        assert gone is None


# TestTemplateCrud removed (#200): the per-WatchedItem
# /api/v1/watched-items/{id}/notification-templates endpoints (list/create/patch/
# delete of WatchedItemNotificationTemplate rows) were deleted in the notification-
# model consolidation. Item-scoped notifications are now NotificationTemplate rows
# with visibility='watched_item', managed via the
# /api/v1/watched-items/{id}/notifications API surface and the dashboard item-
# template routes (tests/dashboard/test_watched_item_templates.py).


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
            json={
                "url": "https://example.com/page",
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["archiver_info_item_id"] == str(item.info_item_id)
        # Name falls back to the InfoItem's name when not supplied.
        assert body["name"] == "Source Item"
        assert body["default_schedule_config"] is None
        assert body["archived_at"] is None

    async def test_omitted_default_schedule_config_persists_sql_null(
        self, client, db_session, info_client
    ):
        """Omitting default_schedule_config stores SQL NULL, not JSONB 'null' (#198).

        Both representations read back as Python None, so the route response is no
        guard — assert the on-disk value matches ``IS NULL``.
        """
        item = await make_info_item(db_session, name="NullCfg")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "url": "https://example.com/page",
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
            },
        )
        assert response.status_code == 201, response.text
        wi_id = response.json()["id"]
        is_sql_null = (
            await db_session.execute(
                text("SELECT default_schedule_config IS NULL FROM watched_items WHERE id = :id"),
                {"id": wi_id},
            )
        ).scalar_one()
        assert is_sql_null is True

    async def test_uses_supplied_name(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="Source")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "url": "https://example.com/page",
                "archiver_info_source_id": str(ULID()),
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
            "/api/v1/watched-items",
            json={
                "url": "https://example.com/page",
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
            },
        )
        assert r1.status_code == 201
        r2 = await client.post(
            "/api/v1/watched-items",
            json={
                "url": "https://example.com/page",
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
            },
        )
        assert r2.status_code == 409
        assert "already" in r2.json()["detail"].lower()

    async def test_unknown_archiver_info_item_returns_422(self, client, info_client):
        info_client.get_info_item = AsyncMock(side_effect=NotFound("nope"))
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "url": "https://example.com/page",
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
            },
        )
        assert response.status_code == 422

    async def test_archiver_server_error_returns_503_with_retry_after(self, client, info_client):
        info_client.get_info_item = AsyncMock(side_effect=ServerError("boom"))
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "url": "https://example.com/page",
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": "01ZZZZZZZZZZZZZZZZZZZZZZZZ",
            },
        )
        assert response.status_code == 503
        assert response.headers.get("Retry-After") == "30"

    async def test_emits_audit_event(self, client, db_session, info_client):
        item = await make_info_item(db_session, name="A")
        await db_session.commit()
        await client.post(
            "/api/v1/watched-items",
            json={
                "url": "https://example.com/page",
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
            },
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
                "archiver_info_source_id": str(ULID()),
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
            json={
                "url": "https://example.com/page",
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert "effective_url" in body
        assert "source_specs" in body
        # effective_url is the supplied (Archiver-authoritative) URL; source_specs
        # default to empty when not seeded at create.
        assert body["effective_url"] == "https://example.com/page"
        assert body["source_specs"] == []

    async def test_create_on_inactive_domain_sets_domain_suspended(
        self, client, db_session, info_client
    ):
        """#191 CR-1: creating on an already-inactive domain marks domain_suspended.

        schedule_tick gates solely on WatchedItem.domain_suspended now (no live
        Domain join), so the flag must be seeded from the domain's state at create.
        """
        from src.core.models.domain import Domain

        item = await make_info_item(db_session, name="OnInactive")
        db_session.add(Domain(name="inactive-create.example", is_active=False))
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(
                item.info_item_id,
                url="https://inactive-create.example/page",
                name="On Inactive Domain",
            ),
        )
        assert response.status_code == 201, response.text
        assert response.json()["domain_suspended"] is True

    async def test_create_on_active_domain_not_suspended(self, client, db_session, info_client):
        """Items created on a healthy (or fresh) domain are not domain-suspended."""
        item = await make_info_item(db_session, name="OnActive")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(
                item.info_item_id,
                url="https://active-create.example/page",
                name="On Active Domain",
            ),
        )
        assert response.status_code == 201, response.text
        assert response.json()["domain_suspended"] is False

    async def test_create_stores_archiver_info_source_id(self, client, db_session, info_client):
        """archiver_info_source_id is persisted when supplied on create."""
        item = await make_info_item(db_session, name="SrcId")
        await db_session.commit()
        src_id = "01ABCDEFGHJKMNPQRSTVWXYZ00"
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "url": "https://example.com/page",
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


class TestIssue188IsActive:
    """#188 — provision-paused on create + pause/resume via patch, decoupled from archive."""

    async def test_create_defaults_active(self, client, db_session, info_client):
        """A WatchedItem created without is_active is active."""
        item = await make_info_item(db_session, name="ActiveDefault")
        await db_session.commit()
        response = await client.post("/api/v1/watched-items", json=_create_body(item.info_item_id))
        assert response.status_code == 201, response.text
        assert response.json()["is_active"] is True

    async def test_create_paused(self, client, db_session, info_client):
        """is_active=False provisions a paused (not archived) WatchedItem."""
        item = await make_info_item(db_session, name="Paused")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items", json=_create_body(item.info_item_id, is_active=False)
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["is_active"] is False
        # Paused is NOT archived.
        assert body["archived_at"] is None

    async def test_create_paused_info_item_linked(self, client, db_session, info_client):
        """is_active=False on the InfoItem-linked path provisions a paused WatchedItem."""
        item = await make_info_item(db_session, name="PausedLinked")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "url": "https://example.com/page",
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
                "is_active": False,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["is_active"] is False

    async def test_patch_pause(self, client, db_session):
        """PATCH is_active=False pauses without touching archived_at."""
        wi = await _make_watched_item(db_session, name="ToPause")
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"is_active": False},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["is_active"] is False
        assert body["archived_at"] is None

    async def test_patch_resume(self, client, db_session):
        """PATCH is_active=True resumes a paused WatchedItem."""
        wi = await _make_watched_item(db_session, name="ToResume", is_active=False)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"is_active": True},
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is True

    async def test_patch_rejects_explicit_null_is_active(self, client, db_session):
        """PATCH with null is_active returns 422 (NOT NULL column)."""
        wi = await _make_watched_item(db_session, name="NullActive")
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"is_active": None},
        )
        assert response.status_code == 422

    async def test_patch_is_active_on_archived_returns_409(self, client, db_session):
        """#188 CR-1: PATCH is_active on an archived item is rejected — use restore."""
        from datetime import UTC, datetime

        wi = await _make_watched_item(
            db_session, name="ArchivedResume", archived_at=datetime.now(UTC), is_active=False
        )
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"is_active": True},
        )
        assert response.status_code == 409
        assert "archived" in response.json()["detail"].lower()
        # State unchanged.
        await db_session.refresh(wi)
        assert wi.is_active is False
        assert wi.archived_at is not None

    async def test_patch_other_fields_on_archived_still_works(self, client, db_session):
        """The archived guard fires only for is_active — other fields stay editable."""
        from datetime import UTC, datetime

        wi = await _make_watched_item(
            db_session, name="ArchivedRename", archived_at=datetime.now(UTC), is_active=False
        )
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"name": "Renamed While Archived"},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed While Archived"

    async def test_check_now_409_when_paused(self, client, db_session):
        """check-now on a paused (not archived) item returns 409, not a silent no-op."""
        wi = await _make_watched_item(db_session, name="PausedCheckNow", is_active=False)
        wi.effective_url = "https://example.com"
        await db_session.commit()
        response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")
        assert response.status_code == 409
        assert "paused" in response.json()["detail"].lower()


class TestIssue189PauseResumeAuditEvents:
    """#189 — dedicated pause/resume audit events, split from the generic UPDATED."""

    async def _events(self, db_session, wi_id, event_type):
        rows = (
            (await db_session.execute(select(AuditLog).where(AuditLog.event_type == event_type)))
            .scalars()
            .all()
        )
        return [r for r in rows if r.payload.get("watched_item_id") == str(wi_id)]

    async def test_pause_emits_paused_not_updated(self, client, db_session):
        """PATCH is_active=False on an active item emits PAUSED, not UPDATED."""
        wi = await _make_watched_item(db_session, name="P", is_active=True)
        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={"is_active": False})
        assert response.status_code == 200
        paused = await self._events(db_session, wi.id, EventType.WATCHED_ITEM_PAUSED)
        updated = await self._events(db_session, wi.id, EventType.WATCHED_ITEM_UPDATED)
        assert len(paused) == 1
        assert paused[0].payload["source"] == "api"
        assert updated == []

    async def test_resume_emits_resumed_not_updated(self, client, db_session):
        """PATCH is_active=True on a paused item emits RESUMED, not UPDATED."""
        wi = await _make_watched_item(db_session, name="R", is_active=False)
        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={"is_active": True})
        assert response.status_code == 200
        resumed = await self._events(db_session, wi.id, EventType.WATCHED_ITEM_RESUMED)
        updated = await self._events(db_session, wi.id, EventType.WATCHED_ITEM_UPDATED)
        assert len(resumed) == 1
        assert updated == []

    async def test_noop_is_active_emits_nothing(self, client, db_session):
        """PATCH is_active=True on an already-active item emits no audit event."""
        wi = await _make_watched_item(db_session, name="N", is_active=True)
        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={"is_active": True})
        assert response.status_code == 200
        paused = await self._events(db_session, wi.id, EventType.WATCHED_ITEM_PAUSED)
        resumed = await self._events(db_session, wi.id, EventType.WATCHED_ITEM_RESUMED)
        updated = await self._events(db_session, wi.id, EventType.WATCHED_ITEM_UPDATED)
        assert paused == [] and resumed == [] and updated == []

    async def test_mixed_patch_emits_both_events(self, client, db_session):
        """PATCH is_active + name emits PAUSED for the transition and UPDATED for the rest."""
        wi = await _make_watched_item(db_session, name="M", is_active=True)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"is_active": False, "name": "Renamed"},
        )
        assert response.status_code == 200
        paused = await self._events(db_session, wi.id, EventType.WATCHED_ITEM_PAUSED)
        updated = await self._events(db_session, wi.id, EventType.WATCHED_ITEM_UPDATED)
        assert len(paused) == 1
        assert len(updated) == 1
        # is_active is carried by the dedicated event, not the generic one.
        assert updated[0].payload["updated_fields"] == ["name"]

    async def test_non_active_patch_unchanged(self, client, db_session):
        """PATCH of non-activation fields still emits a single UPDATED event."""
        wi = await _make_watched_item(db_session, name="U", is_active=True)
        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={"name": "OnlyName"})
        assert response.status_code == 200
        updated = await self._events(db_session, wi.id, EventType.WATCHED_ITEM_UPDATED)
        assert len(updated) == 1
        assert updated[0].payload["updated_fields"] == ["name"]


class TestListFilterByArchiverInfoItemId:
    async def test_filter_returns_matching_item(self, client, db_session):
        """?archiver_info_item_id= returns only the WatchedItem with that ULID."""
        from src.core.models.watched_item import WatchedItem

        item = await make_info_item(db_session, name="Filtered")
        wi = WatchedItem(
            archiver_info_source_id=str(ULID()),
            archiver_info_item_id=item.info_item_id,
            name="Match",
        )
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


class TestPatchDerivesDomainName:
    """#196 — PATCH effective_url must derive domain_name + upsert Domain + suspension."""

    async def test_patch_effective_url_derives_domain_name(self, client, db_session):
        """Setting effective_url via PATCH derives domain_name from its hostname."""
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"effective_url": "https://patched.example/some/path"},
        )
        assert response.status_code == 200
        assert response.json()["domain_name"] == "patched.example"

    async def test_patch_effective_url_upserts_domain(self, client, db_session):
        """PATCH effective_url creates the Domain row if it does not exist."""
        from src.core.models.domain import Domain

        wi = await _make_watched_item(db_session)
        await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"effective_url": "https://fresh-patch-domain.example/x"},
        )
        domain = (
            await db_session.execute(
                select(Domain).where(Domain.name == "fresh-patch-domain.example")
            )
        ).scalar_one_or_none()
        assert domain is not None

    async def test_patch_onto_inactive_domain_sets_domain_suspended(self, client, db_session):
        """PATCH onto an already-inactive domain marks the item domain_suspended."""
        from src.core.models.domain import Domain

        db_session.add(Domain(name="inactive-patch.example", is_active=False))
        wi = await _make_watched_item(db_session)
        await db_session.commit()
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"effective_url": "https://inactive-patch.example/p"},
        )
        assert response.status_code == 200
        assert response.json()["domain_suspended"] is True

    async def test_patch_onto_active_domain_clears_stale_suspension(self, client, db_session):
        """PATCH onto a healthy domain clears a stale domain_suspended=True."""
        wi = await _make_watched_item(db_session, domain_suspended=True)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"effective_url": "https://healthy-patch.example/p"},
        )
        assert response.status_code == 200
        assert response.json()["domain_suspended"] is False

    async def test_patch_effective_url_audits_domain_name(self, client, db_session):
        """The UPDATED audit reflects domain_name alongside effective_url."""
        wi = await _make_watched_item(db_session)
        await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"effective_url": "https://audited-patch.example/p"},
        )
        events = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_UPDATED)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        fields = events[0].payload["updated_fields"]
        assert "effective_url" in fields
        assert "domain_name" in fields

    async def test_patch_url_succession_moves_to_new_domain(self, client, db_session):
        """#196 Finding 3: PATCH effective_url on an item that already has a domain_name
        moves it to the new host — exercises the autoflush path with an existing valid FK
        (URL succession, the realistic Archiver scenario)."""
        from src.core.models.domain import Domain

        db_session.add(Domain(name="old-succession.example"))
        await db_session.flush()
        wi = await _make_watched_item(
            db_session,
            domain_name="old-succession.example",
            effective_url="https://old-succession.example/p",
        )
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"effective_url": "https://new-succession.example/p"},
        )
        assert response.status_code == 200
        assert response.json()["domain_name"] == "new-succession.example"
        new_domain = (
            await db_session.execute(select(Domain).where(Domain.name == "new-succession.example"))
        ).scalar_one_or_none()
        assert new_domain is not None

    async def test_patch_without_effective_url_leaves_domain_name(self, client, db_session):
        """A PATCH that does not touch effective_url must not clobber domain_name."""
        from src.core.models.domain import Domain

        db_session.add(Domain(name="kept.example"))
        await db_session.flush()
        wi = await _make_watched_item(db_session, domain_name="kept.example")
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"description": "just a description change"},
        )
        assert response.status_code == 200
        assert response.json()["domain_name"] == "kept.example"


class TestCreateInfoItemLinkedDomainDerivation:
    """#196 — InfoItem-linked create with a url must derive domain_name + suspension."""

    async def test_infoitem_linked_create_with_url_derives_domain_name(
        self, client, db_session, info_client
    ):
        item = await make_info_item(db_session, name="LinkedWithUrl")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
                "url": "https://linked-create.example/page",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["domain_name"] == "linked-create.example"

    async def test_infoitem_linked_create_with_url_upserts_domain(
        self, client, db_session, info_client
    ):
        from src.core.models.domain import Domain

        item = await make_info_item(db_session, name="LinkedUpsert")
        await db_session.commit()
        await client.post(
            "/api/v1/watched-items",
            json={
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
                "url": "https://linked-upsert.example/page",
            },
        )
        domain = (
            await db_session.execute(select(Domain).where(Domain.name == "linked-upsert.example"))
        ).scalar_one_or_none()
        assert domain is not None

    async def test_infoitem_linked_create_with_url_on_inactive_domain_suspends(
        self, client, db_session, info_client
    ):
        from src.core.models.domain import Domain

        db_session.add(Domain(name="linked-inactive.example", is_active=False))
        item = await make_info_item(db_session, name="LinkedInactive")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
                "url": "https://linked-inactive.example/page",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["domain_suspended"] is True

    async def test_create_denormalizes_domain_cadence_onto_item(
        self, client, db_session, info_client
    ):
        """#205: creating an item on a domain with a cadence copies it onto the item."""
        from ulid import ULID

        from src.core.models.domain import Domain
        from src.core.models.watched_item import WatchedItem

        db_session.add(
            Domain(name="cadence-create.example", default_schedule_config={"interval": "7d"})
        )
        item = await make_info_item(db_session, name="LinkedCadence")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
                "url": "https://cadence-create.example/page",
            },
        )
        assert response.status_code == 201, response.text
        wi = (
            await db_session.execute(
                select(WatchedItem).where(WatchedItem.id == ULID.from_str(response.json()["id"]))
            )
        ).scalar_one()
        assert wi.domain_default_schedule_config == {"interval": "7d"}


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

        # Not reachable through create since #251 (url is required); the guard
        # still covers a row whose URL was emptied out of band.
        wi = WatchedItem(
            archiver_info_item_id=ULID(),
            archiver_info_source_id=str(ULID()),
            name="NoUrl",
            effective_url="",
        )
        db_session.add(wi)
        await db_session.commit()

        response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")
        assert response.status_code == 422
        assert "url" in response.json()["detail"].lower()

    async def test_409_when_a_command_is_already_in_flight(self, client, db_session):
        """CR-16: the issue path's open-command gate is a pre-flight too.

        Post-cutover this is the *common* silent no-op: without the guard the
        operator gets 202 plus a check_requested audit row while the task
        short-circuits on the gate and nothing reaches the origin.
        """
        from datetime import UTC, datetime

        from src.core.fetch_commands import create_fetch_command

        wi = await _make_watched_item(db_session, name="InFlight")
        wi.effective_url = "https://example.com"
        await create_fetch_command(db_session, wi, now=datetime.now(UTC))
        await db_session.commit()

        response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")

        assert response.status_code == 409
        detail = response.json()["detail"].lower()
        assert "in flight" in detail
        # CR-25: "already in flight" alone leaves the operator unable to tell a
        # two-second wait from a stall — the message must say when it clears.
        assert "issued" in detail and "s ago" in detail
        assert "1800s" in detail  # the reaper timeout, quoted from one place

    async def test_409_when_domain_suspended(self, client, db_session):
        """CR-16: parity with pause — the task skips a suspended item too."""
        wi = await _make_watched_item(db_session, name="Suspended")
        wi.effective_url = "https://example.com"
        wi.domain_suspended = True
        await db_session.commit()

        response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")

        assert response.status_code == 409
        assert "suspended" in response.json()["detail"].lower()

    async def test_202_once_the_command_settles(self, client, db_session):
        """A settled (non-open) command must not keep blocking check-now."""
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock, patch

        from src.core.fetch_commands import create_fetch_command
        from src.core.models.fetch_command import FetchCommandStatus

        wi = await _make_watched_item(db_session, name="Settled")
        wi.effective_url = "https://example.com"
        row = await create_fetch_command(db_session, wi, now=datetime.now(UTC))
        row.status = FetchCommandStatus.SUCCEEDED
        await db_session.commit()

        with patch("src.api.routes.watched_items.check_watched_item") as mock_task:
            mock_task.configure.return_value.defer_async = AsyncMock()
            response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")

        assert response.status_code == 202

    async def test_check_now_returns_202(self, client, db_session):
        """#187: check-now on an active WatchedItem returns 202 Accepted."""
        from unittest.mock import AsyncMock, patch

        wi = await _make_watched_item(db_session, name="CheckNow202")
        wi.effective_url = "https://example.com"
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


class TestAsyncCreate:
    """#241 step 3 / #251: create never touches an origin — Archiver owns the URL."""

    async def test_create_stores_the_url_without_probing(self, client, db_session, info_client):
        from src.core.models.watched_item import WatchedItem, WatchHealthStatus

        item = await make_info_item(db_session, name="AsyncCreate")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(item.info_item_id, url="https://async.example/page"),
        )
        assert response.status_code == 201, response.text
        data = response.json()
        # Stored verbatim: no probe, no redirect resolution at create time.
        assert data["effective_url"] == "https://async.example/page"
        assert data["domain_name"] == "async.example"

        wi = await db_session.get(WatchedItem, ULID.from_str(data["id"]))
        # UNKNOWN, not PROBING: Archiver is authoritative for the URL, so a
        # steady-state redirect stays audit-only rather than rewriting it.
        assert wi.health_status == WatchHealthStatus.UNKNOWN

    async def test_create_rejects_invalid_url_syntactically(self, client, db_session, info_client):
        # The API's HttpUrlStr schema rejects this before the route runs; the
        # route-level ValueError handler (CR-3) is the same guard for the
        # dashboard Form paths, covered in tests/core/test_watched_items.py.
        item = await make_info_item(db_session, name="BadUrl")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items", json=_create_body(item.info_item_id, url="not a url")
        )
        assert response.status_code == 422


class TestCreateRequiresArchiverLinks:
    """#251: bare-URL WatchedItems are rolled back — all three links are required."""

    async def test_missing_archiver_info_item_id_returns_422(self, client):
        body = _create_body(ULID())
        del body["archiver_info_item_id"]
        response = await client.post("/api/v1/watched-items", json=body)
        assert response.status_code == 422

    async def test_missing_archiver_info_source_id_returns_422(self, client):
        body = _create_body(ULID())
        del body["archiver_info_source_id"]
        response = await client.post("/api/v1/watched-items", json=body)
        assert response.status_code == 422

    async def test_missing_url_returns_422(self, client):
        body = _create_body(ULID())
        del body["url"]
        response = await client.post("/api/v1/watched-items", json=body)
        assert response.status_code == 422

    async def test_malformed_archiver_info_source_id_returns_422(self, client):
        """CR-2: a malformed ULID fails at the boundary, not later against a
        real captured revision when the drain posts it to Archiver."""
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(ULID(), archiver_info_source_id="not-a-ulid"),
        )
        assert response.status_code == 422

    async def test_patch_rejects_null_archiver_info_source_id(self, client, db_session):
        """The column is NOT NULL — clearing it must fail at the schema, not the DB."""
        wi = await _make_watched_item(db_session, name="KeepSourceId")
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}", json={"archiver_info_source_id": None}
        )
        assert response.status_code == 422
