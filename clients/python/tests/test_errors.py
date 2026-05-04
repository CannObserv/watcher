"""Direct unit tests for error_from_response status → exception mapping."""

from __future__ import annotations

from information_client.errors import (
    AuthError,
    InformationError,
    NotFound,
    ServerError,
    ValidationError,
    error_from_response,
)


def test_401_returns_auth_error():
    err = error_from_response(401, b"missing key")
    assert isinstance(err, AuthError)
    assert err.status_code == 401
    assert "missing key" in err.body


def test_403_returns_auth_error():
    err = error_from_response(403, b"forbidden")
    assert isinstance(err, AuthError)
    assert err.status_code == 403


def test_404_returns_not_found():
    err = error_from_response(404, b"info_item not found")
    assert isinstance(err, NotFound)
    assert err.status_code == 404


def test_422_returns_validation_error():
    err = error_from_response(422, b'{"detail": "bad input"}')
    assert isinstance(err, ValidationError)
    assert err.status_code == 422


def test_500_returns_server_error():
    err = error_from_response(500, b"internal")
    assert isinstance(err, ServerError)
    assert err.status_code == 500


def test_503_returns_server_error():
    err = error_from_response(503, b"unavailable")
    assert isinstance(err, ServerError)


def test_unknown_status_returns_base_information_error():
    err = error_from_response(418, b"teapot")
    assert type(err) is InformationError
    assert err.status_code == 418


def test_body_is_decoded_from_bytes():
    err = error_from_response(404, b"not found: \xe2\x9c\x93")
    assert "✓" in err.body


def test_body_decode_replaces_invalid_utf8():
    err = error_from_response(500, b"\xff\xfe broken")
    assert err.body is not None
    # Should not raise — replacement chars in body, not an exception.


def test_body_truncated_at_2000_chars():
    huge = b"x" * 5000
    err = error_from_response(500, huge)
    assert len(err.body) == 2000


def test_message_includes_status_and_truncated_body():
    err = error_from_response(404, b"the resource was not found anywhere")
    assert "404" in str(err)
    assert "the resource" in str(err)
