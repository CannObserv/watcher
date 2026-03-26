"""Shared Pydantic types for API schemas."""

from typing import Annotated

from pydantic import BeforeValidator, HttpUrl, TypeAdapter

_http_url_adapter = TypeAdapter(HttpUrl)


def _validate_http_url(v: object) -> str:
    """Validate that *v* is a valid HTTP(S) URL and return it as a plain string."""
    return str(_http_url_adapter.validate_python(v))


HttpUrlStr = Annotated[str, BeforeValidator(_validate_http_url)]
"""URL string validated as ``http`` or ``https``. Resolves to plain ``str``."""

ULIDStr = Annotated[str, BeforeValidator(lambda v: str(v))]
