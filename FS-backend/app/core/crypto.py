"""Encryption of stored third-party secrets (plan section 6): Fireflies API keys.

FIREFLIES_KEY_ENCRYPTION_KEY holds one or more comma-separated Fernet keys. The first key encrypts; every listed key
can decrypt, so the key can be rotated without breaking stored values. Fernet is AES-128-CBC with a random IV per
value, authenticated with HMAC-SHA256: a tampered or foreign value fails to decrypt instead of producing garbage.
Plaintext secrets are never logged.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .config import get_settings


class LinkingNotConfigured(Exception):
    """FIREFLIES_KEY_ENCRYPTION_KEY is not set, so secrets cannot be stored (plan 6.4)."""


class SecretDecryptionError(Exception):
    """A stored value cannot be decrypted with any configured key (the user must reconnect)."""


def _keys() -> list[Fernet]:
    raw = get_settings().fireflies_key_encryption_key or ""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise LinkingNotConfigured("Fireflies account linking is not configured on this server.")
    return [Fernet(k.encode()) for k in keys]


def linking_configured() -> bool:
    """True when at least one valid encryption key is configured."""
    try:
        _keys()
    except (LinkingNotConfigured, ValueError):
        return False
    return True


def encrypt_secret(plaintext: str) -> str:
    """Encrypts with the first configured key."""
    return _keys()[0].encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypts with any configured key; raises SecretDecryptionError when none can."""
    try:
        return MultiFernet(_keys()).decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretDecryptionError("Stored secret cannot be decrypted with the configured keys.") from exc


def needs_reencryption(token: str) -> bool:
    """True when the value decrypts, but not with the first (current) key (plan 6.2, lazy re-encryption)."""
    keys = _keys()
    try:
        keys[0].decrypt(token.encode("ascii"))
        return False
    except InvalidToken:
        return True


def reencrypt_secret(token: str) -> str:
    """The same secret, encrypted with the first key. Raises SecretDecryptionError when no key can decrypt it."""
    try:
        return MultiFernet(_keys()).rotate(token.encode("ascii")).decode("ascii")
    except InvalidToken as exc:
        raise SecretDecryptionError("Stored secret cannot be decrypted with the configured keys.") from exc
