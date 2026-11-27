"""Shorter sessions for Organizer members and Club admins (#232, ADR 0057).

An Account with any Organizer membership or Club Role uses
``ORGANIZER_SESSION_MAX_AGE_DAYS`` and ``ORGANIZER_SESSION_IDLE_DAYS``
instead of the player values. The session holds whether it is short
(``open_session_short``), computed at login and again whenever last
seen moves or the value is missing or not a bool, so a membership gained
mid-session applies at the next refresh. Made-up data only.
"""

import time
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client

from apps.accounts.models import User
from apps.accounts.sessions import (
    SESSION_SEEN_KEY,
    SESSION_SHORT_KEY,
    SESSION_STARTED_KEY,
    check_lifetime,
    start_lifetime,
)
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import ClubRoleFactory, OrganizerMemberFactory

pytestmark = pytest.mark.django_db

DAY = 86400
ME = "/api/v1/accounts/me"


def _account() -> User:
    account: User = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


def _session(account: User) -> SessionStore:
    session = SessionStore()
    session[SESSION_KEY] = str(account.pk)
    start_lifetime(session)
    session.save()
    return session


def _expires_in(session: SessionStore) -> float:
    return session.get_expiry_date().timestamp() - time.time()


def _age(session: SessionStore, *, started: int, seen: int) -> None:
    now = int(time.time())
    session[SESSION_STARTED_KEY] = now - started
    session[SESSION_SEEN_KEY] = now - seen


def test_a_member_session_is_short() -> None:
    account = _account()
    OrganizerMemberFactory.create(account=account)

    session = _session(account)

    assert session[SESSION_SHORT_KEY] is True
    expires = _expires_in(session)
    assert expires > DAY * settings.ORGANIZER_SESSION_IDLE_DAYS - 60
    assert expires <= DAY * settings.ORGANIZER_SESSION_IDLE_DAYS


def test_a_club_admin_session_is_short() -> None:
    account = _account()
    ClubRoleFactory.create(account=account)

    assert _session(account)[SESSION_SHORT_KEY] is True


def test_a_player_session_keeps_the_player_lifetime() -> None:
    session = _session(_account())

    assert session[SESSION_SHORT_KEY] is False
    assert _expires_in(session) > DAY * (settings.SESSION_IDLE_DAYS - 1)


def test_a_member_session_ends_after_the_short_idle_time() -> None:
    account = _account()
    OrganizerMemberFactory.create(account=account)
    session = _session(account)
    _age(session, started=DAY + 10, seen=DAY * settings.ORGANIZER_SESSION_IDLE_DAYS)

    assert check_lifetime(session) is False


def test_a_member_session_ends_after_the_short_lifetime() -> None:
    account = _account()
    OrganizerMemberFactory.create(account=account)
    session = _session(account)
    _age(session, started=DAY * settings.ORGANIZER_SESSION_MAX_AGE_DAYS, seen=60)

    assert check_lifetime(session) is False


def test_a_player_session_lasts_past_the_member_idle_time() -> None:
    session = _session(_account())
    _age(session, started=DAY + 10, seen=DAY + 10)

    assert check_lifetime(session) is True


def test_a_membership_gained_mid_session_applies_at_the_next_refresh() -> None:
    account = _account()
    session = _session(account)
    OrganizerMemberFactory.create(account=account)

    # Last seen is fresh: no refresh, so the stored value still holds.
    assert check_lifetime(session) is True
    assert session[SESSION_SHORT_KEY] is False

    _age(session, started=DAY, seen=settings.SESSION_SEEN_REFRESH_SECONDS)
    assert check_lifetime(session) is True
    assert session[SESSION_SHORT_KEY] is True
    assert _expires_in(session) <= DAY * settings.ORGANIZER_SESSION_IDLE_DAYS


@pytest.mark.parametrize("value", [..., "yes", 1, None])
def test_a_missing_or_odd_value_is_computed_again(value: Any) -> None:
    account = _account()
    OrganizerMemberFactory.create(account=account)
    session = _session(account)
    if value is ...:
        del session[SESSION_SHORT_KEY]
    else:
        session[SESSION_SHORT_KEY] = value
    _age(session, started=DAY + 10, seen=DAY * settings.ORGANIZER_SESSION_IDLE_DAYS)

    assert check_lifetime(session) is False
    assert session[SESSION_SHORT_KEY] is True


def test_the_api_refuses_a_member_session_past_its_idle_time(client: Client) -> None:
    account = _account()
    OrganizerMemberFactory.create(account=account)
    client.force_login(account)
    assert client.get(ME).status_code == 200
    stored = client.session
    now = int(time.time())
    stored[SESSION_STARTED_KEY] = now - DAY - 10
    stored[SESSION_SEEN_KEY] = now - DAY * settings.ORGANIZER_SESSION_IDLE_DAYS
    stored.save()

    assert client.get(ME).status_code == 401
