"""``record_match`` runs every model rule before any duplicate check, lock,
or write (review round 1, T1 to T3).

A score above the field's limit, an external id the model refuses, or a
missing submitter is a ``MatchValidationError``, never a database error and
never ``DuplicateMatchError``. The external id check uses the cleaned value.
Each submitter is permitted (#79): a Player's own Account, or an approved
application with a verified Organizer's grant, submitting under that
Organizer's Event (ADR 0021). Made-up data only.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from apps.clubs.tests.factories import verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Game, Match, MatchFlag, MatchFormat, MatchParticipant
from apps.matches.services import (
    DuplicateKind,
    DuplicateMatchError,
    MatchValidationError,
    record_match,
)
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
def teams() -> tuple[list[Player], list[Player]]:
    return [PlayerFactory.create()], [PlayerFactory.create()]


@pytest.fixture
def event() -> Event:
    """An Event of a verified Organizer (ADR 0021)."""
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _granted_application(event: Event) -> PartnerApplication:
    """An approved application with the grant of ``event``'s Organizer."""
    made = approved_application()
    OrganizerApplicationGrantFactory.create(organizer=event.organizer, application=made)
    return made


def _record(teams: tuple[list[Player], list[Player]], **overrides: Any) -> Match:
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": teams[0],
        "team_b": teams[1],
        "games": GAMES,
        "played_on": PLAYED_ON,
    }
    values.update(overrides)
    return record_match(**values).match


def _counts() -> tuple[int, int, int, int]:
    return (
        Match.objects.count(),
        MatchParticipant.objects.count(),
        Game.objects.count(),
        MatchFlag.objects.count(),
    )


def _fields(error: MatchValidationError) -> set[str]:
    return {issue.field for issue in error.issues}


# T1: a score above the field's limit.


def test_score_above_the_field_limit_is_a_validation_error(
    teams: tuple[list[Player], list[Player]],
) -> None:
    before = _counts()

    with pytest.raises(MatchValidationError) as error:
        _record(teams, games=((40000, 39998),), submitted_by_user=teams[0][0].account)

    assert _fields(error.value) == {"games.0"}
    for issue in error.value.issues:
        assert "40000" not in issue.message
        assert "39998" not in issue.message
    assert _counts() == before


# T2: the external id check uses the cleaned value.


def test_padded_external_id_repeats_the_stored_one(
    teams: tuple[list[Player], list[Player]], event: Event
) -> None:
    application = _granted_application(event)
    _record(
        teams, submitted_by_application=application, external_id="ext-1", event=event
    )
    before = _counts()

    with pytest.raises(DuplicateMatchError) as error:
        _record(
            teams,
            submitted_by_application=application,
            external_id=" ext-1",
            event=event,
        )

    assert error.value.kind == DuplicateKind.DUPLICATE_EXTERNAL_ID
    assert _counts() == before


def test_external_id_with_a_nul_is_a_validation_error(
    teams: tuple[list[Player], list[Player]], event: Event
) -> None:
    application = _granted_application(event)
    before = _counts()

    with pytest.raises(MatchValidationError) as error:
        _record(
            teams,
            submitted_by_application=application,
            external_id="a\x00b",
            event=event,
        )

    assert _fields(error.value) == {"external_id"}
    assert _counts() == before


def test_external_id_that_is_not_text_is_a_validation_error(
    teams: tuple[list[Player], list[Player]], event: Event
) -> None:
    application = _granted_application(event)
    _record(
        ([PlayerFactory.create()], [PlayerFactory.create()]),
        submitted_by_application=application,
        external_id="123",
        event=event,
    )
    before = _counts()

    with pytest.raises(MatchValidationError) as error:
        _record(
            teams, submitted_by_application=application, external_id=123, event=event
        )

    assert _fields(error.value) == {"external_id"}
    assert _counts() == before


# T3: a model-invalid exact duplicate is a validation error.


def test_exact_duplicate_without_a_submitter_is_a_validation_error(
    teams: tuple[list[Player], list[Player]],
) -> None:
    _record(teams, submitted_by_user=teams[0][0].account)
    before = _counts()

    with pytest.raises(MatchValidationError) as error:
        _record(teams)

    assert _fields(error.value) == {"submitted_by_user", "submitted_by_application"}
    assert _counts() == before


def test_exact_duplicate_with_a_wrong_local_date_is_a_validation_error(
    teams: tuple[list[Player], list[Player]],
) -> None:
    user = teams[0][0].account
    _record(teams, submitted_by_user=user)
    # 19:00 on 2 June where it was played, but the date played is 1 June.
    played_at = datetime(2026, 6, 2, 19, 0, tzinfo=timezone(timedelta(hours=-7)))
    before = _counts()

    with pytest.raises(MatchValidationError) as error:
        _record(
            teams,
            submitted_by_user=user,
            played_at=played_at,
            played_at_utc_offset=-420,
        )

    assert _fields(error.value) == {"played_on"}
    assert _counts() == before
