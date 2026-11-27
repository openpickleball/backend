"""OPEN's allauth MFA adapter (ADR 0043, ADR 0046, ADR 0056).

allauth stores each TOTP secret and recovery code seed through the
adapter's ``encrypt()`` and reads it through ``decrypt()``. OPEN encrypts
them with the field encryption keys (``FIELD_ENCRYPTION_KEYS``), so the
database holds Fernet tokens only. ``decrypt()`` never falls back to
plain text: a value no key decrypts raises ``DecryptionError``.

For passkeys (#117), the WebAuthn relying party is ``WEBAUTHN_RP_ID``
named ``OPEN``, never the request's host, and the user handle is OPEN's
keyed hash (``apps.accounts.passkeys.user_handle``), never the Account's
row id.
"""

from typing import TYPE_CHECKING, Any

from allauth.mfa.adapter import DefaultMFAAdapter
from django.conf import settings

from apps.accounts.passkeys import user_handle
from apps.encryption.keys import decrypt_text, encrypt_text

if TYPE_CHECKING:
    from django.contrib.auth.models import _User


class MFAAdapter(DefaultMFAAdapter):  # type: ignore[misc]
    def encrypt(self, text: str) -> str:
        return encrypt_text(text)

    def decrypt(self, encrypted_text: str) -> str:
        return decrypt_text(encrypted_text)

    def get_public_key_credential_rp_entity(self) -> dict[str, str]:
        return {"id": settings.WEBAUTHN_RP_ID, "name": "OPEN"}

    def get_public_key_credential_user_entity(self, user: "_User") -> dict[str, Any]:
        entity: dict[str, Any] = super().get_public_key_credential_user_entity(user)
        entity["id"] = user_handle(user)
        return entity
