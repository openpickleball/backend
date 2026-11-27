"""A session that turns short at a refresh is checked against the short
idle time from its old last seen (#232 review).

When a membership gained mid-session flips ``open_session_short`` at the
refresh, the idle check must use the member lifetimes with the last seen
time from before the refresh, so a session idle longer than the member
limit ends instead of surviving because last seen moved to now. Made-up
data only.
"""

import time

import pytest
from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore

from apps.accounts.sessions import (
    SESSION_SEEN_KEY,
    SESSION_SHORT_KEY,
    SESSION_STARTED_KEY,
    check_lifetime,
    start_lifetime,
)
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory

pytestmark = pytest.mark.django_db

DAY = 86400


def test_a_session_idle_past_the_member_limit_ends_when_it_turns_short() -> None:
    account = UserFactory.create()
    session = SessionStore()
    session[SESSION_KEY] = str(account.pk)
    start_lifetime(session)
    assert session[SESSION_SHORT_KEY] is False
    OrganizerMemberFactory.create(account=account)
    now = int(time.time())
    idle = DAY * (settings.ORGANIZER_SESSION_IDLE_DAYS + 2)
    assert idle < DAY * settings.SESSION_IDLE_DAYS
    session[SESSION_STARTED_KEY] = now - idle - 60
    session[SESSION_SEEN_KEY] = now - idle

    assert check_lifetime(session) is False
