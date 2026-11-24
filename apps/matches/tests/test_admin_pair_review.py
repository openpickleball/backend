"""Selecting both sides of a flagged pair reports the second as reviewed (#40).

The first match's action closes the shared flag, so the second has nothing
left to do. That is not a failure: the moderator sees an info message, not
a "nothing to approve" warning. Made-up data only.
"""

from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.matches.models import Match, MatchFlag, MatchFormat
from apps.matches.permissions import MODERATOR_GROUP
from apps.matches.services import record_match
from apps.matches.workflow import confirm_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up pair review reason."


def _confirmed(players: list[Player], games: tuple[tuple[int, int], ...]) -> Match:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=date(2026, 3, 7),
        submitted_by_user=players[0].account,
    ).match
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[2].account)


@pytest.fixture
def moderator_client(client: Client) -> Client:
    account = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    client.force_login(account)
    return client


@pytest.mark.parametrize(
    ("action", "verb"),
    [("approve_selected", "approve"), ("dismiss_flags_selected", "dismiss")],
)
def test_both_sides_of_a_pair_get_no_failure_warning(
    moderator_client: Client, action: str, verb: str
) -> None:
    players = [PlayerFactory.create() for _ in range(4)]
    earlier = _confirmed(players, ((11, 7), (11, 9)))
    later = _confirmed(players, ((11, 3), (11, 2)))
    assert MatchFlag.objects.filter(match=later, duplicate_of=earlier).exists()

    response = moderator_client.post(
        reverse("admin:matches_match_changelist") + "?review=all",
        {
            "action": action,
            "_selected_action": [str(earlier.pk), str(later.pk)],
            "reason": REASON,
        },
        follow=True,
    )

    messages = list(get_messages(response.wsgi_request))
    texts = [str(m) for m in messages]
    assert not any(f"nothing to {verb}" in text for text in texts)
    assert not any(m.level_tag == "warning" for m in messages)
    assert f"{later.public_code} was already reviewed." in texts
    assert earlier.is_counted()
    assert later.is_counted()


def test_a_match_that_never_needed_review_still_warns(
    moderator_client: Client,
) -> None:
    players = [PlayerFactory.create() for _ in range(4)]
    plain = _confirmed(players, ((11, 7), (11, 9)))

    response = moderator_client.post(
        reverse("admin:matches_match_changelist") + "?review=all",
        {
            "action": "approve_selected",
            "_selected_action": [str(plain.pk)],
            "reason": REASON,
        },
        follow=True,
    )

    texts = [str(m) for m in get_messages(response.wsgi_request)]
    assert f"{plain.public_code} was not changed: nothing to approve." in texts
