"""Integration tests for WatchedItem API endpoints."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from ulid import ULID

from src.core.models.audit_log import AuditLog, EventType
from src.core.models.watched_item import WatchedItem
from tests.conftest import make_info_item, make_watched_item

pytestmark = pytest.mark.integration


def _create_body(info_item_id, **overrides):
    """A minimal valid ``POST /api/v1/watched-items`` body.

    #251: the InfoItem link, its URL, and the InfoSource link are all required —
    there is no URL-only creation path. #260 adds non-empty ``source_specs``:
    Archiver always has them at provisioning time, and a spec-less item has no
    defined extraction.
    """
    body = {
        "archiver_info_item_id": str(info_item_id),
        "url": "https://example.com/page",
        "archiver_info_source_id": str(ULID()),
        "source_specs": [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}],
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
        # Removed in #253 — Archiver allocates the registry id on its side of
        # content.revisions and never reports it back, so the field could only
        # ever be null. Breaking, and deliberate: a permanently-null field reads
        # as "not synced yet".
        assert "archiver_revision_id" not in data

    async def test_revisions_404_unknown_watched_item(self, client):
        from ulid import ULID

        response = await client.get(f"/api/v1/watched-items/{ULID()}/revisions")
        assert response.status_code == 404


class TestCreateWatchedItem:
    async def test_creates_with_url_derived_name_fallback(self, client, db_session):
        item = await make_info_item(db_session, name="Source Item")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(item.info_item_id),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["archiver_info_item_id"] == str(item.info_item_id)
        # #254: the name falls back to the URL, not the InfoItem's name — the SDK
        # call that could read the latter is gone, and the announcement that
        # replaced it carries no name field either.
        assert body["name"] == "example.com/page"
        assert body["default_schedule_config"] is None
        assert body["archived_at"] is None

    async def test_omitted_default_schedule_config_persists_sql_null(self, client, db_session):
        """Omitting default_schedule_config stores SQL NULL, not JSONB 'null' (#198).

        Both representations read back as Python None, so the route response is no
        guard — assert the on-disk value matches ``IS NULL``.
        """
        item = await make_info_item(db_session, name="NullCfg")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(item.info_item_id),
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

    async def test_uses_supplied_name(self, client, db_session):
        item = await make_info_item(db_session, name="Source")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(
                item.info_item_id,
                name="Overridden",
                default_schedule_config={"interval": "10m"},
                default_tags=["regulatory"],
            ),
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Overridden"
        assert body["default_schedule_config"] == {"interval": "10m"}
        assert body["default_tags"] == ["regulatory"]

    async def test_duplicate_archiver_info_item_id_returns_409(self, client, db_session):
        item = await make_info_item(db_session, name="X")
        await db_session.commit()
        r1 = await client.post("/api/v1/watched-items", json=_create_body(item.info_item_id))
        assert r1.status_code == 201
        r2 = await client.post("/api/v1/watched-items", json=_create_body(item.info_item_id))
        assert r2.status_code == 409
        assert "already" in r2.json()["detail"].lower()

    async def test_an_unknown_info_item_is_accepted_not_rejected(self, client):
        """#254: the SDK validation is gone with the SDK, and nothing replaced it.

        Validating against the local reconciled view cannot work on a create —
        Archiver provisions and POSTs immediately, well inside the snapshot
        period, so every legitimate create would race its own announcement. The
        cost is a row for a nonexistent InfoItem lingering, since absence is not
        revocation on `info.registry`; the fix is retiring this route once
        archiver#141's producer is live, not re-adding an HTTP call.
        """
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body("01ZZZZZZZZZZZZZZZZZZZZZZZZ"),
        )
        assert response.status_code == 201, response.text

    async def test_emits_audit_event(self, client, db_session):
        item = await make_info_item(db_session, name="A")
        await db_session.commit()
        await client.post("/api/v1/watched-items", json=_create_body(item.info_item_id))
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

    async def test_creates_with_url_and_source_specs(self, client, db_session):
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

    async def test_response_includes_effective_url_and_source_specs(self, client, db_session):
        """WatchedItem response always includes effective_url and source_specs."""
        item = await make_info_item(db_session, name="RespFields")
        await db_session.commit()
        specs = [{"schema_version": 1, "extraction": {"algorithm": "full_page"}}]
        response = await client.post(
            "/api/v1/watched-items",
            json={
                "url": "https://example.com/page",
                "archiver_info_source_id": str(ULID()),
                "archiver_info_item_id": str(item.info_item_id),
                "source_specs": specs,
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert "effective_url" in body
        assert "source_specs" in body
        # effective_url is the supplied (Archiver-authoritative) URL; source_specs
        # are stored as sent — since #260 there is no unseeded variant.
        assert body["effective_url"] == "https://example.com/page"
        assert body["source_specs"] == specs

    async def test_create_without_source_specs_is_422(self, client, db_session):
        """#260: the spec-less WatchedItem is unreachable through the API door.

        Archiver always has specs at "Begin Watching" time — it refuses to
        announce a source as live without them — so the "optional at create"
        affordance only ever admitted a state the pipeline had no ratified
        behaviour for.
        """
        item = await make_info_item(db_session, name="NoSpecs")
        await db_session.commit()
        body = _create_body(item.info_item_id)
        del body["source_specs"]

        response = await client.post("/api/v1/watched-items", json=body)

        assert response.status_code == 422, response.text
        assert "source_specs" in response.text

    async def test_create_with_empty_source_specs_is_422(self, client, db_session):
        """The same state spelled explicitly is refused the same way."""
        item = await make_info_item(db_session, name="EmptySpecs")
        await db_session.commit()

        response = await client.post(
            "/api/v1/watched-items", json=_create_body(item.info_item_id, source_specs=[])
        )

        assert response.status_code == 422, response.text
        assert "source_specs" in response.text

    async def test_create_on_inactive_domain_sets_domain_suspended(self, client, db_session):
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

    async def test_create_on_active_domain_not_suspended(self, client, db_session):
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

    async def test_create_stores_archiver_info_source_id(self, client, db_session):
        """archiver_info_source_id is persisted when supplied on create."""
        item = await make_info_item(db_session, name="SrcId")
        await db_session.commit()
        src_id = "01ABCDEFGHJKMNPQRSTVWXYZ00"
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(item.info_item_id, archiver_info_source_id=src_id),
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

    async def test_response_includes_last_observed_at(self, client, db_session):
        """#266: last_observed_at rides beside last_checked_at on every response.

        Additive and non-breaking — the pair is what makes freshness readable:
        last_checked_at says "we tried at T", last_observed_at says "content was
        verified current as of T" (#264, and a 304 counts too since #249).
        """
        observed = datetime(2026, 8, 15, 12, 30, tzinfo=UTC)
        wi = await _make_watched_item(db_session, name="ObservedFields", last_observed_at=observed)
        response = await client.get(f"/api/v1/watched-items/{wi.id}")
        assert response.status_code == 200
        body = response.json()
        assert "last_observed_at" in body
        assert body["last_observed_at"].startswith("2026-08-15T12:30:00")

    async def test_response_last_observed_at_null_before_first_observation(
        self, client, db_session
    ):
        """A never-observed item reports null, not an absent key."""
        wi = await _make_watched_item(db_session, name="NeverObserved")
        body = (await client.get(f"/api/v1/watched-items/{wi.id}")).json()
        assert body["last_observed_at"] is None


class TestIssue188IsActive:
    """#188 — provision-paused on create + pause/resume via patch, decoupled from archive."""

    async def test_create_defaults_active(self, client, db_session):
        """A WatchedItem created without is_active is active."""
        item = await make_info_item(db_session, name="ActiveDefault")
        await db_session.commit()
        response = await client.post("/api/v1/watched-items", json=_create_body(item.info_item_id))
        assert response.status_code == 201, response.text
        assert response.json()["is_active"] is True

    async def test_create_paused(self, client, db_session):
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

    async def test_create_paused_info_item_linked(self, client, db_session):
        """is_active=False on the InfoItem-linked path provisions a paused WatchedItem."""
        item = await make_info_item(db_session, name="PausedLinked")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(item.info_item_id, is_active=False),
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

    async def test_patch_rejects_empty_source_specs(self, client, db_session):
        """#260: PATCH cannot re-open the door the create schema closed."""
        wi = await _make_watched_item(db_session)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"source_specs": []},
        )
        assert response.status_code == 422, response.text

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

    async def test_infoitem_linked_create_with_url_derives_domain_name(self, client, db_session):
        item = await make_info_item(db_session, name="LinkedWithUrl")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(item.info_item_id, url="https://linked-create.example/page"),
        )
        assert response.status_code == 201, response.text
        assert response.json()["domain_name"] == "linked-create.example"

    async def test_infoitem_linked_create_with_url_upserts_domain(self, client, db_session):
        from src.core.models.domain import Domain

        item = await make_info_item(db_session, name="LinkedUpsert")
        await db_session.commit()
        await client.post(
            "/api/v1/watched-items",
            json=_create_body(item.info_item_id, url="https://linked-upsert.example/page"),
        )
        domain = (
            await db_session.execute(select(Domain).where(Domain.name == "linked-upsert.example"))
        ).scalar_one_or_none()
        assert domain is not None

    async def test_infoitem_linked_create_with_url_on_inactive_domain_suspends(
        self, client, db_session
    ):
        from src.core.models.domain import Domain

        db_session.add(Domain(name="linked-inactive.example", is_active=False))
        item = await make_info_item(db_session, name="LinkedInactive")
        await db_session.commit()
        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(item.info_item_id, url="https://linked-inactive.example/page"),
        )
        assert response.status_code == 201, response.text
        assert response.json()["domain_suspended"] is True

    async def test_create_denormalizes_domain_cadence_onto_item(self, client, db_session):
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
            json=_create_body(item.info_item_id, url="https://cadence-create.example/page"),
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
        from unittest.mock import patch

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
        from unittest.mock import patch

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
        from unittest.mock import patch

        wi = await _make_watched_item(db_session, name="CheckNow202")
        wi.effective_url = "https://example.com"
        await db_session.commit()

        with patch("src.api.routes.watched_items.check_watched_item") as mock_task:
            mock_task.configure.return_value.defer_async = AsyncMock()
            response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")

        assert response.status_code == 202

    async def test_202_emits_audit_log(self, client, db_session):
        """#3 fix: check-now must write a WATCHED_ITEM_CHECK_REQUESTED audit entry."""
        from unittest.mock import patch

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

    async def test_create_stores_the_url_without_probing(self, client, db_session):
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

    async def test_create_rejects_invalid_url_syntactically(self, client, db_session):
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


class TestThrottleFloorRelease:
    """CR-1 (#254): the API's half of the throttle escape hatch."""

    async def test_patching_the_schedule_config_releases_the_floor(self, client, db_session):
        wi = await make_watched_item(db_session, name="Throttled")
        wi.throttle_floor_interval = "1d"
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_schedule_config": {"interval": "30m"}},
        )

        assert response.status_code == 200, response.text
        await db_session.refresh(wi)
        assert wi.throttle_floor_interval is None

    async def test_an_unrelated_patch_leaves_the_floor_alone(self, client, db_session):
        wi = await make_watched_item(db_session, name="Throttled")
        wi.throttle_floor_interval = "1d"
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}", json={"description": "just a note"}
        )

        assert response.status_code == 200, response.text
        await db_session.refresh(wi)
        assert wi.throttle_floor_interval == "1d"


class TestDeleteRegistryOwned:
    """CR-7 (#254): deletion is not durable for a key the registry still announces.

    `info.registry` is level-triggered, so the next snapshot recreates the row —
    absence is not revocation. A 409 naming the authority beats a delete that
    silently undoes itself.
    """

    async def test_deleting_a_reconciled_item_is_refused(self, client, db_session):
        wi = await make_watched_item(db_session, name="Registry-owned")
        wi.archived_at = datetime(2026, 8, 1, tzinfo=UTC)
        wi.is_active = False
        wi.applied_generation = 4
        await db_session.commit()

        response = await client.delete(f"/api/v1/watched-items/{wi.id}")

        assert response.status_code == 409
        assert "Archiver" in response.json()["detail"]
        assert await db_session.get(WatchedItem, wi.id) is not None

    async def test_deleting_an_un_announced_item_still_works(self, client, db_session):
        """The whole population during the rollout: POST-created, never announced."""
        wi = await make_watched_item(db_session, name="Local only")
        wi.archived_at = datetime(2026, 8, 1, tzinfo=UTC)
        wi.is_active = False
        await db_session.commit()
        assert wi.applied_generation is None

        response = await client.delete(f"/api/v1/watched-items/{wi.id}")

        assert response.status_code == 204


class TestScheduleConfigValidation:
    """CR-10 (#254 round 2): `schedule_tick` iterates every WatchedItem in one
    task, so an unparseable stored interval is not one broken row — it raises out
    of `compute_next_check` and stops scheduling for the whole system. The write
    boundary is the only place that can hold that line.
    """

    async def test_patch_rejects_an_unparseable_interval(self, client, db_session):
        wi = await make_watched_item(db_session, name="Bogus")
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_schedule_config": {"interval": "every other tuesday"}},
        )

        assert response.status_code == 422
        await db_session.refresh(wi)
        assert wi.default_schedule_config is None

    async def test_create_rejects_an_unparseable_interval(self, client, db_session):
        item = await make_info_item(db_session, name="Bogus")
        await db_session.commit()

        response = await client.post(
            "/api/v1/watched-items",
            json=_create_body(item.info_item_id, default_schedule_config={"interval": "soon"}),
        )

        assert response.status_code == 422

    async def test_a_valid_interval_still_passes(self, client, db_session):
        wi = await make_watched_item(db_session, name="Fine")
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_schedule_config": {"interval": "6h"}},
        )

        assert response.status_code == 200, response.text

    async def test_an_intervalless_config_is_rejected(self, client, db_session):
        """CR-19 (#254 round 3): both cadence boundaries enforce the same rule.

        Verified against the contract history (cannobserv#324, archiver#150):
        delegation has exactly one spelling — `None`/omit — never an empty
        document left open to interpretation. The Domain boundary has rejected
        `{}` since #205; this brings the item tier into line. The resolver's
        `{}`-passes-through branch stays as defensive rendering for legacy rows.
        """
        wi = await make_watched_item(db_session, name="Empty")
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}", json={"default_schedule_config": {}}
        )

        assert response.status_code == 422
        await db_session.refresh(wi)
        assert wi.default_schedule_config is None

    async def test_a_non_string_interval_is_a_readable_422(self, client, db_session):
        """CR-18: the 422 detail must state the constraint, not leak an `re`
        internal ("expected string or bytes-like object")."""
        wi = await make_watched_item(db_session, name="IntInterval")
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_schedule_config": {"interval": 6}},
        )

        assert response.status_code == 422
        detail = str(response.json()["detail"])
        assert "must be a string" in detail
        assert "bytes-like" not in detail

    async def test_clearing_with_null_still_works(self, client, db_session):
        """`None` is the one spelling of "inherit" — it must keep passing."""
        wi = await make_watched_item(db_session, name="Clear")
        wi.default_schedule_config = {"interval": "6h"}
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}", json={"default_schedule_config": None}
        )

        assert response.status_code == 200, response.text
        await db_session.refresh(wi)
        assert wi.default_schedule_config is None

    async def test_a_hostile_row_cannot_reach_the_scheduler(self, client, db_session):
        """The consequence the validator exists to prevent, stated as a test."""
        from src.core.scheduling.cadence import compute_next_check
        from src.core.scheduling.resolution import resolved_schedule_config

        wi = await make_watched_item(db_session, name="Hostile")
        await db_session.commit()
        await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_schedule_config": {"interval": "every other tuesday"}},
        )
        await db_session.refresh(wi)

        # Whatever the route accepted, the scheduler's own call must not raise.
        compute_next_check(
            schedule_config=resolved_schedule_config(wi),
            last_checked_at=datetime(2026, 8, 1, tzinfo=UTC),
            now=datetime(2026, 8, 11, tzinfo=UTC),
        )


class TestCadencePatchAudit:
    """The cadence write is routed around the generic setattr loop (CR-12), which
    is exactly the shape that silently drops a field from the audit trail."""

    async def test_a_cadence_change_is_still_audited(self, client, db_session):
        wi = await make_watched_item(db_session, name="Audited")
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_schedule_config": {"interval": "6h"}},
        )
        assert response.status_code == 200, response.text

        rows = (
            (
                await db_session.execute(
                    select(AuditLog).where(AuditLog.event_type == EventType.WATCHED_ITEM_UPDATED)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert "default_schedule_config" in rows[0].payload["updated_fields"]

    async def test_the_cadence_still_lands_alongside_other_fields(self, client, db_session):
        wi = await make_watched_item(db_session, name="Audited")
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_schedule_config": {"interval": "6h"}, "description": "note"},
        )

        assert response.status_code == 200, response.text
        await db_session.refresh(wi)
        assert wi.default_schedule_config == {"interval": "6h"}
        assert wi.description == "note"


class TestRegistryOwnedPause:
    """#254 break-glass follow-through: item-level pause lives in exactly one
    place — Archiver's dashboard — once the announcement path is authoritative.

    Gated at cutover (2026-08-13): archiver#150's import ran, archiver#141's
    producer is live, and the delta path is verified in production. A local
    toggle on a reconciled item would silently revert within the snapshot
    period, so a 409 naming the authority beats a control that lies. Mirrors
    the DELETE guard's condition: never-announced rows (`applied_generation IS
    NULL`) keep the local toggle — the registry has no opinion to defer to.
    """

    async def test_pausing_a_reconciled_item_is_refused(self, client, db_session):
        wi = await make_watched_item(db_session, name="Registry-owned", is_active=True)
        wi.applied_generation = 2
        await db_session.commit()

        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={"is_active": False})

        assert response.status_code == 409
        assert "Archiver" in response.json()["detail"]
        await db_session.refresh(wi)
        assert wi.is_active is True

    async def test_resuming_a_reconciled_item_is_refused(self, client, db_session):
        """Resume is Archiver's call too — `active` is one field, one owner."""
        wi = await make_watched_item(db_session, name="Registry-owned", is_active=False)
        wi.applied_generation = 2
        await db_session.commit()

        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={"is_active": True})

        assert response.status_code == 409
        await db_session.refresh(wi)
        assert wi.is_active is False

    async def test_a_never_announced_item_still_toggles(self, client, db_session):
        wi = await make_watched_item(db_session, name="Local only", is_active=True)
        await db_session.commit()
        assert wi.applied_generation is None

        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={"is_active": False})

        assert response.status_code == 200, response.text
        await db_session.refresh(wi)
        assert wi.is_active is False

    async def test_a_noop_toggle_on_a_reconciled_item_is_still_refused(self, client, db_session):
        """Same posture as the archived guard: the 409 teaches where the control
        lives, and a no-op that succeeds teaches the opposite."""
        wi = await make_watched_item(db_session, name="Registry-owned", is_active=True)
        wi.applied_generation = 2
        await db_session.commit()

        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={"is_active": True})

        assert response.status_code == 409

    async def test_other_fields_still_patch_on_a_reconciled_item(self, client, db_session):
        """The guard is on `is_active` alone — Watcher-local fields stay editable."""
        wi = await make_watched_item(db_session, name="Registry-owned")
        wi.applied_generation = 2
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}", json={"description": "still mine"}
        )

        assert response.status_code == 200, response.text

    async def test_the_reconcile_itself_is_not_blocked(self, client, db_session):
        """The guard lives in `set_watched_item_active`; the reconcile writes the
        column directly and must keep doing so — it IS the authority's path."""
        from src.workers.registry_reconcile import reconcile_announcement
        from tests.workers.test_registry_reconcile import _announcement

        wi = await make_watched_item(db_session, name="Registry-owned", is_active=True)
        await db_session.commit()

        await reconcile_announcement(
            db_session,
            _announcement(wi.archiver_info_item_id, generation=3, active=False),
        )

        await db_session.refresh(wi)
        assert wi.is_active is False
        assert wi.applied_generation == 3


class TestRegistryOwnedFields:
    """CR-22 (#254 round 4): the snapshot cannot repair local drift.

    The hourly republish carries the same generation, which the `>` ordering
    guard classifies as stale — so "self-corrects at the next snapshot" covers
    missed messages only, never a local write that diverges after applying. That
    makes every locally-writable announcement-owned column a permanent-divergence
    path, and the pause guard covered only one of the five. Same carve-out as
    everywhere: never-announced rows keep all their fields.
    """

    FIELDS = (
        ("effective_url", "https://example.org/moved"),
        ("source_specs", [{"selector": "#drift"}]),
        ("archiver_info_source_id", "01ZZZZZZZZZZZZZZZZZZZZZZZZ"),
    )

    async def test_announcement_owned_fields_409_on_a_reconciled_item(self, client, db_session):
        wi = await make_watched_item(db_session, name="Registry-owned")
        wi.applied_generation = 2
        await db_session.commit()

        for field, value in self.FIELDS:
            response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={field: value})
            assert response.status_code == 409, f"{field}: {response.text}"
            assert "Archiver" in response.json()["detail"]

    async def test_the_same_fields_still_patch_on_a_never_announced_item(self, client, db_session):
        wi = await make_watched_item(db_session, name="Local only")
        await db_session.commit()

        for field, value in self.FIELDS:
            response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={field: value})
            assert response.status_code == 200, f"{field}: {response.text}"

    async def test_watcher_local_fields_still_patch_on_a_reconciled_item(self, client, db_session):
        wi = await make_watched_item(db_session, name="Registry-owned")
        wi.applied_generation = 2
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={
                "name": "Renamed locally",
                "description": "mine",
                "default_schedule_config": {"interval": "6h"},
                "default_tags": ["local"],
            },
        )
        assert response.status_code == 200, response.text


class TestRestoreOnReconciledItem:
    """CR-23: restore must not re-arm what the registry paused.

    Archive→restore was a two-step bypass of the pause guard: archive (allowed,
    Watcher-local) flips is_active False, restore flipped it True unconditionally
    — and the divergence never repaired, because the snapshot re-announcing
    active:false at the same generation is ignored as stale.
    """

    async def test_restore_clears_archived_at_but_leaves_is_active(self, client, db_session):
        wi = await make_watched_item(db_session, name="Registry-owned", is_active=True)
        wi.applied_generation = 2
        await db_session.commit()
        r = await client.post(f"/api/v1/watched-items/{wi.id}/archive")
        assert r.status_code == 200

        response = await client.post(f"/api/v1/watched-items/{wi.id}/restore")

        assert response.status_code == 200, response.text
        await db_session.refresh(wi)
        assert wi.archived_at is None
        assert wi.is_active is False  # the registry re-arms it, not restore

    async def test_restore_still_reactivates_a_never_announced_item(self, client, db_session):
        wi = await make_watched_item(db_session, name="Local only", is_active=True)
        await db_session.commit()
        await client.post(f"/api/v1/watched-items/{wi.id}/archive")

        response = await client.post(f"/api/v1/watched-items/{wi.id}/restore")

        assert response.status_code == 200, response.text
        await db_session.refresh(wi)
        assert wi.archived_at is None
        assert wi.is_active is True

    async def test_an_announcement_rearms_a_restored_item(self, client, db_session):
        """The full loop the ownership design intends: restore leaves it paused,
        Archiver's next real mutation re-arms it."""
        from src.workers.registry_reconcile import reconcile_announcement
        from tests.workers.test_registry_reconcile import _announcement

        wi = await make_watched_item(db_session, name="Registry-owned", is_active=True)
        wi.applied_generation = 2
        await db_session.commit()
        await client.post(f"/api/v1/watched-items/{wi.id}/archive")
        await client.post(f"/api/v1/watched-items/{wi.id}/restore")

        await reconcile_announcement(
            db_session, _announcement(wi.archiver_info_item_id, generation=3, active=True)
        )

        await db_session.refresh(wi)
        assert wi.is_active is True


class TestCheckNowPausedDetail:
    """CR-24: the 409 must not point at a resume control that also 409s."""

    async def test_paused_reconciled_item_names_archiver(self, client, db_session):
        wi = await make_watched_item(db_session, name="Registry-owned", is_active=False)
        wi.applied_generation = 2
        await db_session.commit()

        response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")

        assert response.status_code == 409
        assert "Archiver" in response.json()["detail"]

    async def test_paused_local_item_keeps_the_plain_detail(self, client, db_session):
        wi = await make_watched_item(db_session, name="Local only", is_active=False)
        await db_session.commit()

        response = await client.post(f"/api/v1/watched-items/{wi.id}/check-now")

        assert response.status_code == 409
        assert "Archiver" not in response.json()["detail"]


class TestPatchStatusRepublishGate:
    """watcher#264 CR-4: the PATCH defer is gated on a wire-visible change —
    a no-op PATCH must not publish a watch-status full set."""

    def _spy(self, monkeypatch) -> AsyncMock:
        import src.api.routes.watched_items as api_mod

        spy = AsyncMock()
        monkeypatch.setattr(api_mod, "defer_status_republish", spy)
        return spy

    async def test_noop_patch_does_not_defer(self, client, db_session, monkeypatch):
        wi = await _make_watched_item(db_session)
        spy = self._spy(monkeypatch)
        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={})
        assert response.status_code == 200
        assert spy.await_count == 0

    async def test_field_patch_defers(self, client, db_session, monkeypatch):
        wi = await _make_watched_item(db_session)
        spy = self._spy(monkeypatch)
        response = await client.patch(
            f"/api/v1/watched-items/{wi.id}",
            json={"default_schedule_config": {"interval": "30m"}},
        )
        assert response.status_code == 200
        assert spy.await_count == 1

    async def test_pause_patch_defers(self, client, db_session, monkeypatch):
        wi = await _make_watched_item(db_session)
        spy = self._spy(monkeypatch)
        response = await client.patch(f"/api/v1/watched-items/{wi.id}", json={"is_active": False})
        assert response.status_code == 200
        assert spy.await_count == 1
