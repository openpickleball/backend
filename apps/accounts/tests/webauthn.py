"""A software passkey for tests of WebAuthn (issue #117).

It plays the browser and the authenticator together: it takes allauth's
creation or request options and returns the JSON a browser's WebAuthn
call would give, signed with its own made-up ES256 key. Each call can
name the origin and relying party ID it claims, so tests can send a
credential from a disallowed origin. No real authenticator or key is
used.
"""

import os
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fido2.cose import ES256
from fido2.utils import sha256, websafe_decode, websafe_encode
from fido2.webauthn import (
    AttestationObject,
    AttestedCredentialData,
    AuthenticatorData,
    CollectedClientData,
)

_FLAG = AuthenticatorData.FLAG


def _new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@dataclass
class SoftwarePasskey:
    """One made-up credential, as a platform authenticator holds it."""

    rp_id: str
    origin: str
    key: ec.EllipticCurvePrivateKey = field(default_factory=_new_key)
    credential_id: bytes = field(default_factory=lambda: os.urandom(32))
    user_handle: bytes | None = None
    counter: int = 0

    @property
    def id(self) -> str:
        """The credential id, base64url as the browser sends it."""
        return websafe_encode(self.credential_id)

    def _flags(self, user_verified: bool) -> AuthenticatorData.FLAG:
        flags = _FLAG.UP
        if user_verified:
            flags |= _FLAG.UV
        return flags

    def register(
        self,
        creation_options: dict[str, Any],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        user_verified: bool = True,
        resident: bool = True,
    ) -> dict[str, Any]:
        """The registration response for allauth's creation options.

        Keeps the user handle the options name, as an authenticator does.
        """
        options = creation_options["publicKey"]
        self.user_handle = websafe_decode(options["user"]["id"])
        public_key = ES256.from_cryptography_key(  # type: ignore[no-untyped-call]
            self.key.public_key()
        )
        attested = AttestedCredentialData.create(
            b"\0" * 16, self.credential_id, public_key
        )
        auth_data = AuthenticatorData.create(
            sha256((rp_id or self.rp_id).encode()),
            self._flags(user_verified) | _FLAG.AT,
            self.counter,
            attested,
        )
        attestation = AttestationObject.create("none", auth_data, {})
        client_data = CollectedClientData.create(
            "webauthn.create",
            websafe_decode(options["challenge"]),
            origin or self.origin,
        )
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": websafe_encode(client_data),
                "attestationObject": websafe_encode(attestation),
            },
            "clientExtensionResults": {"credProps": {"rk": resident}},
        }

    def sign_in(
        self,
        request_options: dict[str, Any],
        *,
        origin: str | None = None,
        rp_id: str | None = None,
        user_verified: bool = True,
        user_handle: bytes | None = None,
        send_user_handle: bool = True,
    ) -> dict[str, Any]:
        """The authentication response for allauth's request options.

        ``user_handle`` replaces the one kept at registration, and
        ``send_user_handle`` False leaves it out.
        """
        options = request_options["publicKey"]
        self.counter += 1
        auth_data = AuthenticatorData.create(
            sha256((rp_id or self.rp_id).encode()),
            self._flags(user_verified),
            self.counter,
        )
        client_data = CollectedClientData.create(
            "webauthn.get",
            websafe_decode(options["challenge"]),
            origin or self.origin,
        )
        signature = self.key.sign(
            bytes(auth_data) + client_data.hash, ec.ECDSA(hashes.SHA256())
        )
        response: dict[str, Any] = {
            "clientDataJSON": websafe_encode(client_data),
            "authenticatorData": websafe_encode(auth_data),
            "signature": websafe_encode(signature),
        }
        handle = user_handle if user_handle is not None else self.user_handle
        if send_user_handle and handle is not None:
            response["userHandle"] = websafe_encode(handle)
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": response,
            "clientExtensionResults": {},
        }
