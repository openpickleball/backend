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
from fido2.cose import ES256, CoseKey
from fido2.utils import sha256, websafe_decode, websafe_encode
from fido2.webauthn import (
    AttestationObject,
    AttestedCredentialData,
    AuthenticatorData,
    CollectedClientData,
)

_FLAG = AuthenticatorData.FLAG


def _members(client_data: object) -> dict[str, Any]:
    """The extra client data members a test asked for, if any."""
    if client_data is None:
        return {}
    if not isinstance(client_data, dict):
        raise TypeError("client_data must be a dict")
    return {str(key): value for key, value in client_data.items()}


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

    def _cose_key(self) -> CoseKey:
        """The public key as the authenticator data carries it."""
        cose: CoseKey = ES256.from_cryptography_key(  # type: ignore[no-untyped-call]
            self.key.public_key()
        )
        return cose

    def _sign(self, message: bytes) -> bytes:
        return self.key.sign(message, ec.ECDSA(hashes.SHA256()))

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
        client_data: object = None,
    ) -> dict[str, Any]:
        """The registration response for allauth's creation options.

        Keeps the user handle the options name, as an authenticator does.
        ``client_data`` adds members to the client data (``crossOrigin``,
        ``topOrigin``).
        """
        options = creation_options["publicKey"]
        self.user_handle = websafe_decode(options["user"]["id"])
        public_key = self._cose_key()
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
        collected = CollectedClientData.create(
            "webauthn.create",
            websafe_decode(options["challenge"]),
            origin or self.origin,
            **_members(client_data),
        )
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "response": {
                "clientDataJSON": websafe_encode(collected),
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
        client_data: object = None,
    ) -> dict[str, Any]:
        """The authentication response for allauth's request options.

        ``user_handle`` replaces the one kept at registration, and
        ``send_user_handle`` False leaves it out. ``client_data`` adds
        members to the client data (``crossOrigin``, ``topOrigin``).
        """
        options = request_options["publicKey"]
        self.counter += 1
        auth_data = AuthenticatorData.create(
            sha256((rp_id or self.rp_id).encode()),
            self._flags(user_verified),
            self.counter,
        )
        collected = CollectedClientData.create(
            "webauthn.get",
            websafe_decode(options["challenge"]),
            origin or self.origin,
            **_members(client_data),
        )
        signature = self._sign(bytes(auth_data) + collected.hash)
        response: dict[str, Any] = {
            "clientDataJSON": websafe_encode(collected),
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


def mldsa87_available() -> bool:
    """Whether fido2 and cryptography here support ML-DSA-87 keys."""
    return -50 in CoseKey.supported_algorithms()


@dataclass
class MLDSA87Passkey(SoftwarePasskey):
    """A made-up post-quantum credential: an ML-DSA-87 key (a 2592-byte
    public key, 4627-byte signatures), as fido2 supports when the
    installed cryptography does."""

    mldsa_key: Any = None

    def __post_init__(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import mldsa

        if self.mldsa_key is None:
            self.mldsa_key = mldsa.MLDSA87PrivateKey.generate()

    def _cose_key(self) -> CoseKey:
        cose: CoseKey = CoseKey.for_alg(-50).from_cryptography_key(
            self.mldsa_key.public_key()
        )
        return cose

    def _sign(self, message: bytes) -> bytes:
        signature: bytes = self.mldsa_key.sign(message)
        return signature
