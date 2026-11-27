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
from typing import TYPE_CHECKING, Any

from allauth.headless.internal.restkit import inputs
from allauth.headless.mfa.inputs import (
    AddWebAuthnInput,
    AuthenticateWebAuthnInput,
    LoginWebAuthnInput,
    ReauthenticateWebAuthnInput,
)
from allauth.mfa.adapter import get_adapter
from allauth.mfa.models import Authenticator
from allauth.mfa.webauthn.internal import auth as webauthn_auth
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.utils.crypto import salted_hmac
from fido2.utils import websafe_encode
from fido2.webauthn import AuthenticationResponse, RegistrationResponse

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
        raise _incorrect()


# Fixed text: never the id that was sent.
UNKNOWN_PASSKEY = "Select passkeys from your passkey list."


def _incorrect() -> ValidationError:
    error: ValidationError = get_adapter().validation_error("incorrect_code")
    return error


def _parse_registration(credential: object) -> RegistrationResponse:
    """The registration response, or allauth's ``incorrect_code``. allauth
    catches only TypeError, so a mismatched ``id`` would be a 500."""
    if not isinstance(credential, dict):
        raise _incorrect()
    try:
        parsed: RegistrationResponse = RegistrationResponse.from_dict(credential)
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        raise _incorrect() from error
    return parsed


def _parse_authentication(credential: object) -> AuthenticationResponse:
    """The authentication response, or allauth's ``incorrect_code``."""
    if not isinstance(credential, dict):
        raise _incorrect()
    try:
        parsed: AuthenticationResponse = AuthenticationResponse.from_dict(credential)
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        raise _incorrect() from error
    return parsed


def _registered_with_id(credential_id: str) -> QuerySet[Authenticator]:
    found: QuerySet[Authenticator] = Authenticator.objects.filter(
        type=Authenticator.Type.WEBAUTHN, data__credential__id=credential_id
    )
    return found


class _ChecksOrigin:
    """Check the credential's origin and shape before allauth verifies
    it. allauth's parser lets a KeyError or AttributeError through (a
    500), so OPEN's parser reads it first."""

    cleaned_data: dict[str, Any]

    def clean_credential(self) -> Any:
        credential = self.cleaned_data.get("credential")
        check_origin(credential)
        _parse_authentication(credential)
        return super().clean_credential()  # type: ignore[misc]


class AddPasskeyInput(AddWebAuthnInput):  # type: ignore[misc]
    """allauth's add input, from an allowed origin only, refusing a
    credential id that any Account already holds (WebAuthn's
    registration step that allauth leaves out), so passkey login finds
    one passkey by its id."""

    def clean_credential(self) -> Any:
        credential = self.cleaned_data.get("credential")
        check_origin(credential)
        response = _parse_registration(credential)
        canonical = websafe_encode(response.raw_id)
        if credential.get("id") != canonical:
            raise _incorrect()
        if _registered_with_id(canonical).exists():
            raise _incorrect()
        return credential


class AuthenticatePasskeyInput(_ChecksOrigin, AuthenticateWebAuthnInput):  # type: ignore[misc]
    """allauth's MFA login stage input, from an allowed origin only."""


class ReauthenticatePasskeyInput(_ChecksOrigin, ReauthenticateWebAuthnInput):  # type: ignore[misc]
    """allauth's passkey reauthentication input, from an allowed origin
    only."""


class LoginPasskeyInput(LoginWebAuthnInput):  # type: ignore[misc]
    """Passkey login, finding the Account by the credential, not by
    allauth's row id user handle.

    The passkey is the one WebAuthn Authenticator with the assertion's
    credential id. A user handle, when sent, must be that Account's
    keyed handle. allauth then verifies the assertion against the
    Account's passkeys and requires user verification, with no per
    Account rate limit (the Account comes from the request).
    """

    def clean_credential(self) -> Authenticator:
        credential = self.cleaned_data.get("credential")
        check_origin(credential)
        response = _parse_authentication(credential)
        found = list(_registered_with_id(websafe_encode(response.raw_id))[:2])
        if len(found) != 1:
            raise _incorrect()
        account = found[0].user
        handle = response.response.user_handle
        if handle is not None and not user_handle_matches(account, handle):
            raise _incorrect()
        authenticator: Authenticator = webauthn_auth.complete_authentication(
            account, credential
        )
        if (
            authenticator.pk != found[0].pk
            or not response.response.authenticator_data.is_user_verified()
        ):
            raise _incorrect()
        return authenticator


class UpdatePasskeyInput(inputs.Input):  # type: ignore[misc]
    """A rename: a public passkey id from the player's list and a name.
    Any other id is refused with one fixed message."""

    name = inputs.CharField(required=True, max_length=100)

    def __init__(self, *args: Any, user: "_User", **kwargs: Any) -> None:
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        sent = self.data.get("id")
        known = passkeys_by_public_id(self.user)
        if not isinstance(sent, str) or sent not in known:
            raise ValidationError(UNKNOWN_PASSKEY, code="invalid")
        cleaned["id"] = known[sent]
        return cleaned


class DeletePasskeyInput(inputs.Input):  # type: ignore[misc]
    """A removal: a non-empty list of public passkey ids from the
    player's list. Any other id refuses the whole request with one fixed
    message."""

    def __init__(self, *args: Any, user: "_User", **kwargs: Any) -> None:
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self) -> dict[str, Any]:
        cleaned: dict[str, Any] = super().clean() or {}
        sent = self.data.get("authenticators")
        if (
            not isinstance(sent, list)
            or not sent
            or not all(isinstance(value, str) for value in sent)
        ):
            raise ValidationError(UNKNOWN_PASSKEY, code="invalid")
        known = passkeys_by_public_id(self.user)
        if any(value not in known for value in sent):
            raise ValidationError(UNKNOWN_PASSKEY, code="invalid")
        cleaned["authenticators"] = [known[value] for value in dict.fromkeys(sent)]
        return cleaned
