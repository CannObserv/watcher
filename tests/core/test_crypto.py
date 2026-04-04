"""Tests for Fernet URL encryption utility."""

import pytest
from cryptography.fernet import Fernet

from src.core.crypto import decrypt_apprise_url, encrypt_apprise_url


@pytest.fixture(autouse=True)
def set_test_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("APPRISE_SECRET_KEY", key)


def test_encrypt_returns_string():
    token = encrypt_apprise_url("slack://T/A/T/#ops")
    assert isinstance(token, str)
    assert token != "slack://T/A/T/#ops"


def test_round_trip():
    url = "mailtos://user:pass@smtp.example.com"
    assert decrypt_apprise_url(encrypt_apprise_url(url)) == url


def test_different_plaintexts_produce_different_tokens():
    a = encrypt_apprise_url("slack://T/A/T/#ops")
    b = encrypt_apprise_url("slack://X/Y/Z/#dev")
    assert a != b


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("APPRISE_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="APPRISE_SECRET_KEY"):
        encrypt_apprise_url("slack://T/A/T/#ops")
