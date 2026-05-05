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

    def test_nested_field_violation_path_reflects_json_structure(self):
        """A bad nested field surfaces as a path list, not a flat key."""
        # ``target.url`` is required; removing it triggers a nested-required failure.
        issues = validate_info_spec_with_errors({**VALID_DOC, "target": {}})
        assert len(issues) >= 1
        # At least one issue must point inside the ``target`` object.
        assert any(i.path and i.path[0] == "target" for i in issues)

    def test_nested_extraction_violation_path_includes_extraction(self):
        """css algorithm without selector violates the v1 schema's allOf rule."""
        issues = validate_info_spec_with_errors({**VALID_DOC, "extraction": {"algorithm": "css"}})
        assert len(issues) >= 1
        # The conditional rule fires inside the ``extraction`` subtree.
        assert any(
            (i.path and "extraction" in [str(s) for s in i.path]) or "extraction" in i.message
            for i in issues
        )


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
