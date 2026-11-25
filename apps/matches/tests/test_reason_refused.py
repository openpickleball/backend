"""A refused reason has its own error class, and the admin catches only it (#40).

``ReasonRefused`` (a ``ValueError``) is what the workflow raises when an
actor who is not acting as moderator gives a reason. The admin turns only
that, and ``MatchActionDenied``, into the fixed "not allowed" text; any
other ``ValueError`` is not hidden behind it. Made-up data only.
"""

from datetime import date
from typing import Any

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.tests.factories import EventFactory
from apps.matches import admin as matches_admin
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.permissions import MODERATOR_GROUP
from apps.matches.services import record_match
from apps.matches.workflow import ReasonRefused, void_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 1, 17)
REASON = "Made-up reason."


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


def _event_match(players: list[Player], admin: User) -> Match:
    """A confirmed match of an Event whose verified Organizer ``admin`` runs."""
    organizer = verified_organizer()
    OrganizerMemberFactory.create(
        organizer=organizer, account=admin, role=MemberRole.ADMIN
    )
    event = EventFactory.create(organizer=organizer)
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=PLAYED_ON,
        submitted_by_user=admin,
        event=event,
    ).match


def test_reason_refused_is_a_value_error() -> None:
    assert issubclass(ReasonRefused, ValueError)


def test_an_organizer_admin_giving_a_reason_gets_reason_refused(
    players: list[Player],
) -> None:
    organizer_admin: User = UserFactory.create()
    match = _event_match(players, organizer_admin)

    with pytest.raises(ReasonRefused, match=r"Only a moderator may give a reason\."):
        void_match(match, actor=organizer_admin, reason=REASON)

    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED


@pytest.fixture
def moderator_client(client: Client) -> Client:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    client.force_login(account)
    return client


def _raise_other(*args: Any, **kwargs: Any) -> Any:
    raise ValueError("other")


def test_another_value_error_in_an_action_is_not_hidden(
    moderator_client: Client,
    players: list[Player],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _event_match(players, UserFactory.create())
    monkeypatch.setattr(matches_admin, "void_match", _raise_other)

    with pytest.raises(ValueError, match="other"):
        moderator_client.post(
            reverse("admin:matches_match_changelist") + "?review=all",
            {
                "action": "void_selected",
                "_selected_action": [str(match.pk)],
                "reason": REASON,
            },
        )


def test_another_value_error_on_the_edit_page_is_not_hidden(
    moderator_client: Client,
    players: list[Player],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match = _event_match(players, UserFactory.create())
    monkeypatch.setattr(matches_admin, "edit_match", _raise_other)

    with pytest.raises(ValueError, match="other"):
        moderator_client.post(
            reverse("admin:matches_match_edit", args=[match.pk]),
            {
                "reason": REASON,
                "played_on": PLAYED_ON.isoformat(),
                "team_a": " ".join(p.public_code for p in players[:2]),
                "team_b": " ".join(p.public_code for p in players[2:]),
                "games": "11-2\n11-3",
            },
        )
