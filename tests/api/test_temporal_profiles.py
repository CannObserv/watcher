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
