"""Session and app token lifetimes (#100, ADR 0047).

A signed-in session, a browser's cookie session or an app's session
token, ends ``SESSION_MAX_AGE_DAYS`` after login or ``SESSION_IDLE_DAYS``
after it was last seen, whichever comes first, on every path: allauth's
endpoints and the OPEN API. Last seen is written at most once per
``SESSION_SEEN_REFRESH_SECONDS``. An ended session is deleted with its
user session row. Made-up data only.
"""

import time
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.usersessions.models import UserSession
from django.conf import settings
from django.contrib.sessions.models import Session

from apps.accounts.sessions import SESSION_SEEN_KEY, SESSION_STARTED_KEY
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    PASSWORD,
    HeadlessSession,
    app_headers,
)

pytestmark = pytest.mark.django_db

EMAIL = "lifetime.player@example.org"
DAY = 86400
ME = "/api/v1/accounts/me"


@pytest.fixture(autouse=True)
def account() -> Any:
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)
    return account


def _signed_in(base: str) -> HeadlessSession:
    session = HeadlessSession(base)
    assert session.login(EMAIL).status_code == 200
    return session


def _key(session: HeadlessSession) -> str:
    key = session.store().session_key
    assert key
    return key


def _age(session: HeadlessSession, started: float, seen: float) -> None:
    """Move the session's start and last seen ``started`` and ``seen``
    seconds into the past."""
    store = session.store()
    now = time.time()
    store[SESSION_STARTED_KEY] = int(now - started)
    store[SESSION_SEEN_KEY] = int(now - seen)
    store.save()


def _allauth_status(session: HeadlessSession) -> int:
    return int(session.get("auth/session").status_code)


def _api_status(session: HeadlessSession) -> int:
    headers = app_headers(session.token) if session.base == APP else {}
    return int(session.client.get(ME, headers=headers).status_code)


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_a_login_starts_both_clocks(base: str) -> None:
    before = int(time.time())
    session = _signed_in(base)

    store = session.store()
    assert before <= store[SESSION_STARTED_KEY] <= time.time()
    assert store[SESSION_SEEN_KEY] == store[SESSION_STARTED_KEY]
    expiry = Session.objects.get(session_key=_key(session)).expire_date.timestamp()
    assert expiry <= store[SESSION_STARTED_KEY] + settings.SESSION_IDLE_DAYS * DAY + 1


def test_the_cookie_age_is_the_lifetime() -> None:
    assert settings.SESSION_COOKIE_AGE == settings.SESSION_MAX_AGE_DAYS * DAY


@pytest.mark.parametrize("base", [APP, BROWSER])
@pytest.mark.parametrize("path", ["allauth", "api"])
@pytest.mark.parametrize(
    ("started", "seen"),
    [
        (31 * DAY, 60),  # past the lifetime, though recently seen
        (8 * DAY, 7 * DAY + 60),  # idle too long
    ],
)
def test_an_expired_session_is_ended(
    base: str, path: str, started: int, seen: int
) -> None:
    session = _signed_in(base)
    key = _key(session)
    _age(session, started, seen)

    status = _allauth_status(session) if path == "allauth" else _api_status(session)

    if path == "api":
        assert status == 401
    else:
        # allauth answers an unknown app token with 410, a signed-out
        # browser session with 401.
        assert status == (410 if base == APP else 401)
    assert not Session.objects.filter(session_key=key).exists()
    assert not UserSession.objects.filter(session_key=key).exists()


@pytest.mark.parametrize("base", [APP, BROWSER])
@pytest.mark.parametrize("path", ["allauth", "api"])
def test_a_session_in_use_lives_on_and_last_seen_moves_hourly(
    base: str, path: str
) -> None:
    session = _signed_in(base)
    _age(session, 5 * DAY, 2 * 3600)
    old_seen = session.store()[SESSION_SEEN_KEY]

    status = _allauth_status(session) if path == "allauth" else _api_status(session)

    assert status == 200
    store = session.store()
    assert store[SESSION_SEEN_KEY] >= time.time() - 5
    assert store[SESSION_SEEN_KEY] > old_seen
    expiry = Session.objects.get(session_key=_key(session)).expire_date.timestamp()
    limit = min(
        store[SESSION_SEEN_KEY] + settings.SESSION_IDLE_DAYS * DAY,
        store[SESSION_STARTED_KEY] + settings.SESSION_MAX_AGE_DAYS * DAY,
    )
    assert abs(expiry - limit) <= 2


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_last_seen_is_not_written_within_the_hour(base: str) -> None:
    session = _signed_in(base)
    _age(session, 600, 600)
    seen = session.store()[SESSION_SEEN_KEY]

    assert _allauth_status(session) == 200
    assert _api_status(session) == 200

    assert session.store()[SESSION_SEEN_KEY] == seen


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_the_lifetime_caps_the_stored_expiry(base: str) -> None:
    session = _signed_in(base)
    _age(session, 29 * DAY, 2 * 3600)

    assert _allauth_status(session) == 200

    store = session.store()
    expiry = Session.objects.get(session_key=_key(session)).expire_date.timestamp()
    assert (
        expiry <= store[SESSION_STARTED_KEY] + settings.SESSION_MAX_AGE_DAYS * DAY + 1
    )


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_a_signed_in_session_without_clocks_gets_them(base: str) -> None:
    session = _signed_in(base)
    store = session.store()
    del store[SESSION_STARTED_KEY]
    del store[SESSION_SEEN_KEY]
    store.save()

    assert _allauth_status(session) == 200

    store = session.store()
    assert store[SESSION_STARTED_KEY] >= time.time() - 5
    assert store[SESSION_SEEN_KEY] == store[SESSION_STARTED_KEY]


def test_a_signed_out_session_gets_no_clocks() -> None:
    session = HeadlessSession(BROWSER)
    session.signup(email="new.lifetime@example.org")

    store = session.store()
    assert SESSION_STARTED_KEY not in store
    assert SESSION_SEEN_KEY not in store
