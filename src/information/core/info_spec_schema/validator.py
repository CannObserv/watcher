"""Validate InfoSpec document bodies against the v1 JSON Schema."""

import json
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


def validate_info_spec(document: dict[str, Any]) -> None:
    """Raise InfoSpecValidationError if document is invalid against the declared schema_version.

    Currently supports schema_version=1 only.
    """
    schema_version = document.get("schema_version")
    if schema_version != 1:
        raise InfoSpecValidationError(
            f"Unsupported schema_version: {schema_version!r} (expected 1)"
        )
    schema = _load_v1_schema()
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda e: e.path)
    if errors:
        details = "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)
        raise InfoSpecValidationError(f"InfoSpec invalid: {details}")
