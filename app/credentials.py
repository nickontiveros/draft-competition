"""Encryption-at-rest for Kalshi private keys.

If CRED_SECRET is set (a Fernet key), credentials are encrypted before storage.
Without it (local dev), they are stored as-is with a marker prefix.
"""

from cryptography.fernet import Fernet

from app.config import settings

_PLAIN_PREFIX = "plain:"
_ENC_PREFIX = "enc:"


def seal(secret: str) -> str:
    if settings.cred_secret:
        f = Fernet(settings.cred_secret.encode())
        return _ENC_PREFIX + f.encrypt(secret.encode()).decode()
    return _PLAIN_PREFIX + secret


def unseal(stored: str) -> str:
    if stored.startswith(_ENC_PREFIX):
        f = Fernet(settings.cred_secret.encode())
        return f.decrypt(stored[len(_ENC_PREFIX):].encode()).decode()
    if stored.startswith(_PLAIN_PREFIX):
        return stored[len(_PLAIN_PREFIX):]
    return stored
