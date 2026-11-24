"""An over-long action reason gets a clear warning and changes nothing (#40).

Both the match and the organizer admin. The warning names the limit and
never repeats the reason. Made-up data only.
"""

from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.contrib.messages.storage.base import Message
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.tests.factories import OrganizerFactory
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.permissions import MODERATOR_GROUP
from apps.matches.services import record_match
from apps.matches.workflow import confirm_match
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

TOO_LONG = "Keep the reason to 1000 characters or fewer."
LONG_REASON = "Made-up " + "r" * 993


@pytest.fixture
def moderator_client(client: Client) -> Client:
    account = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    client.force_login(account)
    return client


def _post(client: Client, url: str, action: str, pk: int) -> list[Message]:
    response = client.post(
        url,
        {"action": action, "_selected_action": [str(pk)], "reason": LONG_REASON},
        follow=True,
    )
    assert response.status_code == 200
    return list(get_messages(response.wsgi_request))


def test_the_reason_is_over_the_limit() -> None:
    assert len(LONG_REASON) == 1001


@pytest.mark.parametrize(
    "action", ["approve_selected", "void_selected", "dismiss_flags_selected"]
)
def test_match_action_with_a_long_reason_warns_and_changes_nothing(
    moderator_client: Client, action: str
) -> None:
    players = [PlayerFactory.create() for _ in range(4)]
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=date(2026, 3, 8),
        submitted_by_user=players[0].account,
    ).match
    confirm_match(match, actor=players[0].account)
    confirm_match(match, actor=players[2].account)
    before = AuditEvent.objects.count()

    messages = _post(
        moderator_client,
        reverse("admin:matches_match_changelist") + "?review=all",
        action,
        match.pk,
    )

    assert [str(m) for m in messages] == [TOO_LONG]
    assert messages[0].level_tag == "warning"
    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED
    assert AuditEvent.objects.count() == before


@pytest.mark.parametrize("action", ["verify_selected", "unverify_selected"])
def test_organizer_action_with_a_long_reason_warns_and_changes_nothing(
    moderator_client: Client, action: str
) -> None:
    organizer = OrganizerFactory.create()

    messages = _post(
        moderator_client,
        reverse("admin:clubs_organizer_changelist"),
        action,
        organizer.pk,
    )

    assert [str(m) for m in messages] == [TOO_LONG]
    organizer.refresh_from_db()
    assert organizer.is_verified is False
    assert not AuditEvent.objects.exists()
