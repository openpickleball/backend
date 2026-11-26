"""Encrypt and decrypt text with the keys in FIELD_ENCRYPTION_KEYS (ADR 0046).

The setting lists Fernet keys, newest first. The newest key encrypts;
every listed key decrypts, so a key stays listed until
``manage.py reencrypt_fields`` has rewritten every value with a newer
one. Errors carry fixed text: never a key, a token, or a value.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

MISSING_KEYS_MESSAGE = (
    "Set FIELD_ENCRYPTION_KEYS to encrypt or read personal data (see .env.example)."
)
DECRYPTION_FAILED_MESSAGE = (
    "A stored value could not be decrypted with any key in FIELD_ENCRYPTION_KEYS."
)


class DecryptionError(Exception):
    """No configured key decrypts a value. Fixed text, never the value."""

    def __init__(self) -> None:
        super().__init__(DECRYPTION_FAILED_MESSAGE)


def fernet() -> MultiFernet:
    """The configured keys, newest first, as one ``MultiFernet``."""
    keys = tuple(settings.FIELD_ENCRYPTION_KEYS)
    if not keys:
        raise ImproperlyConfigured(MISSING_KEYS_MESSAGE)
    return _fernet(keys)


@lru_cache(maxsize=8)
def _fernet(keys: tuple[str, ...]) -> MultiFernet:
    # Cached per tuple of keys, so a settings override takes effect.
    return MultiFernet([Fernet(key) for key in keys])


def encrypt_text(text: str) -> str:
    """``text`` encrypted with the newest key, as a Fernet token."""
    return fernet().encrypt(text.encode()).decode()


def decrypt_text(token: str) -> str:
    """The text in ``token``; ``DecryptionError`` when no key decrypts it."""
    keys = fernet()
    try:
        return keys.decrypt(token.encode()).decode()
    except (InvalidToken, UnicodeError):
        raise DecryptionError from None


def is_token(value: str) -> bool:
    """True when a configured key decrypts ``value``."""
    try:
        decrypt_text(value)
    except DecryptionError:
        return False
    return True


def rotate_text(token: str) -> str:
    """``token`` encrypted again with the newest key, keeping its text."""
    keys = fernet()
    try:
        return keys.rotate(token.encode()).decode()
    except (InvalidToken, UnicodeError):
        raise DecryptionError from None
