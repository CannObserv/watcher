"""InfoSpec JSON Schema definitions and validator."""

from src.information.core.info_spec_schema.validator import (
    InfoSpecValidationError,
    validate_info_spec,
)

__all__ = ["InfoSpecValidationError", "validate_info_spec"]
