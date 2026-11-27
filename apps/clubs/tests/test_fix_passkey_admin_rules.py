"""Passkey removal follows the last MFA admin rules (#232 after #117).

A passkey is a method (ADR 0056). Removing passkeys so the Account has
no method left, while it is an admin of an Organizer with no other
active, unheld admin with MFA, gets 409 ``last_mfa_admin`` and removes
and records nothing, as removing TOTP does. A removal that leaves no
method tells the other admins. Removing one method while the other
remains is allowed and sends nothing. A passkey reauthentication counts
as the step-up's second factor step. Made-up data only.
"""

import json
from typing import Any

import pytest
from allauth.account.authentication import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.mfa.models import Authenticator
from django.core import mail
from django.core.cache import cache

from apps.accounts.models import User
from apps.accounts.passkeys import public_passkey_id
from apps.accounts.tests.factories import enroll_totp
from apps.accounts.tests.headless import BROWSER, HeadlessSession, csrf_header
from apps.accounts.tests.passkey_flows import (
    PASSKEY_PATH,
    REAUTHENTICATE_PATH,
    added,
    changes,
    new_passkey,
    passkeys,
    request_options,
    use_passkey_settings,
    verified_account,
)
from apps.accounts.tests.webauthn import SoftwarePasskey
from apps.clubs.models import MemberRole, Organizer
from apps.clubs.tests.factories import OrganizerFactory, OrganizerMemberFactory
from apps.players.tests.factories import PlayerFactory
from config.auth import STEP_UP_MESSAGE

pytestmark = pytest.mark.django_db

REFUSED_CODE = "last_mfa_admin"
NOTICE_SUBJECT = "Security notice"


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any, commit_after_each_request: None) -> None:
    use_passkey_settings(settings)
    cache.clear()


def _admin(organizer: Organizer, account: User | None = None, **kwargs: Any) -> User:
    account = account or verified_account()
    PlayerFactory.create(account=account)
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN, **kwargs
    )
    return account


def _passkey_admin(
    organizer: Organizer, passkey: SoftwarePasskey
) -> tuple[User, HeadlessSession]:
    """An admin whose only method is ``passkey``, with a session that just
    added it (a recent reauthentication and an MFA step)."""
    account = verified_account()
    session = added(BROWSER, account, passkey)
    _admin(organizer, account, mfa=False)
    return account, session


def _remove_passkeys(session: HeadlessSession, account: User) -> Any:
    ids = [public_passkey_id(row) for row in passkeys(account)]
    return session.delete_json(PASSKEY_PATH, {"authenticators": ids})


def _notices() -> list[Any]:
    return [m for m in mail.outbox if str(m.subject).startswith(NOTICE_SUBJECT)]


def test_the_last_mfa_admin_cannot_remove_its_last_passkey() -> None:
    organizer = OrganizerFactory.create()
    _admin(organizer, mfa=False)
    account, session = _passkey_admin(organizer, new_passkey())
    before = changes(account)

    response = _remove_passkeys(session, account)

    assert response.status_code == 409
    assert [e["code"] for e in response.json()["errors"]] == [REFUSED_CODE]
    assert len(passkeys(account)) == 1
    assert changes(account) == before


def test_removing_the_last_passkey_tells_the_other_admins() -> None:
    organizer = OrganizerFactory.create()
    other = _admin(organizer)
    account, session = _passkey_admin(organizer, new_passkey())
    mail.outbox.clear()

    response = _remove_passkeys(session, account)

    assert response.status_code == 200, response.content
    assert {to for m in _notices() for to in m.to} == {other.email}


def test_removing_a_passkey_while_totp_remains_is_allowed_and_silent() -> None:
    organizer = OrganizerFactory.create()
    _admin(organizer, mfa=False)
    account, session = _passkey_admin(organizer, new_passkey())
    enroll_totp(account)
    mail.outbox.clear()

    response = _remove_passkeys(session, account)

    assert response.status_code == 200, response.content
    assert _notices() == []


def test_removing_totp_while_a_passkey_remains_is_allowed_and_silent() -> None:
    organizer = OrganizerFactory.create()
    _admin(organizer, mfa=False)
    account, session = _passkey_admin(organizer, new_passkey())
    enroll_totp(account)
    mail.outbox.clear()

    response = session.delete("account/authenticators/totp")

    assert response.status_code == 200, response.content
    assert Authenticator.objects.filter(user=account, type="webauthn").exists()
    assert _notices() == []


def test_a_passkey_reauthentication_satisfies_the_step_up() -> None:
    organizer = OrganizerFactory.create()
    passkey = new_passkey()
    account, session = _passkey_admin(organizer, passkey)
    store = session.store()
    records = store[AUTHENTICATION_METHODS_SESSION_KEY]
    for record in records:
        record["at"] -= 10_000
    store[AUTHENTICATION_METHODS_SESSION_KEY] = records
    store.save()
    path = f"/api/v1/organizer/organizers/{organizer.public_code}/verification-request"
    body = json.dumps({"statement": "A made-up weekly ladder."})

    refused = session.client.post(
        path, body, content_type="application/json", headers=csrf_header(session.client)
    )
    assert refused.status_code == 403
    assert refused.json()["message"] == STEP_UP_MESSAGE

    credential = passkey.sign_in(request_options(session, REAUTHENTICATE_PATH))
    again = session.post(REAUTHENTICATE_PATH, {"credential": credential})
    assert again.status_code == 200, again.content
    done = session.client.post(
        path, body, content_type="application/json", headers=csrf_header(session.client)
    )

    assert done.status_code == 201, done.content
    assert account.pk
