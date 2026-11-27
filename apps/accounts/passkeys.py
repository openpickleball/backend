"""Passkeys (WebAuthn) with OPEN's rules (issue #117, ADR 0056).

allauth.mfa runs the WebAuthn ceremonies. These helpers hold what OPEN
changes around them:

- Ids. allauth names an Account to the authenticator by its row id (the
  WebAuthn user handle) and lists a passkey by its Authenticator row id.
  OPEN uses keyed hashes instead (CLAUDE.md, Public identifiers):
  ``user_handle`` for the Account, ``public_passkey_id`` for a passkey.
  The handle is checked against ``SECRET_KEY`` and every key in
  ``SECRET_KEY_FALLBACKS``, so a key rotation does not lock out passkeys
  while the old key is a fallback.
- Origins. allauth's WebAuthn server accepts any origin under the
  relying party ID. OPEN accepts only the exact origins in
  ``WEBAUTHN_ALLOWED_ORIGINS`` (``check_origin``), read from the
  credential's client data before allauth verifies it, so a refused
  credential consumes no challenge and changes nothing.
"""

import base64
import binascii
import hmac
import json
from typing import TYPE_CHECKING

from allauth.mfa.adapter import get_adapter
from allauth.mfa.models import Authenticator
from django.conf import settings
from django.utils.crypto import salted_hmac

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

_HANDLE_SALT = "apps.accounts.passkeys.user_handle"
_ID_SALT = "apps.accounts.passkeys.public_passkey_id"
# Hex characters of a public passkey id: 128 bits.
_ID_LENGTH = 32


def _handle(pk: object, secret: str | bytes) -> bytes:
    digest: str = salted_hmac(
        _HANDLE_SALT, str(pk), secret=secret, algorithm="sha256"
    ).hexdigest()
    # 64 ASCII bytes, WebAuthn's limit for a user handle.
    return digest.encode("ascii")


def user_handle(account: "_User") -> bytes:
    """The WebAuthn user handle of ``account``: a keyed hash of its id."""
    return _handle(account.pk, settings.SECRET_KEY)


def user_handle_matches(account: "_User", handle: bytes) -> bool:
    """Whether ``handle`` is ``account``'s, made with the current secret
    key or any fallback key."""
    secrets = [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
    return any(
        hmac.compare_digest(handle, _handle(account.pk, secret)) for secret in secrets
    )


def public_passkey_id(authenticator: Authenticator) -> str:
    """The id the API shows a passkey by: a keyed hash of its row id."""
    digest: str = salted_hmac(
        _ID_SALT, str(authenticator.pk), algorithm="sha256"
    ).hexdigest()
    return digest[:_ID_LENGTH]


def passkeys_by_public_id(account: "_User") -> dict[str, Authenticator]:
    """The Account's own passkeys, keyed by their public ids."""
    rows = Authenticator.objects.filter(
        user_id=account.pk, type=Authenticator.Type.WEBAUTHN
    )
    return {public_passkey_id(row): row for row in rows}


def _websafe_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def client_data_origin(credential: object) -> str | None:
    """The origin in a WebAuthn credential's client data, or None when
    the credential or its client data is malformed."""
    if not isinstance(credential, dict):
        return None
    response = credential.get("response")
    if not isinstance(response, dict):
        return None
    encoded = response.get("clientDataJSON")
    if not isinstance(encoded, str):
        return None
    try:
        client_data = json.loads(_websafe_decode(encoded))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(client_data, dict):
        return None
    origin = client_data.get("origin")
    return origin if isinstance(origin, str) else None


def check_origin(credential: object) -> None:
    """Refuse a credential whose origin is not an allowed origin, with
    allauth's own error for a bad credential (``incorrect_code``)."""
    if client_data_origin(credential) not in settings.WEBAUTHN_ALLOWED_ORIGINS:
        raise get_adapter().validation_error("incorrect_code")
