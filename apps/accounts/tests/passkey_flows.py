"""Steps of the passkey flows for tests (issue #117). Made-up data only.

Each test module that uses these calls ``use_passkey_settings`` from an
autouse fixture, so the relying party and the allowed origin match the
software passkey's.
"""

from typing import TYPE_CHECKING, Any

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import PASSWORD, HeadlessSession
from apps.accounts.tests.webauthn import SoftwarePasskey
from apps.audit.models import AuditAction, AuditEvent

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

RP_ID = "example.org"
PWA = "https://app.example.org"
PASSKEY_PATH = "account/authenticators/webauthn"
LOGIN_PATH = "auth/webauthn/login"
AUTHENTICATE_PATH = "auth/webauthn/authenticate"
REAUTHENTICATE_PATH = "auth/webauthn/reauthenticate"


def use_passkey_settings(settings: Any) -> None:
    settings.WEBAUTHN_RP_ID = RP_ID
    settings.WEBAUTHN_ALLOWED_ORIGINS = [PWA]
    settings.MFA_TOTP_TOLERANCE = 1


def new_passkey() -> SoftwarePasskey:
    return SoftwarePasskey(rp_id=RP_ID, origin=PWA)


def verified_account(**fields: Any) -> "_User":
    """A made-up Account with a verified login address."""
    account: _User = UserFactory.create(password=PASSWORD, **fields)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


def signed_in(base: str, account: "_User") -> HeadlessSession:
    """A session signed in by password and just reauthenticated."""
    session = HeadlessSession(base)
    assert session.login(account.email).status_code == 200
    assert session.reauthenticate().status_code == 200
    return session


def creation_options(session: HeadlessSession) -> dict[str, Any]:
    response = session.get(f"{PASSKEY_PATH}?passwordless")
    assert response.status_code == 200, response.content
    options: dict[str, Any] = response.json()["data"]["creation_options"]
    return options


def post_registration(
    session: HeadlessSession, credential: object, name: object = "Phone"
) -> Any:
    return session.post(
        PASSKEY_PATH, {"name": name, "credential": credential, "passwordless": True}
    )


def add(session: HeadlessSession, passkey: SoftwarePasskey, **register: Any) -> Any:
    credential = passkey.register(creation_options(session), **register)
    return post_registration(session, credential)


def added(base: str, account: "_User", passkey: SoftwarePasskey) -> HeadlessSession:
    """A signed-in session that added ``passkey`` to ``account``."""
    session = signed_in(base, account)
    response = add(session, passkey)
    assert response.status_code == 200, response.content
    return session


def request_options(session: HeadlessSession, path: str) -> dict[str, Any]:
    response = session.get(path)
    assert response.status_code == 200, response.content
    options: dict[str, Any] = response.json()["data"]["request_options"]
    return options


def error_codes(response: Any) -> list[str]:
    return [error["code"] for error in response.json()["errors"]]


def changes(account: "_User") -> list[Any]:
    return [
        event.after
        for event in AuditEvent.objects.filter(
            action=AuditAction.MFA_CHANGE, actor_user_id=account.pk
        ).order_by("pk")
    ]


def passkeys(account: "_User") -> list[Authenticator]:
    return list(
        Authenticator.objects.filter(
            user_id=account.pk, type=Authenticator.Type.WEBAUTHN
        ).order_by("pk")
    )
