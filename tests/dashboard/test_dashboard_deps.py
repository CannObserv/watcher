"""Unit tests for dashboard auth dependency."""

import hashlib

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient


class TestGetDashboardUser:
    def test_missing_user_id_raises_307(self):
        from src.dashboard.deps import get_dashboard_user

        app = FastAPI()

        @app.get("/protected")
        async def protected(user=pytest.importorskip("fastapi").Depends(get_dashboard_user)):
            return {"ok": True}

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/protected", follow_redirects=False)
        assert r.status_code == 307
        assert "/__exe.dev/login" in r.headers["location"]

    def test_missing_email_raises_307(self):
        from src.dashboard.deps import get_dashboard_user

        app = FastAPI()

        @app.get("/protected")
        async def protected(user=pytest.importorskip("fastapi").Depends(get_dashboard_user)):
            return {"ok": True}

        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/protected", headers={"X-ExeDev-UserID": "usr_1"}, follow_redirects=False)
        assert r.status_code == 307


class TestGenerateApiKey:
    def test_format(self):
        from src.dashboard.deps import generate_api_key

        raw_key, key_hash, key_prefix = generate_api_key()
        assert raw_key.startswith("co_")
        assert len(raw_key) == 35  # "co_" (3) + 32 hex chars
        assert len(key_hash) == 64  # SHA-256 hex
        assert key_prefix == raw_key[:8]

    def test_is_random(self):
        from src.dashboard.deps import generate_api_key

        r1, _, _ = generate_api_key()
        r2, _, _ = generate_api_key()
        assert r1 != r2

    def test_hash_matches(self):
        from src.dashboard.deps import generate_api_key

        raw_key, key_hash, _ = generate_api_key()
        assert key_hash == hashlib.sha256(raw_key.encode()).hexdigest()
