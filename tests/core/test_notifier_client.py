"""Tests for src.core.notifier_client — config factory and idempotency key builder."""

from datetime import UTC, datetime

import pytest
from notifier_client import NotifierClient
from ulid import ULID

from src.core.notifications.events import WatchEvent, WatchEventType
from src.core.notifier_client.client import build_idempotency_key, get_notifier_client


def _make_event(event_type=WatchEventType.CHANGE_DETECTED, *, metadata=None):
    return WatchEvent(
        event_type=event_type,
        watched_item_id=str(ULID()),
        item_name="Test Watch",
        item_url="https://example.com",
        occurred_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        metadata=metadata or {},
    )


class TestGetNotifierClient:
    def test_raises_when_base_url_missing(self, monkeypatch):
        monkeypatch.delenv("NOTIFIER_BASE_URL", raising=False)
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        with pytest.raises(RuntimeError, match="NOTIFIER_BASE_URL"):
            get_notifier_client()

    def test_raises_when_api_key_missing(self, monkeypatch):
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.delenv("NOTIFIER_API_KEY", raising=False)

        with pytest.raises(RuntimeError, match="NOTIFIER_API_KEY"):
            get_notifier_client()

    def test_returns_client_when_env_set(self, monkeypatch):
        monkeypatch.setenv("NOTIFIER_BASE_URL", "http://localhost:9000")
        monkeypatch.setenv("NOTIFIER_API_KEY", "nk_test")

        client = get_notifier_client()
        assert isinstance(client, NotifierClient)


class TestBuildIdempotencyKey:
    def test_change_detected_uses_change_id(self):
        change_id = str(ULID())
        event = _make_event(
            WatchEventType.CHANGE_DETECTED,
            metadata={"change_id": change_id},
        )
        source_id = str(ULID())

        key = build_idempotency_key(event, source_id)
        assert key == f"watcher:change_detected:{source_id}:{change_id}"

    def test_non_change_event_uses_watch_id_and_timestamp(self):
        event = _make_event(WatchEventType.WATCH_CREATED)
        source_id = str(ULID())

        key = build_idempotency_key(event, source_id)
        occurred_ms = int(event.occurred_at.timestamp() * 1000)
        assert key == f"watcher:watch_created:{source_id}:{event.watched_item_id}:{occurred_ms}"

    def test_key_is_stable_for_same_inputs(self):
        change_id = str(ULID())
        event = _make_event(
            WatchEventType.CHANGE_DETECTED,
            metadata={"change_id": change_id},
        )
        source_id = str(ULID())

        assert build_idempotency_key(event, source_id) == build_idempotency_key(event, source_id)

    def test_different_sources_produce_different_keys(self):
        change_id = str(ULID())
        event = _make_event(
            WatchEventType.CHANGE_DETECTED,
            metadata={"change_id": change_id},
        )

        key_a = build_idempotency_key(event, "source-aaa")
        key_b = build_idempotency_key(event, "source-bbb")
        assert key_a != key_b
