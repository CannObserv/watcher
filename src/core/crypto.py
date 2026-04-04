"""Fernet symmetric encryption for Apprise URL credentials."""

import os

from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    """Return a Fernet instance keyed from APPRISE_SECRET_KEY env var."""
    key = os.environ.get("APPRISE_SECRET_KEY")
    if not key:
        raise RuntimeError(
            "APPRISE_SECRET_KEY environment variable not set. "
            'Generate one with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_apprise_url(url: str) -> str:
    """Encrypt an Apprise URL string. Returns a Fernet token (str)."""
    return _get_fernet().encrypt(url.encode()).decode()


def decrypt_apprise_url(token: str) -> str:
    """Decrypt a Fernet-encrypted Apprise URL token. Returns the plaintext URL."""
    return _get_fernet().decrypt(token.encode()).decode()
