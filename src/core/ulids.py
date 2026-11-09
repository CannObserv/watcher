"""The canonical-ULID parse guard (#432).

Watcher accepts one spelling of a ULID: 26 characters of **uppercase**
Crockford base32, first character ≤ 7 — the form ``str(ULID)`` produces and
the OpenAPI document advertises. The library is not that contract: python-ulid
rejected lowercase from 2.6.0 and accepts it again from 4.1.0. So every parse of
an id that arrives from outside — a path, a filter, a request body, a bus
message, a task argument — goes through :func:`parse_canonical_ulid`, and a
dependency bump cannot change what Watcher accepts.

``tests/core/test_ulids.py`` fails any bare ``ULID.from_str`` in ``src/``
outside an allowlist that names its reason.
"""

import re

from ulid import ULID

ULID_PATTERN = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
"""Canonical ULID: 26 chars of uppercase Crockford base32, first char ≤ 7."""

_CANONICAL = re.compile(ULID_PATTERN)


def parse_canonical_ulid(value: object) -> ULID:
    """Parse *value* as a canonical ULID.

    Raises ``TypeError`` for a non-string and ``ValueError`` for any string off
    :data:`ULID_PATTERN` — the same exception types ``ULID.from_str`` raises, so
    a caller swaps one for the other unchanged. ``fullmatch`` rather than the
    pattern's ``$``, which would also match before a trailing newline.
    """
    if not isinstance(value, str):
        raise TypeError(f"ULID must be of type str, not {type(value).__name__}")
    if _CANONICAL.fullmatch(value) is None:
        raise ValueError("not a canonical ULID: 26 uppercase Crockford base32 characters")
    return ULID.from_str(value)
