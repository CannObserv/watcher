"""Validate InfoSpec document bodies against the v1 JSON Schema."""

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

_v1_schema: dict[str, Any] | None = None


def _load_v1_schema() -> dict[str, Any]:
    global _v1_schema
    if _v1_schema is None:
        pkg = resources.files("src.information.core.info_spec_schema")
        text = pkg.joinpath("v1.json").read_text()
        _v1_schema = json.loads(text)
    return _v1_schema


class InfoSpecValidationError(ValueError):
    """Raised when a document fails InfoSpec schema validation."""


@dataclass(frozen=True)
class InfoSpecValidationIssue:
    """Single validation problem with a structured path + message.

    Used by callers (e.g. the validate_info_spec tool route) that need to return
    field-level errors instead of a single concatenated string.
    """

    path: list[str | int]
    message: str


def validate_info_spec_with_errors(document: dict[str, Any]) -> list[InfoSpecValidationIssue]:
    """Return a list of validation issues, empty if the document is valid.

    Does not raise. Use this when you want to surface per-field errors to a
    caller (e.g. an HTTP 200 ``{"valid": false, "errors": [...]}`` response).
    """
    schema_version = document.get("schema_version")
    if schema_version != 1:
        return [
            InfoSpecValidationIssue(
                path=["schema_version"],
                message=f"Unsupported schema_version: {schema_version!r} (expected 1)",
            )
        ]
    schema = _load_v1_schema()
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda e: e.path)
    return [InfoSpecValidationIssue(path=list(e.absolute_path), message=e.message) for e in errors]


def validate_info_spec(document: dict[str, Any]) -> None:
    """Raise InfoSpecValidationError if document is invalid against the declared schema_version.

    Currently supports schema_version=1 only.
    """
    issues = validate_info_spec_with_errors(document)
    if issues:
        details = "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        raise InfoSpecValidationError(f"InfoSpec invalid: {details}")
