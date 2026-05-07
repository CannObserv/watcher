"""Tests for shared Pydantic validators."""

import pytest

from src.api.schemas.validators import validate_event_list


class TestValidateEventList:
    def test_valid_events_returned_unchanged(self):
        result = validate_event_list(["change_detected", "watch_error"])
        assert result == ["change_detected", "watch_error"]

    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="At least one event"):
            validate_event_list([])

    def test_single_valid_event(self):
        assert validate_event_list(["watch_created"]) == ["watch_created"]

    def test_unknown_event_raises(self):
        with pytest.raises(ValueError, match="Unknown event type"):
            validate_event_list(["not_a_real_event"])

    def test_mixed_valid_and_invalid_raises(self):
        with pytest.raises(ValueError, match="not_a_real_event"):
            validate_event_list(["change_detected", "not_a_real_event"])

    def test_error_message_names_invalid_events(self):
        with pytest.raises(ValueError, match=r"\['bad_event'\]"):
            validate_event_list(["bad_event"])
