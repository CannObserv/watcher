"""Integration tests for temporal profile API endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestCreateProfile:
    async def test_create_event_profile(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={
                "name": "Profiled Watch",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = watch_resp.json()["id"]
        response = await client.post(
            f"/api/watches/{watch_id}/profiles",
            json={
                "profile_type": "event",
                "reference_date": "2026-04-15",
                "rules": [
                    {"days_before": 30, "interval": "6h"},
                    {"days_before": 7, "interval": "1h"},
                ],
                "post_action": "reduce_frequency",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["profile_type"] == "event"
        assert data["reference_date"] == "2026-04-15"
        assert len(data["rules"]) == 2

    async def test_create_profile_invalid_watch(self, client):
        response = await client.post(
            "/api/watches/00000000000000000000000000/profiles",
            json={
                "profile_type": "event",
                "reference_date": "2026-04-15",
                "rules": [],
                "post_action": "deactivate",
            },
        )
        assert response.status_code == 404


class TestListProfiles:
    async def test_list_profiles_for_watch(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={
                "name": "Multi-Profile Watch",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = watch_resp.json()["id"]
        await client.post(
            f"/api/watches/{watch_id}/profiles",
            json={
                "profile_type": "event",
                "reference_date": "2026-04-15",
                "rules": [{"days_before": 7, "interval": "1h"}],
                "post_action": "deactivate",
            },
        )
        await client.post(
            f"/api/watches/{watch_id}/profiles",
            json={
                "profile_type": "seasonal",
                "date_range_start": "2026-01-15",
                "date_range_end": "2026-06-30",
                "rules": [{"days_before": 0, "interval": "2h"}],
                "post_action": "reduce_frequency",
            },
        )
        response = await client.get(f"/api/watches/{watch_id}/profiles")
        assert response.status_code == 200
        assert len(response.json()) == 2


class TestUpdateProfile:
    async def test_update_rules(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={"name": "Update Watch", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        create_resp = await client.post(
            f"/api/watches/{watch_id}/profiles",
            json={
                "profile_type": "event",
                "reference_date": "2026-04-15",
                "rules": [{"days_before": 30, "interval": "6h"}],
                "post_action": "reduce_frequency",
            },
        )
        profile_id = create_resp.json()["id"]
        response = await client.patch(
            f"/api/watches/{watch_id}/profiles/{profile_id}",
            json={
                "rules": [
                    {"days_before": 7, "interval": "1h"},
                    {"days_before": 1, "interval": "30m"},
                ],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["rules"]) == 2
        assert data["rules"][0]["days_before"] == 7

    async def test_update_is_active(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={"name": "Deactivate Watch", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        create_resp = await client.post(
            f"/api/watches/{watch_id}/profiles",
            json={
                "profile_type": "deadline",
                "reference_date": "2026-05-01",
                "rules": [],
                "post_action": "deactivate",
            },
        )
        profile_id = create_resp.json()["id"]
        response = await client.patch(
            f"/api/watches/{watch_id}/profiles/{profile_id}",
            json={"is_active": False},
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False

    async def test_update_post_action(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={"name": "Action Watch", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        create_resp = await client.post(
            f"/api/watches/{watch_id}/profiles",
            json={
                "profile_type": "event",
                "reference_date": "2026-04-15",
                "rules": [],
                "post_action": "reduce_frequency",
            },
        )
        profile_id = create_resp.json()["id"]
        response = await client.patch(
            f"/api/watches/{watch_id}/profiles/{profile_id}",
            json={"post_action": "archive"},
        )
        assert response.status_code == 200
        assert response.json()["post_action"] == "archive"

    async def test_update_creates_audit_log(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={"name": "Audit Watch", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        create_resp = await client.post(
            f"/api/watches/{watch_id}/profiles",
            json={
                "profile_type": "event",
                "reference_date": "2026-04-15",
                "rules": [],
                "post_action": "deactivate",
            },
        )
        profile_id = create_resp.json()["id"]
        await client.patch(
            f"/api/watches/{watch_id}/profiles/{profile_id}",
            json={"is_active": False},
        )
        response = await client.get("/api/audit", params={"event_type": "profile.updated"})
        events = response.json()
        assert len(events) >= 1
        assert events[0]["payload"]["profile_id"] == profile_id
        assert events[0]["payload"]["updated_fields"] == ["is_active"]

    async def test_update_nonexistent_profile(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={"name": "Missing Watch", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        response = await client.patch(
            f"/api/watches/{watch_id}/profiles/00000000000000000000000000",
            json={"is_active": False},
        )
        assert response.status_code == 404

    async def test_update_empty_body(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={"name": "Empty Watch", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        create_resp = await client.post(
            f"/api/watches/{watch_id}/profiles",
            json={
                "profile_type": "event",
                "reference_date": "2026-04-15",
                "rules": [],
                "post_action": "deactivate",
            },
        )
        profile_id = create_resp.json()["id"]
        response = await client.patch(
            f"/api/watches/{watch_id}/profiles/{profile_id}",
            json={},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is True
        assert data["post_action"] == "deactivate"
        assert data["rules"] == []

    async def test_update_multiple_fields(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={"name": "Multi Watch", "url": "https://example.com", "content_type": "html"},
        )
        watch_id = watch_resp.json()["id"]
        create_resp = await client.post(
            f"/api/watches/{watch_id}/profiles",
            json={
                "profile_type": "event",
                "reference_date": "2026-04-15",
                "rules": [{"days_before": 30, "interval": "6h"}],
                "post_action": "reduce_frequency",
            },
        )
        profile_id = create_resp.json()["id"]
        response = await client.patch(
            f"/api/watches/{watch_id}/profiles/{profile_id}",
            json={"is_active": False, "post_action": "archive"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is False
        assert data["post_action"] == "archive"
        # Rules unchanged
        assert len(data["rules"]) == 1
        # Audit records both fields
        audit_resp = await client.get(
            "/api/audit",
            params={"event_type": "profile.updated"},
        )
        events = audit_resp.json()
        assert len(events) >= 1
        assert sorted(events[0]["payload"]["updated_fields"]) == [
            "is_active",
            "post_action",
        ]


class TestDeleteProfile:
    async def test_delete_profile(self, client):
        watch_resp = await client.post(
            "/api/watches",
            json={
                "name": "Delete Profile Watch",
                "url": "https://example.com",
                "content_type": "html",
            },
        )
        watch_id = watch_resp.json()["id"]
        create_resp = await client.post(
            f"/api/watches/{watch_id}/profiles",
            json={
                "profile_type": "event",
                "reference_date": "2026-04-15",
                "rules": [],
                "post_action": "deactivate",
            },
        )
        profile_id = create_resp.json()["id"]
        response = await client.delete(f"/api/watches/{watch_id}/profiles/{profile_id}")
        assert response.status_code == 204
