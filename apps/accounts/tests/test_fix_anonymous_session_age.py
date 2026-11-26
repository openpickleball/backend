"""A signed-out session keeps Django's two-week age (#100 review).

Raising ``SESSION_COOKIE_AGE`` to the signed-in lifetime lengthened only
signed-out sessions, which hold the signup details (legal name, birth
year and month) and pending TOTP secrets. Signed-in sessions set their
own expiry, so the cookie age stays at 14 days. Made-up data only.
"""

import time

import pytest
from django.conf import settings
from django.contrib.sessions.models import Session

from apps.accounts.tests.headless import BROWSER, HeadlessSession

pytestmark = pytest.mark.django_db

DAY = 86400


def test_the_cookie_age_is_two_weeks() -> None:
    assert settings.SESSION_COOKIE_AGE == 14 * DAY


def test_a_signup_hold_expires_within_two_weeks() -> None:
    session = HeadlessSession(BROWSER)
    session.signup(email="anonymous.age@example.org")
    key = session.store().session_key

    expiry = Session.objects.get(session_key=key).expire_date.timestamp()

    assert expiry <= time.time() + 14 * DAY + 5
