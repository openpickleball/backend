"""A password reset that wipes the last method tells the other admins
(#232 after #117).

A reset from a key removes every passkey (ADR 0056) and is never
refused. When those passkeys were the Account's last method, the other
admins of its Organizers and Clubs get the "removed their last
two-factor method" notice, as a removal through the passkey view sends.
Made-up data only.
"""

from typing import Any

import pytest
from django.core import mail
from django.core.cache import cache

from apps.accounts.models import User
from apps.accounts.tests.factories import enroll_totp
from apps.accounts.tests.headless import (
    BROWSER,
    NEW_PASSWORD,
    RESET_PREFIX,
    HeadlessSession,
    key_from_link,
    link_in_last_mail,
)
from apps.accounts.tests.passkey_flows import (
    added,
    new_passkey,
    passkeys,
    use_passkey_settings,
    verified_account,
)
from apps.clubs.models import MemberRole, Organizer
from apps.clubs.tests.factories import OrganizerFactory, OrganizerMemberFactory
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

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


def _reset(account: User) -> Any:
    session = HeadlessSession(BROWSER)
    response = session.post("auth/password/request", {"email": account.email})
    assert response.status_code == 200
    key = key_from_link(link_in_last_mail(RESET_PREFIX), RESET_PREFIX)
    return session.post("auth/password/reset", {"key": key, "password": NEW_PASSWORD})


def _notice_recipients() -> set[str]:
    return {
        to
        for message in mail.outbox
        if str(message.subject).startswith(NOTICE_SUBJECT)
        for to in message.to
    }


def test_a_reset_that_wipes_the_last_passkey_tells_the_other_admins() -> None:
    organizer = OrganizerFactory.create()
    other = _admin(organizer)
    account = verified_account()
    added(BROWSER, account, new_passkey())
    _admin(organizer, account, mfa=False)
    mail.outbox.clear()

    response = _reset(account)

    assert response.status_code in (200, 401), response.content
    assert passkeys(account) == []
    assert _notice_recipients() == {other.email}


def test_a_reset_that_leaves_totp_sends_no_notice() -> None:
    organizer = OrganizerFactory.create()
    _admin(organizer)
    account = verified_account()
    added(BROWSER, account, new_passkey())
    enroll_totp(account)
    _admin(organizer, account)
    mail.outbox.clear()

    _reset(account)

    assert passkeys(account) == []
    assert _notice_recipients() == set()
