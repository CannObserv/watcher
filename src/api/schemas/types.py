"""Shared Pydantic types for API schemas."""

from typing import Annotated

from pydantic import BeforeValidator

ULIDStr = Annotated[str, BeforeValidator(lambda v: str(v))]
