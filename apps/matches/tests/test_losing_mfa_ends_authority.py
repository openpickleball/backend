"""Removing the last two-factor method ends Organizer authority at once
(#232, ADR 0057). Made-up data only.

After a member removes its TOTP through allauth's headless flow, it can
no longer submit, confirm, or change an Event match of its Organizer,
and its other session (one with a user session row) is gone.
"""

from datetime import date
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.usersessions.models import UserSession
from django.contrib.sessions.models import Session
from django.core.cache import cache

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, totp_code, totp_secret
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.permissions import (
    MatchSubmission,
    can_change_match,
    can_confirm_as_organizer,
    can_create_match,
)
from apps.matches.services import record_match
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _setup(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1
    cache.clear()


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_removing_totp_ends_authority_and_the_other_session(
    base: str, commit_after_each_request: None
) -> None:
    event = EventFactory.create(organizer=verified_organizer())
    member: User = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=member, email=member.email, verified=True, primary=True
    )
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    players = [PlayerFactory.create() for _ in range(4)]
    submission = MatchSubmission(
        event=event, club=None, player_ids=frozenset(p.pk for p in players)
    )
    recorded: Match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 3), (11, 4)),
        played_on=date(2026, 6, 2),
        submitted_by_user=member,
        event=event,
    ).match
    assert recorded.status == MatchStatus.CONFIRMED
    assert can_create_match(member, submission)
    assert can_change_match(member, recorded)
    assert can_confirm_as_organizer(member, recorded)
    secret = totp_secret(member)
    current = HeadlessSession(base)
    assert current.mfa_login(member.email, totp_code(secret)).status_code == 200
    other = HeadlessSession(APP)
    assert other.mfa_login(member.email, totp_code(secret, 1)).status_code == 200
    other_key = other.store().session_key
    assert UserSession.objects.filter(session_key=other_key).exists()
    assert current.reauthenticate().status_code == 200

    response = current.delete("account/authenticators/totp")

    assert response.status_code == 200
    assert not can_create_match(member, submission)
    assert not can_change_match(member, recorded)
    assert not can_confirm_as_organizer(member, recorded)
    assert not Session.objects.filter(session_key=other_key).exists()
    assert not UserSession.objects.filter(session_key=other_key).exists()
