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
import struct
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
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.utils.crypto import salted_hmac
from fido2 import cbor
from fido2.cose import CoseKey
from fido2.utils import websafe_encode
from fido2.webauthn import AuthenticationResponse, RegistrationResponse

from apps.accounts.mfa import MFA_METHOD_TYPES, record_mfa_change

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


# Real client data is a few hundred bytes. Anything over this is refused
# before it is decoded, so a deeply nested document never reaches the
# JSON decoder.
_MAX_CLIENT_DATA_BYTES = 4096
_MAX_CLIENT_DATA_TEXT = -(-_MAX_CLIENT_DATA_BYTES // 3) * 4
# allauth's challenges are 32 random bytes, 43 base64url characters.
_MAX_CHALLENGE_TEXT = 128


def _websafe_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def client_data_origin(credential: object) -> str | None:
    """The origin in a WebAuthn credential's client data, or None.

    None when the credential or its client data is malformed or too
    large (``type`` and ``challenge`` must be text, the challenge at most
    128 characters), or when the credential was made in a cross-origin
    frame (``crossOrigin`` other than false, or any ``topOrigin``): OPEN
    is never framed (``frame-ancestors 'none'``)."""
    if not isinstance(credential, dict):
        return None
    response = credential.get("response")
    if not isinstance(response, dict):
        return None
    encoded = response.get("clientDataJSON")
    if not isinstance(encoded, str) or len(encoded) > _MAX_CLIENT_DATA_TEXT:
        return None
    try:
        raw = _websafe_decode(encoded)
        if len(raw) > _MAX_CLIENT_DATA_BYTES:
            return None
        client_data = json.loads(raw)
    except (binascii.Error, ValueError, RecursionError):
        return None
    if not isinstance(client_data, dict):
        return None
    # fido2 compares ``type`` with its enum and decodes ``challenge`` with
    # bytes() when it is not text: both must be text, the challenge short.
    if not isinstance(client_data.get("type"), str):
        return None
    challenge = client_data.get("challenge")
    if not isinstance(challenge, str) or len(challenge) > _MAX_CHALLENGE_TEXT:
        return None
    if client_data.get("crossOrigin", False) is not False:
        return None
    if "topOrigin" in client_data:
        return None
    origin = client_data.get("origin")
    return origin if isinstance(origin, str) else None


def check_origin(credential: object) -> None:
    """Refuse a credential whose origin is not an allowed origin, with
    allauth's own error for a bad credential (``incorrect_code``)."""
    if client_data_origin(credential) not in settings.WEBAUTHN_ALLOWED_ORIGINS:
        raise _incorrect()


def remove_passkeys_after_reset(account: "_User") -> None:
    """Remove every passkey of ``account`` after a password reset from a
    key (ADR 0056), recording ``removed webauthn`` per passkey, and
    ``removed recovery_codes`` when no other method keeps the codes, as
    the passkey view does. Someone who knew the password could have added
    a passkey, which signs in alone. Call inside the reset's transaction:
    the Account row is locked first, before any authenticator row. When
    the passkeys were the last method, the other admins get the security
    notice (#232), registered in this transaction.
    """
    list(
        get_user_model()
        .objects.select_for_update()
        .filter(pk=account.pk)
        .values_list("pk", flat=True)
    )
    rows = list(
        Authenticator.objects.select_for_update().filter(
            user_id=account.pk, type=Authenticator.Type.WEBAUTHN
        )
    )
    if not rows:
        return
    codes = Authenticator.objects.filter(
        user_id=account.pk, type=Authenticator.Type.RECOVERY_CODES
    )
    codes_go = (
        not Authenticator.objects.filter(user_id=account.pk)
        .exclude(type__in=[Authenticator.Type.RECOVERY_CODES])
        .exclude(pk__in=[row.pk for row in rows])
        .exists()
        and codes.exists()
    )
    for _ in rows:
        record_mfa_change(account, "removed", Authenticator.Type.WEBAUTHN)
    if codes_go:
        record_mfa_change(account, "removed", Authenticator.Type.RECOVERY_CODES)
        codes.delete()
    Authenticator.objects.filter(pk__in=[row.pk for row in rows]).delete()
    if not Authenticator.objects.filter(
        user_id=account.pk, type__in=MFA_METHOD_TYPES
    ).exists():
        # The reset is never refused, but the other admins of the
        # Account's Organizers and Clubs hear it lost its last method
        # (#232, ADR 0057). Imported here: apps.clubs imports this app.
        from apps.clubs.notices import schedule_mfa_removed

        schedule_mfa_removed(account.pk)


# Fixed text: never the id that was sent.
UNKNOWN_PASSKEY = "Select passkeys from your passkey list."
PASSKEY_NAME_NOT_TEXT = "Enter the passkey's name as text."


def _incorrect() -> ValidationError:
    error: ValidationError = get_adapter().validation_error("incorrect_code")
    return error


# What fido2's JSON and CBOR decoders raise on malformed input. Its CBOR
# decoder recurses, so deeply nested data raises RecursionError.
_DECODE_ERRORS = (
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    IndexError,
    OverflowError,
    RecursionError,
    struct.error,
)

# Generous caps on the binary members of a credential, in bytes. Each
# binary member must be base64url text within its cap before anything is
# decoded: fido2 turns a JSON integer N into N zero bytes and a list of
# integers into bytes, so a member of another type is refused, never
# parsed. Real values fit with room to spare: authenticator data of
# under 200 bytes, an ECDSA or EdDSA signature of under 150 bytes and an
# RSA one of up to 512, an ML-DSA-87 signature (which fido2 offers through
# allauth's server when cryptography supports it) of 4627 bytes, and an
# attestation object of a few hundred bytes, a few KiB with a certificate
# chain or an ML-DSA-87 public key (2592 bytes). The caps bound the
# decoders' work; RecursionError covers nesting that fits under them.
_MAX_CREDENTIAL_ID_BYTES = 1023  # WebAuthn's limit for a credential id
_MAX_USER_HANDLE_BYTES = 64  # WebAuthn's limit for a user handle
_MAX_ATTESTATION_OBJECT_BYTES = 16384
_MAX_AUTHENTICATOR_DATA_BYTES = 4096
_MAX_SIGNATURE_BYTES = 8192


def _text_length(max_bytes: int) -> int:
    """The longest base64url text that ``max_bytes`` bytes encode to."""
    return -(-max_bytes // 3) * 4


def _text_within(value: object, max_bytes: int) -> bool:
    return isinstance(value, str) and len(value) <= _text_length(max_bytes)


# Where fido2 2.2.1 turns an untrusted value into bytes while parsing a
# credential (RegistrationResponse and AuthenticationResponse.from_dict),
# and what refuses a bad value before fido2 sees it. _parse_value decodes
# text with websafe_decode and passes anything else to the bytes type
# (``bytes(N)`` is N zero bytes, a list of ints becomes bytes), and
# websafe_decode itself calls bytes() on anything but text.
#  1. ``rawId`` (both responses; ``id`` is compared with it as is): text
#     within 1023 bytes (_binary_members_ok).
#  2. ``response.clientDataJSON`` (CollectedClientData, a bytes type):
#     text within 4 KiB (_binary_members_ok, client_data_origin).
#  3. CollectedClientData's ``challenge`` (websafe_decode): text of at
#     most 128 characters, and ``type`` text (client_data_origin). The
#     JSON is bounded by 2 and its RecursionError is caught.
#  4. ``response.attestationObject`` (AttestationObject, a bytes type):
#     text within 16 KiB (_binary_members_ok).
#  5. AttestationObject's ``authData`` (AuthenticatorData(...) calls
#     bytes()): the object must be a CBOR map with a text ``fmt``, a byte
#     string ``authData``, and a map ``attStmt`` (_attestation_shape_ok).
#     Its recursive CBOR decoding is bounded by 4, RecursionError caught.
#  6. AuthenticatorData's parsing (from 5 or 7) reads real bytes only:
#     attested credential data and extensions are CBOR decoded from them,
#     bounded by the caps, RecursionError caught.
#  7. ``response.authenticatorData`` (AuthenticatorData, a bytes type):
#     text within 4 KiB (_binary_members_ok).
#  8. ``response.signature``: text within 8 KiB (_binary_members_ok).
#  9. ``response.userHandle`` (bytes or null): absent, null, or text
#     within 64 bytes (_binary_members_ok).
# No other member becomes bytes: ``clientExtensionResults`` is wrapped as
# a mapping (a non-mapping raises AttributeError, caught; its shape is
# checked when a passkey is added, _extension_results_ok), and
# ``authenticatorAttachment`` and ``type`` are fido2 string enums, whose
# lookup gives None for an unknown value rather than raising.
# After parsing, a COSE key's values reach int.from_bytes or cryptography.
# int.from_bytes takes bytes or any iterable of ints from 0 to 255 (a
# CBOR array, or a CBOR map's keys, bounded by the cap) and raises
# TypeError or ValueError on anything else, both caught; cryptography
# refuses non-bytes. The key is
# probed when added (_public_key_usable) and its errors are caught when an
# assertion is verified. fido2's server decodes only the session's own
# challenge.
def _binary_members_ok(
    credential: dict[str, Any], response_caps: dict[str, int]
) -> bool:
    """Whether ``id``, ``rawId``, and each capped response member are text
    within their caps, and ``userHandle``, when present and not null, too."""
    response = credential.get("response")
    if not isinstance(response, dict):
        return False
    if not all(
        _text_within(credential.get(name), _MAX_CREDENTIAL_ID_BYTES)
        for name in ("id", "rawId")
    ):
        return False
    if not all(
        _text_within(response.get(name), cap) for name, cap in response_caps.items()
    ):
        return False
    handle = response.get("userHandle")
    return handle is None or _text_within(handle, _MAX_USER_HANDLE_BYTES)


_REGISTRATION_CAPS = {
    "clientDataJSON": _MAX_CLIENT_DATA_BYTES,
    "attestationObject": _MAX_ATTESTATION_OBJECT_BYTES,
}
_AUTHENTICATION_CAPS = {
    "clientDataJSON": _MAX_CLIENT_DATA_BYTES,
    "authenticatorData": _MAX_AUTHENTICATOR_DATA_BYTES,
    "signature": _MAX_SIGNATURE_BYTES,
}


def _attestation_shape_ok(text: str) -> bool:
    """Whether the attestation object is a CBOR map with a text ``fmt``, a
    byte string ``authData``, and a map ``attStmt``. fido2 builds the
    authenticator data with ``bytes()``, so an integer there would make
    memory in proportion to its value. The text is within its cap, and
    the decoder's errors (RecursionError included) read as malformed."""
    try:
        attestation = cbor.decode(_websafe_decode(text))
    except (binascii.Error, *_DECODE_ERRORS):
        return False
    return (
        isinstance(attestation, dict)
        and isinstance(attestation.get("fmt"), str)
        and isinstance(attestation.get("authData"), bytes)
        and isinstance(attestation.get("attStmt"), dict)
    )


def _parse_registration(credential: object) -> RegistrationResponse:
    """The registration response, or allauth's ``incorrect_code``.

    allauth catches only TypeError, so a mismatched ``id`` or an
    attestation object that does not decode was a 500, and fido2 asserts
    that the authenticator data holds the attested credential data.
    """
    if (
        not isinstance(credential, dict)
        or not _binary_members_ok(credential, _REGISTRATION_CAPS)
        or not _attestation_shape_ok(credential["response"]["attestationObject"])
    ):
        raise _incorrect()
    try:
        parsed: RegistrationResponse = RegistrationResponse.from_dict(credential)
        auth_data = parsed.response.attestation_object.auth_data
    except _DECODE_ERRORS as error:
        raise _incorrect() from error
    if auth_data.credential_data is None:
        raise _incorrect()
    return parsed


def _parse_authentication(credential: object) -> AuthenticationResponse:
    """The authentication response, or allauth's ``incorrect_code``."""
    if not isinstance(credential, dict) or not _binary_members_ok(
        credential, _AUTHENTICATION_CAPS
    ):
        raise _incorrect()
    try:
        parsed: AuthenticationResponse = AuthenticationResponse.from_dict(credential)
    except _DECODE_ERRORS as error:
        raise _incorrect() from error
    return parsed


# What fido2's COSE keys raise when a stored key is malformed (a missing
# or non-bytes coordinate, an unsupported curve or algorithm) as they
# build the cryptography key to verify a signature.
_KEY_ERRORS = (
    KeyError,
    TypeError,
    ValueError,
    AttributeError,
    IndexError,
    OverflowError,
    NotImplementedError,
    UnsupportedAlgorithm,
    struct.error,
)


def _public_key_usable(public_key: CoseKey) -> bool:
    """Whether fido2 can verify signatures with ``public_key``.

    It is tried on a signature that cannot be valid: a well formed key
    answers InvalidSignature, a malformed one raises one of _KEY_ERRORS.
    """
    try:
        public_key.verify(b"\0", b"\0")
    except InvalidSignature:
        return True
    except _KEY_ERRORS:
        return False
    return False


def _complete_authentication(account: "_User", credential: Any) -> Authenticator:
    """allauth's assertion check, with a stored malformed public key
    refused as ``incorrect_code`` instead of a 500."""
    try:
        authenticator: Authenticator = webauthn_auth.complete_authentication(
            account, credential
        )
    except _KEY_ERRORS as error:
        raise _incorrect() from error
    return authenticator


def _extension_results_ok(credential: dict[str, Any]) -> bool:
    """Whether the client extension results are what allauth reads: an
    object when present, ``credProps`` an object when present, and its
    ``rk`` a boolean when present (allauth's ``is_passwordless``)."""
    if "clientExtensionResults" not in credential:
        return True
    results = credential["clientExtensionResults"]
    if not isinstance(results, dict):
        return False
    if "credProps" not in results:
        return True
    props = results["credProps"]
    if not isinstance(props, dict):
        return False
    return "rk" not in props or isinstance(props["rk"], bool)


def _registered_with_id(credential_id: str) -> QuerySet[Authenticator]:
    found: QuerySet[Authenticator] = Authenticator.objects.filter(
        type=Authenticator.Type.WEBAUTHN, data__credential__id=credential_id
    )
    return found


class CredentialField(forms.JSONField):
    """allauth's credential field, taking only a JSON object.

    The headless clients send the credential as an object. Django's
    JSONField would parse a string with ``json.loads`` and catch only
    JSONDecodeError, so a string of deeply nested JSON (RecursionError)
    or a long run of digits (ValueError) was a 500. A missing or empty
    value (null, ``{}``, ``[]``, ``""``) gets Django's ``required``, as
    allauth's field gave; any other value that is not an object gets
    allauth's ``incorrect_code``.
    """

    def to_python(self, value: Any) -> Any:
        if value in self.empty_values:
            return None
        if not isinstance(value, dict):
            raise _incorrect()
        return value


class _ChecksOrigin:
    """Check the credential's origin and shape before allauth verifies
    it. allauth's parser lets a KeyError or AttributeError through (a
    500), so OPEN's parser reads it first."""

    cleaned_data: dict[str, Any]

    def clean_credential(self) -> Any:
        credential = self.cleaned_data.get("credential")
        check_origin(credential)
        _parse_authentication(credential)
        try:
            return super().clean_credential()  # type: ignore[misc]
        except _KEY_ERRORS as error:
            # A stored passkey with a malformed public key.
            raise _incorrect() from error


class AddPasskeyInput(AddWebAuthnInput):  # type: ignore[misc]
    """allauth's add input with OPEN's checks, each refused with
    allauth's ``incorrect_code`` before allauth verifies the credential:
    an allowed origin and no cross-origin frame (``check_origin``), a
    registration within the size caps that decodes, attested credential
    data whose id is the credential's ``id`` and ``rawId``, a public key
    fido2 can verify with, well formed extension results, and a
    credential id no Account holds yet (WebAuthn's registration step that
    allauth leaves out), so passkey login finds one passkey by its id.
    The name, when sent, is text of at most 100 characters, as a rename
    takes."""

    credential = CredentialField(required=True)

    name = inputs.CharField(required=False, max_length=100)

    def clean_name(self) -> str:
        sent = self.data.get("name")
        if sent is not None and not isinstance(sent, str):
            raise ValidationError(PASSKEY_NAME_NOT_TEXT, code="invalid")
        name: str = super().clean_name()
        return name

    def clean_credential(self) -> Any:
        credential = self.cleaned_data.get("credential")
        check_origin(credential)
        response = _parse_registration(credential)
        if not _extension_results_ok(credential):
            raise _incorrect()
        attested = response.response.attestation_object.auth_data.credential_data
        if attested is None or not _public_key_usable(attested.public_key):
            raise _incorrect()
        # The stored JSON id must be the attested credential id, in
        # canonical base64url, since the uniqueness check and the login
        # lookup read it.
        canonical = websafe_encode(attested.credential_id)
        if credential.get("id") != canonical or response.raw_id != (
            attested.credential_id
        ):
            raise _incorrect()
        if _registered_with_id(canonical).exists():
            raise _incorrect()
        return credential


class AuthenticatePasskeyInput(_ChecksOrigin, AuthenticateWebAuthnInput):  # type: ignore[misc]
    """allauth's MFA login stage input, from an allowed origin only."""

    credential = CredentialField(required=True)


class ReauthenticatePasskeyInput(_ChecksOrigin, ReauthenticateWebAuthnInput):  # type: ignore[misc]
    """allauth's passkey reauthentication input, from an allowed origin
    only."""

    credential = CredentialField(required=True)


class LoginPasskeyInput(LoginWebAuthnInput):  # type: ignore[misc]
    """Passkey login, finding the Account by the credential, not by
    allauth's row id user handle.

    The passkey is the one WebAuthn Authenticator with the assertion's
    credential id. A user handle, when sent, must be that Account's
    keyed handle. allauth then verifies the assertion against the
    Account's passkeys and requires user verification, with no per
    Account rate limit (the Account comes from the request).
    """

    credential = CredentialField(required=True)

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
        authenticator = _complete_authentication(account, credential)
        if (
            authenticator.pk != found[0].pk
            or not response.response.authenticator_data.is_user_verified()
        ):
            raise _incorrect()
        return authenticator


class UpdatePasskeyInput(inputs.Input):  # type: ignore[misc]
    """A rename: a public passkey id from the player's list and a name,
    text of at most 100 characters. Any other id is refused with one
    fixed message, and a name that is not text with another."""

    name = inputs.CharField(required=True, max_length=100)

    def __init__(self, *args: Any, user: "_User", **kwargs: Any) -> None:
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_name(self) -> str:
        if not isinstance(self.data.get("name"), str):
            raise ValidationError(PASSKEY_NAME_NOT_TEXT, code="invalid")
        name: str = self.cleaned_data["name"]
        return name

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
