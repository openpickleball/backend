"""A retry of a submission that confirmed a pending match is idempotent
(review finding E, #26).

A submitter whose exact duplicate confirmed a pending match gets the same
result again on a retry: that match, ``created=False``, with no new Audit
Event or signal. Any other submitter's exact duplicate is still refused.
Made-up data only.
"""

from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.services import DuplicateMatchError, RecordedMatch, record_match
from apps.matches.tests.conftest import SignalLog
from apps.partners.models import PartnerApplication
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    approved_application,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7),)


@pytest.fixture
def two_players() -> list[Player]:
    return [PlayerFactory.create(), PlayerFactory.create()]


@pytest.fixture
def event() -> Event:
    """An Event of a verified Organizer (ADR 0021: the trusted submitter)."""
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _record(players: Sequence[Player], **values: Any) -> RecordedMatch:
    return record_match(
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=GAMES,
        played_on=PLAYED_ON,
        **values,
    )


def _event_account(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _granted(event: Event) -> PartnerApplication:
    application = approved_application()
    OrganizerApplicationGrantFactory.create(
        organizer=event.organizer, application=application
    )
    return application


def _confirms(match: Match) -> int:
    return AuditEvent.objects.filter(
        action=AuditAction.MATCH_CONFIRM, target_object_id=str(match.pk)
    ).count()


def test_an_application_retry_returns_the_confirmed_match(
    two_players: list[Player], event: Event, signals: SignalLog
) -> None:
    pending = _record(two_players, submitted_by_user=two_players[0].account).match
    application = _granted(event)
    values: dict[str, Any] = {
        "submitted_by_application": application,
        "event": event,
        "external_id": "made-up-1",
    }
    first = _record(two_players, **values)

    retry = _record(two_players, **values)

    assert first.match.pk == pending.pk
    assert retry == RecordedMatch(match=retry.match, created=False)
    assert retry.match.pk == pending.pk
    assert Match.objects.get(pk=pending.pk).status == MatchStatus.CONFIRMED
    assert _confirms(pending) == 1
    assert signals.confirmed == [pending.pk]


def test_an_account_retry_returns_the_confirmed_match(
    two_players: list[Player], event: Event, signals: SignalLog
) -> None:
    pending = _record(two_players, submitted_by_user=two_players[0].account).match
    account = _event_account(event)
    _record(two_players, submitted_by_user=account, event=event)

    retry = _record(two_players, submitted_by_user=account, event=event)

    assert not retry.created
    assert retry.match.pk == pending.pk
    assert _confirms(pending) == 1
    assert signals.confirmed == [pending.pk]


def test_another_submitter_is_still_refused(
    two_players: list[Player], event: Event
) -> None:
    _record(two_players, submitted_by_user=two_players[0].account)
    _record(two_players, submitted_by_user=_event_account(event), event=event)

    with pytest.raises(DuplicateMatchError):
        _record(two_players, submitted_by_user=_event_account(event), event=event)
