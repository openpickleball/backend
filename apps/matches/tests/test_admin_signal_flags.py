"""The match admin shows no unusual submission flag (#206, ADR 0049).

A signal flag holds nothing, so a match with only signal flags is not
"Disputed or held", and its change page lists no flag row. Moderators see
these flags only as counts in the Organizer review. Made-up data only.
"""

from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.tests.factories import EventFactory
from apps.matches.models import FlagReason, FlagRule, Match, MatchFormat
from apps.matches.permissions import MODERATOR_GROUP
from apps.matches.services import record_match
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

CHANGELIST = "admin:matches_match_changelist"


@pytest.fixture
def moderator_client(client: Client) -> Client:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    client.force_login(account)
    return client


@pytest.fixture
def flagged() -> Match:
    """An Organizer's first match: one first_matches flag, nothing else."""
    event = EventFactory.create(organizer=verified_organizer())
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    match = record_match(
        format=MatchFormat.SINGLES,
        team_a=[PlayerFactory.create()],
        team_b=[PlayerFactory.create()],
        games=((11, 7), (11, 4)),
        played_on=date(2026, 5, 2),
        event=event,
        submitted_by_user=member,
    ).match
    assert list(match.flags.values_list("reason", "rule")) == [
        (FlagReason.UNUSUAL_SUBMISSION, FlagRule.FIRST_MATCHES)
    ]
    return match


def test_a_signal_flag_is_not_held(moderator_client: Client, flagged: Match) -> None:
    every = moderator_client.get(reverse(CHANGELIST)).content.decode()
    held = moderator_client.get(reverse(CHANGELIST) + "?held=yes").content.decode()

    assert flagged.public_code in every
    assert flagged.public_code not in held
    assert "Unusual submission" not in every


def test_the_change_page_lists_no_signal_flag(
    moderator_client: Client, flagged: Match
) -> None:
    response = moderator_client.get(
        reverse("admin:matches_match_change", args=[flagged.pk])
    )

    page = response.content.decode()
    assert response.status_code == 200
    assert "Unusual submission" not in page
    assert "First matches" not in page
