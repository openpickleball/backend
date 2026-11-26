"""An organizer change during a submission is caught before the write (#163).

``record_match`` and ``edit_match`` check the submitter's authority before
validation, then write in a transaction. An Event's organizer that changes
in between must not let the match through: the write transaction holds
the Event row and checks the authority again. The change here is a raw
write, so it skips ``Event.save()``'s own lock and lands exactly between
the two checks. Made-up data only.
"""

from datetime import date
from typing import Any

import pytest
from django.db.models import QuerySet

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import services, workflow
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.permissions import MatchActionDenied, MatchPermissionDenied
from apps.matches.services import record_match
from apps.matches.workflow import edit_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(2)]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _move_event_after_first_call(
    monkeypatch: pytest.MonkeyPatch, module: Any, name: str, event: Event
) -> None:
    """Change the Event's organizer by a raw write right after the first check."""
    real = getattr(module, name)
    calls = {"count": 0}

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            QuerySet(model=Event).filter(pk=event.pk).update(
                organizer=verified_organizer()
            )
        return result

    monkeypatch.setattr(module, name, wrapped)


def test_record_match_refuses_when_the_organizer_changed_after_the_check(
    monkeypatch: pytest.MonkeyPatch, players: list[Player], event: Event
) -> None:
    account = _member(event)
    _move_event_after_first_call(monkeypatch, services, "check_can_create_match", event)

    with pytest.raises(MatchPermissionDenied):
        record_match(
            format=MatchFormat.SINGLES,
            team_a=players[:1],
            team_b=players[1:],
            games=((11, 7),),
            played_on=PLAYED_ON,
            submitted_by_user=account,
            event=event,
        )

    assert not Match.objects.exists()


def test_edit_match_refuses_when_the_organizer_changed_after_the_check(
    monkeypatch: pytest.MonkeyPatch, players: list[Player], event: Event
) -> None:
    account = _member(event)
    original = record_match(
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, 7),),
        played_on=PLAYED_ON,
        submitted_by_user=account,
        event=event,
    ).match
    _move_event_after_first_call(monkeypatch, workflow, "can_create_match", event)

    with pytest.raises(MatchActionDenied):
        edit_match(
            original,
            actor=account,
            format=MatchFormat.SINGLES,
            team_a=players[:1],
            team_b=players[1:],
            games=((11, 3),),
            played_on=PLAYED_ON,
        )

    assert list(Match.objects.values_list("pk", "status")) == [
        (original.pk, MatchStatus.CONFIRMED)
    ]
