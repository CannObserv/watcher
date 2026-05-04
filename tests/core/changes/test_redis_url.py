"""REDIS_URL resolution tests."""

from src.core.changes.redis_url import get_redis_url


def test_default_when_unset(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert get_redis_url() == "redis://localhost:6379/0"


def test_uses_env_var_when_set(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://example.invalid:6379/3")
    assert get_redis_url() == "redis://example.invalid:6379/3"
