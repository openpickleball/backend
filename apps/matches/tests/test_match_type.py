"""Each Match stores its match type when it is recorded (#163, ADR 0035).

The type is the Event's kind, read from the database, ``rec_verified`` for
a Club with no Event, and ``self_reported`` for neither. It never changes
afterward, so a later change of the Event's kind moves only new matches.
Made-up data only.
"""

from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import (
    ClubFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    Match,
    MatchFormat,
    MatchImmutable,
    MatchType,
)
from apps.matches.services import RecordedMatch, record_match
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.matches.workflow import edit_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7),)
_FITS = "The match type must fit the match's event and club."


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(2)]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(
        organizer=verified_organizer(), kind=EventKind.TOURNAMENT
    )
    return made


def _member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _record(players: Sequence[Player], **overrides: Any) -> RecordedMatch:
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": players[:1],
        "team_b": players[1:],
        "games": GAMES,
        "played_on": PLAYED_ON,
    }
    values.update(overrides)
    return record_match(**values)


def _stored_type(match: Match) -> str:
    stored: str = Match.objects.values_list("match_type", flat=True).get(pk=match.pk)
    return stored


def test_the_choices_are_the_engine_match_types() -> None:
    assert MatchType.values == ["tournament", "league", "rec_verified", "self_reported"]
    assert Match._meta.get_field("match_type").default == MatchType.SELF_REPORTED


def test_a_self_submission_is_self_reported(players: list[Player]) -> None:
    recorded = _record(players, submitted_by_user=players[0].account)

    assert _stored_type(recorded.match) == MatchType.SELF_REPORTED


@pytest.mark.parametrize("kind", EventKind.values)
def test_an_event_match_takes_the_event_kind(
    players: list[Player], event: Event, kind: str
) -> None:
    Event.objects.filter(pk=event.pk).update(kind=kind)
    event.refresh_from_db()

    recorded = _record(players, submitted_by_user=_member(event), event=event)

    assert _stored_type(recorded.match) == kind


def test_the_event_kind_is_read_from_the_database(
    players: list[Player], event: Event
) -> None:
    # A stale instance never decides the type.
    event.kind = EventKind.LEAGUE

    recorded = _record(players, submitted_by_user=_member(event), event=event)

    assert _stored_type(recorded.match) == MatchType.TOURNAMENT


def test_a_kind_change_moves_only_new_matches(
    players: list[Player], event: Event
) -> None:
    account = _member(event)
    first = _record(players, submitted_by_user=account, event=event).match
    event.kind = EventKind.LEAGUE
    event.save()

    second = _record(
        players, submitted_by_user=account, event=event, games=((11, 3),)
    ).match

    assert _stored_type(first) == MatchType.TOURNAMENT
    assert _stored_type(second) == MatchType.LEAGUE


def test_a_correction_takes_the_event_kind_at_correction_time(
    players: list[Player], event: Event
) -> None:
    account = _member(event)
    original = _record(players, submitted_by_user=account, event=event).match
    event.kind = EventKind.REC_VERIFIED
    event.save()

    correction = edit_match(
        original,
        actor=account,
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, 2),),
        played_on=PLAYED_ON,
        as_moderator=False,
    )

    assert _stored_type(original) == MatchType.TOURNAMENT
    assert _stored_type(correction) == MatchType.REC_VERIFIED


def test_a_correction_of_a_club_match_is_rec_verified(
    players: list[Player], moderator: User
) -> None:
    original: Match = MatchFactory.create(club=ClubFactory.create())

    correction = edit_match(
        original,
        actor=moderator,
        reason="Made-up correction",
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, 2),),
        played_on=PLAYED_ON,
    )

    assert _stored_type(correction) == MatchType.REC_VERIFIED


@pytest.mark.parametrize(
    ("with_event", "with_club", "match_type"),
    [
        (True, False, MatchType.SELF_REPORTED),
        (False, True, MatchType.TOURNAMENT),
        (False, True, MatchType.SELF_REPORTED),
        (False, False, MatchType.REC_VERIFIED),
        (False, False, MatchType.LEAGUE),
    ],
)
def test_the_database_refuses_a_type_that_does_not_fit(
    with_event: bool, with_club: bool, match_type: str
) -> None:
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "played_on": PLAYED_ON,
        "best_of": 1,
        "submitted_by_user": UserFactory.create(),
        "match_type": match_type,
    }
    if with_event:
        values["event"] = EventFactory.create()
    if with_club:
        values["club"] = ClubFactory.create()

    with (
        transaction.atomic(),
        pytest.raises(IntegrityError, match="matches_match_match_type_fits_source"),
    ):
        Match.objects.create(**values)


def test_clean_reports_a_type_that_does_not_fit_without_the_value() -> None:
    match = Match(
        format=MatchFormat.SINGLES,
        played_on=PLAYED_ON,
        best_of=1,
        submitted_by_user=UserFactory.create(),
        event=EventFactory.create(),
        match_type=MatchType.SELF_REPORTED,
    )

    with pytest.raises(ValidationError) as caught:
        match.full_clean()

    assert caught.value.message_dict["match_type"] == [_FITS]


def test_clean_accepts_every_event_kind_for_an_event_match() -> None:
    event = EventFactory.create()
    for kind in EventKind.values:
        match = Match(
            format=MatchFormat.SINGLES,
            played_on=PLAYED_ON,
            best_of=1,
            submitted_by_user=UserFactory.create(),
            event=event,
            match_type=kind,
        )
        match.full_clean(exclude=["public_code"])


def test_the_ledger_guard_refuses_a_type_change() -> None:
    match: Match = MatchFactory.create(event=EventFactory.create())
    match.match_type = MatchType.LEAGUE

    with pytest.raises(MatchImmutable):
        match.save()
    with pytest.raises(MatchImmutable):
        match.save(update_fields=["match_type", "status"])
    with pytest.raises(MatchImmutable):
        Match.objects.filter(pk=match.pk).update(match_type=MatchType.LEAGUE)


def test_a_raw_write_of_a_type_that_does_not_fit_is_refused() -> None:
    match: Match = MatchFactory.create(event=EventFactory.create())

    with (
        transaction.atomic(),
        pytest.raises(IntegrityError, match="matches_match_match_type_fits_source"),
    ):
        raw_update(match, match_type=MatchType.SELF_REPORTED)
