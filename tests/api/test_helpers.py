"""Unit tests for shared route helpers."""

import pytest
from fastapi import HTTPException
from ulid import ULID

from src.api.routes.helpers import parse_filter_ulid, parse_ulid


class TestParseUlid:
    def test_valid_ulid_returns_ulid(self):
        value = str(ULID())
        result = parse_ulid(value)
        assert isinstance(result, ULID)

    def test_invalid_ulid_raises_404(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_ulid("not-a-ulid", "Watch")
        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail


class TestParseFilterUlid:
    def test_valid_ulid_returns_ulid(self):
        value = str(ULID())
        result = parse_filter_ulid(value, "Watch")
        assert isinstance(result, ULID)

    def test_invalid_ulid_raises_400(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_filter_ulid("not-a-ulid", "Watch")
        assert exc_info.value.status_code == 400

    def test_invalid_ulid_detail_mentions_field(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_filter_ulid("not-a-ulid", "watch_id")
        assert "watch_id" in exc_info.value.detail
