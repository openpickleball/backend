"""An app token whose session expired in the store loses its row (#100
review).

The session store's own expiry is the instant the lifetime check
compares, so in real time the store refuses an ended session first: the
token then names an empty session, and the lifetime check never sees it.
The token strategy must still delete the token's user session row (its
IP address and user agent). Sessions are aged here the way time ages
them: the stored times and the store's expiry together. Made-up data
only.
"""

import time
from datetime import UTC, datetime

import pytest
from allauth.account.models import EmailAddress
from allauth.usersessions.models import UserSession
from django.contrib.sessions.models import Session

from apps.accounts.sessions import SESSION_SEEN_KEY, SESSION_STARTED_KEY
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import APP, PASSWORD, HeadlessSession, app_headers

pytestmark = pytest.mark.django_db

EMAIL = "natural.expiry@example.org"
DAY = 86400


def _expire_naturally(session: HeadlessSession) -> str:
    store = session.store()
    key = store.session_key
    assert key
    now = int(time.time())
    store[SESSION_STARTED_KEY] = now - 31 * DAY
    store[SESSION_SEEN_KEY] = now - DAY
    store.set_expiry(datetime.fromtimestamp(now - 60, tz=UTC))
    store.save()
    assert Session.objects.get(session_key=key).expire_date.timestamp() < now
    return key


@pytest.mark.parametrize("path", ["allauth", "api"])
def test_an_expired_token_loses_its_row(path: str) -> None:
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)
    session = HeadlessSession(APP)
    assert session.login(EMAIL).status_code == 200
    key = _expire_naturally(session)

    if path == "allauth":
        status = session.get("auth/session").status_code
    else:
        status = session.client.get(
            "/api/v1/accounts/me", headers=app_headers(session.token)
        ).status_code

    assert status == 401
    assert not UserSession.objects.filter(session_key=key).exists()
