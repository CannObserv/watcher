"""Tests for Watch Pydantic schemas."""

import pytest
from pydantic import ValidationError

from src.api.schemas.watch import WatchCreate, WatchUpdate


class TestWatchCreate:
    def test_valid_watch_create(self):
        data = WatchCreate(
            name="Test Watch",
            url="https://example.com/page",
            content_type="html",
        )
        assert data.name == "Test Watch"
        assert data.url == "https://example.com/page"
        assert data.content_type == "html"
        assert data.fetch_config == {}
        assert data.schedule_config == {}

    def test_watch_create_requires_name(self):
        with pytest.raises(ValidationError):
            WatchCreate(url="https://example.com", content_type="html")

    def test_watch_create_requires_url(self):
        with pytest.raises(ValidationError):
            WatchCreate(name="Test", content_type="html")

    def test_watch_create_validates_content_type(self):
        with pytest.raises(ValidationError):
            WatchCreate(name="Test", url="https://example.com", content_type="invalid")

    def test_watch_create_with_configs(self):
        data = WatchCreate(
            name="PDF Watch",
            url="https://example.com/report.pdf",
            content_type="pdf",
            fetch_config={"timeout": 30},
            schedule_config={"interval": "6h"},
        )
        assert data.fetch_config == {"timeout": 30}
        assert data.schedule_config == {"interval": "6h"}


class TestWatchUpdate:
    def test_update_partial(self):
        data = WatchUpdate(name="New Name")
        assert data.name == "New Name"
        assert data.url is None
        assert data.is_active is None

    def test_update_all_fields(self):
        data = WatchUpdate(
            name="Updated",
            url="https://new.example.com",
            content_type="pdf",
            fetch_config={"selectors": ["#main"]},
            schedule_config={"interval": "1h"},
            is_active=False,
        )
        assert data.is_active is False

    def test_update_empty_is_valid(self):
        data = WatchUpdate()
        assert data.name is None
