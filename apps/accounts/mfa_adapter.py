"""OPEN's allauth MFA adapter (ADR 0043, ADR 0046).

allauth stores each TOTP secret and recovery code seed through the
adapter's ``encrypt()`` and reads it through ``decrypt()``. OPEN encrypts
them with the field encryption keys (``FIELD_ENCRYPTION_KEYS``), so the
database holds Fernet tokens only. ``decrypt()`` never falls back to
plain text: a value no key decrypts raises ``DecryptionError``.
"""

from allauth.mfa.adapter import DefaultMFAAdapter

from apps.encryption.keys import decrypt_text, encrypt_text


class MFAAdapter(DefaultMFAAdapter):  # type: ignore[misc]
    def encrypt(self, text: str) -> str:
        return encrypt_text(text)

    def decrypt(self, encrypted_text: str) -> str:
        return decrypt_text(encrypted_text)
