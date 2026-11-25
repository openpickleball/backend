"""A new match stores the Event's kind read under the share lock (#163).

``draft_match`` reads the kind before the write transaction. A kind change
that commits in between must reach a match ``record_match`` records: only
``record_match`` reads the kind again after ``hold_event``'s share lock. A
correction (``edit_match``) copies the type of the match it corrects
instead, so the same kind change never reaches it (the maintainer's
decision on #163). The change here is a raw write made just before the
lock, so it lands exactly in that gap. Made-up data only.
"""

from datetime import date
from typing import Any

import pytest
from django.db.models import QuerySet

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches import services, workflow
from apps.matches.models import Match, MatchFormat, MatchType
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
    made: Event = EventFactory.create(
        organizer=verified_organizer(), kind=EventKind.LEAGUE
    )
    return made


def _member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _change_kind_before_hold(
    monkeypatch: pytest.MonkeyPatch, module: Any, event: Event
) -> None:
    real = module.hold_event

    def wrapped(*args: Any, **kwargs: Any) -> None:
        QuerySet(model=Event).filter(pk=event.pk).update(kind=EventKind.TOURNAMENT)
        real(*args, **kwargs)

    monkeypatch.setattr(module, "hold_event", wrapped)


def _stored_type(match: Match) -> str:
    stored: str = Match.objects.values_list("match_type", flat=True).get(pk=match.pk)
    return stored


def test_record_match_stores_the_kind_read_under_the_lock(
    monkeypatch: pytest.MonkeyPatch, players: list[Player], event: Event
) -> None:
    _change_kind_before_hold(monkeypatch, services, event)

    match = record_match(
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, 7),),
        played_on=PLAYED_ON,
        submitted_by_user=_member(event),
        event=event,
    ).match

    assert _stored_type(match) == MatchType.TOURNAMENT


def test_edit_match_keeps_the_old_match_type_over_a_kind_change(
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
    _change_kind_before_hold(monkeypatch, workflow, event)

    correction = edit_match(
        original,
        actor=account,
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, 3),),
        played_on=PLAYED_ON,
        as_moderator=False,
    )

    assert _stored_type(original) == MatchType.LEAGUE
    assert _stored_type(correction) == MatchType.LEAGUE
