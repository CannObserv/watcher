"""Typed exceptions raised by InformationClient."""

from __future__ import annotations


class InformationError(Exception):
    """Base error for the Information SDK."""

    def __init__(
        self, message: str, *, status_code: int | None = None, body: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class AuthError(InformationError):
    """401 / 403 from the Information service."""


class NotFound(InformationError):
    """404 — InfoItem or InfoSpec missing."""


class ValidationError(InformationError):
    """422 — request body or path didn't validate."""


class ServerError(InformationError):
    """5xx from the Information service."""


def error_from_response(status: int, body: bytes) -> InformationError:
    """Map an HTTP status + body to the appropriate InformationError subclass."""
    body_text = body.decode("utf-8", errors="replace")[:2000]  # truncate noisy bodies
    msg = f"Information service returned {status}: {body_text[:200]}"
    if status in (401, 403):
        return AuthError(msg, status_code=status, body=body_text)
    if status == 404:
        return NotFound(msg, status_code=status, body=body_text)
    if status == 422:
        return ValidationError(msg, status_code=status, body=body_text)
    if 500 <= status < 600:
        return ServerError(msg, status_code=status, body=body_text)
    return InformationError(msg, status_code=status, body=body_text)
