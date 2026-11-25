"""The admin refuses a moderator's action on a match they played in (#40).

The refusal shows only the fixed "not allowed" text, never which rule
failed, and changes nothing. A moderator who did not play still acts.
Made-up data only.
"""

from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.contrib.messages.storage.base import Message
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.permissions import MODERATOR_GROUP, MatchActionDenied
from apps.matches.services import record_match
from apps.matches.workflow import confirm_match, dispute_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up moderator reason."
PLAYED_ON = date(2026, 2, 14)
DENIED = MatchActionDenied.message
OTHER_RULES = ("Only a moderator", "current status", "nothing to", "reason")


def _moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


@pytest.fixture
def playing_moderator() -> User:
    return _moderator()


@pytest.fixture
def players(playing_moderator: User) -> list[Player]:
    """Player 0 belongs to the moderator."""
    return [
        PlayerFactory.create(account=playing_moderator),
        *(PlayerFactory.create() for _ in range(3)),
    ]


def _confirmed(players: list[Player], **extra: object) -> Match:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
        **extra,  # type: ignore[arg-type]
    ).match
    if match.status == MatchStatus.PENDING:
        confirm_match(match, actor=players[0].account)
        confirm_match(match, actor=players[2].account)
    return Match.objects.get(pk=match.pk)


def _run(client: Client, action: str, match: Match) -> list[Message]:
    response = client.post(
        reverse("admin:matches_match_changelist") + "?review=all",
        {"action": action, "_selected_action": [str(match.pk)], "reason": REASON},
        follow=True,
    )
    assert response.status_code == 200
    return list(get_messages(response.wsgi_request))


def _assert_denied(messages: list[Message], match: Match) -> None:
    assert [str(m) for m in messages] == [
        f"{match.public_code} was not changed: {DENIED}"
    ]
    assert messages[0].level_tag == "warning"
    for wording in OTHER_RULES:
        assert wording not in str(messages[0])


def test_approve_a_disputed_match_they_played_in_is_refused(
    client: Client, playing_moderator: User, players: list[Player]
) -> None:
    match = _confirmed(players)
    dispute_match(match, actor=players[1].account)
    events = AuditEvent.objects.count()
    client.force_login(playing_moderator)

    messages = _run(client, "approve_selected", match)

    _assert_denied(messages, match)
    assert Match.objects.get(pk=match.pk).status == MatchStatus.DISPUTED
    assert AuditEvent.objects.count() == events


@pytest.mark.parametrize("disputed", [False, True])
def test_void_a_match_they_played_in_is_refused(
    client: Client, playing_moderator: User, players: list[Player], disputed: bool
) -> None:
    match = _confirmed(players)
    if disputed:
        dispute_match(match, actor=players[1].account)
    status = Match.objects.get(pk=match.pk).status
    events = AuditEvent.objects.count()
    client.force_login(playing_moderator)

    messages = _run(client, "void_selected", match)

    _assert_denied(messages, match)
    assert Match.objects.get(pk=match.pk).status == status
    assert AuditEvent.objects.count() == events


def _member_event(account: User) -> Event:
    """An Event of a verified Organizer that ``account`` is an admin of."""
    organizer = verified_organizer()
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    event: Event = EventFactory.create(organizer=organizer)
    return event


def test_void_with_event_authority_still_shows_only_the_fixed_text(
    client: Client, playing_moderator: User, players: list[Player]
) -> None:
    """The fallback path may allow the change but refuses the reason:
    the message is still the fixed denied text, never a 500."""
    match = _confirmed(players, event=_member_event(playing_moderator))
    events = AuditEvent.objects.count()
    client.force_login(playing_moderator)

    messages = _run(client, "void_selected", match)

    _assert_denied(messages, match)
    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED
    assert AuditEvent.objects.count() == events


@pytest.mark.parametrize("with_event", [False, True])
def test_edit_page_post_shows_the_fixed_text(
    client: Client, playing_moderator: User, players: list[Player], with_event: bool
) -> None:
    extra: dict[str, object] = {}
    if with_event:
        extra["event"] = _member_event(playing_moderator)
    match = _confirmed(players, **extra)
    events = AuditEvent.objects.count()
    client.force_login(playing_moderator)

    response = client.post(
        reverse("admin:matches_match_edit", args=[match.pk]),
        {
            "reason": REASON,
            "played_on": PLAYED_ON.isoformat(),
            "team_a": " ".join(p.public_code for p in players[:2]),
            "team_b": " ".join(p.public_code for p in players[2:]),
            "games": "11-2\n11-3",
        },
    )

    page = response.content.decode()
    assert response.status_code == 200
    assert DENIED in page
    assert "Only a moderator" not in page
    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED
    assert not Match.objects.filter(corrects=match).exists()
    assert AuditEvent.objects.count() == events


def test_a_moderator_who_did_not_play_still_succeeds(
    client: Client, players: list[Player]
) -> None:
    match = _confirmed(players)
    dispute_match(match, actor=players[1].account)
    client.force_login(_moderator())

    messages = _run(client, "approve_selected", match)

    assert [str(m) for m in messages] == ["1 match(es) approved."]
    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED
