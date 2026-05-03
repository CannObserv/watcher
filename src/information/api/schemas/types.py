"""Shared FastAPI/Pydantic types for the Information service."""

from typing import Annotated

from pydantic import AfterValidator
from ulid import ULID


def _validate_ulid_str(value: str) -> str:
    """Pydantic validator: reject non-ULID path strings with 422."""
    try:
        ULID.from_str(value)
    except ValueError as e:
        raise ValueError(f"Invalid ULID: {value!r}") from e
    return value


ULIDStr = Annotated[str, AfterValidator(_validate_ulid_str)]
