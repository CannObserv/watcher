"""Shared Pydantic types for API schemas."""

from typing import Annotated

from pydantic import BeforeValidator, HttpUrl, TypeAdapter, WithJsonSchema
from ulid import ULID

_http_url_adapter = TypeAdapter(HttpUrl)


def _validate_http_url(v: object) -> str:
    """Validate that *v* is a valid HTTP(S) URL and return it as a plain string."""
    return str(_http_url_adapter.validate_python(v))


HttpUrlStr = Annotated[str, BeforeValidator(_validate_http_url)]
"""URL string validated as ``http`` or ``https``. Resolves to plain ``str``."""

ULIDStr = Annotated[str, BeforeValidator(lambda v: str(v))]
"""ULID rendered as a string. Coercion only — use for **outbound** fields, where
the value comes from a ``ULID`` column and is well-formed by construction."""


ULID_PATTERN = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
"""Canonical ULID: 26 chars of uppercase Crockford base32, first char ≤ 7.
Mirrors what ``ULID.from_str`` accepts — kept in sync by the schema tests."""


def _validate_ulid_ref(v: object) -> str:
    """Validate that *v* is a well-formed ULID, returning it as a plain string.

    Same parser as ``parse_ulid`` in ``src/api/routes/helpers.py``, so an
    inbound reference is held to exactly the standard a path parameter is
    (canonical Crockford base32, **uppercase** — ``ULID.from_str`` rejects the
    lowercase form, and so does this).

    *v* is passed through un-coerced: a non-string trips ``from_str``'s own
    ``TypeError`` and fails as a type error, rather than being stringified into
    a length complaint about a value that was never a ULID to begin with.
    """
    try:
        return str(ULID.from_str(v))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"must be a 26-character ULID: {exc}") from exc


ULIDRefStr = Annotated[
    str,
    BeforeValidator(_validate_ulid_ref),
    # A BeforeValidator is invisible to JSON Schema, so state the constraint in
    # the spec too (#251 CR-10) — otherwise a client generated from the OpenAPI
    # document sees a bare string and sends values the API rejects.
    WithJsonSchema({"type": "string", "format": "ulid", "pattern": ULID_PATTERN}),
]
"""ULID string validated on the way **in**. Use for client-supplied cross-schema
references (#251) — a malformed one must fail at the boundary as a 422, not
persist and surface later as an Archiver-side failure against a real revision."""
