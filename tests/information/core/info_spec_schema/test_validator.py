"""Unit tests for the InfoSpec v1 validator."""

import pytest

from src.information.core.info_spec_schema import (
    InfoSpecValidationError,
    InfoSpecValidationIssue,
    validate_info_spec,
    validate_info_spec_with_errors,
)

VALID_DOC = {
    "schema_version": 1,
    "target": {"url": "https://example.com"},
    "extraction": {"algorithm": "full_page"},
    "fingerprint": {"algorithm": "simhash"},
}


class TestValidateInfoSpecWithErrors:
    def test_valid_doc_returns_empty_list(self):
        assert validate_info_spec_with_errors(VALID_DOC) == []

    def test_missing_required_field_yields_issue(self):
        bad = dict(VALID_DOC)
        bad.pop("fingerprint")
        issues = validate_info_spec_with_errors(bad)
        assert len(issues) >= 1
        assert all(isinstance(i, InfoSpecValidationIssue) for i in issues)

    def test_unsupported_schema_version_yields_targeted_issue(self):
        issues = validate_info_spec_with_errors({**VALID_DOC, "schema_version": 99})
        assert len(issues) == 1
        assert issues[0].path == ["schema_version"]
        assert "schema_version" in issues[0].message


class TestValidateInfoSpecRaises:
    def test_invalid_doc_raises_with_structured_issues(self):
        bad = dict(VALID_DOC)
        bad.pop("fingerprint")
        with pytest.raises(InfoSpecValidationError) as exc_info:
            validate_info_spec(bad)
        # The exception carries the same structured list that
        # validate_info_spec_with_errors would have returned.
        assert len(exc_info.value.issues) >= 1
        assert all(hasattr(i, "path") and hasattr(i, "message") for i in exc_info.value.issues)

    def test_unsupported_schema_version_raises_with_single_issue(self):
        with pytest.raises(InfoSpecValidationError) as exc_info:
            validate_info_spec({**VALID_DOC, "schema_version": 2})
        assert len(exc_info.value.issues) == 1
        assert exc_info.value.issues[0].path == ["schema_version"]

    def test_valid_doc_does_not_raise(self):
        validate_info_spec(VALID_DOC)  # no exception

    def test_construct_error_without_issues_defaults_to_empty_list(self):
        """Manual raise (e.g. from preview_extraction's defensive path) leaves issues=[]."""
        err = InfoSpecValidationError("freeform message")
        assert err.issues == []
        assert str(err) == "freeform message"
