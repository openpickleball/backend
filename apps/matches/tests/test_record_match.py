"""``record_match``: validation, then exact and near duplicate detection.

Every submitter here is permitted (#79): the Account fixtures are members
of one verified Organizer, the application is approved with that
Organizer's grant, and ``_record`` submits under that Organizer's Event
(ADR 0021). A Player's own Account submits with no Event, as a
self-submission. Made-up players and accounts only.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import OrganizerMember
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    BestOf,
    FlagReason,
    Game,
    GameTarget,
    Match,
    MatchFlag,
    MatchFormat,
    MatchParticipant,
    MatchStatus,
    ScoringType,
    Team,
)
from apps.matches.services import (
    DuplicateKind,
    DuplicateMatchError,
    MatchIssue,
    MatchValidationError,
    _issues_from_validation_error,
    match_fingerprint,
    record_match,
)
from apps.matches.tests.factories import raw_update
from apps.partners.models import OrganizerApplicationGrant, PartnerApplication
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    approved_application,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7), (9, 11), (11, 4))
OTHER_GAMES = ((11, 5), (11, 8))


def _swapped(games: Sequence[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    return tuple((b, a) for a, b in games)


def _players(count: int) -> list[Player]:
    return [PlayerFactory.create() for _ in range(count)]


@pytest.fixture
def event() -> Event:
    """An Event of a verified Organizer."""
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _event_account(event: Event) -> User:
    """A member of ``event``'s Organizer, so it may submit there."""
    made: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=made)
    return made


def _granted_application(event: Event) -> PartnerApplication:
    """An approved application with the grant of ``event``'s Organizer."""
    made = approved_application()
    OrganizerApplicationGrantFactory.create(organizer=event.organizer, application=made)
    return made


@pytest.fixture
def user(event: Event) -> User:
    return _event_account(event)


@pytest.fixture
def other_user(event: Event) -> User:
    return _event_account(event)


@pytest.fixture
def application(event: Event) -> PartnerApplication:
    return _granted_application(event)


@pytest.fixture
def team_a() -> list[Player]:
    return _players(2)


@pytest.fixture
def team_b() -> list[Player]:
    return _players(2)


def _permitted_event(submitter: User | PartnerApplication) -> Event | None:
    """An Event where ``submitter`` may submit, or None for a Player's own
    Account, which self-submits."""
    if isinstance(submitter, User):
        member = OrganizerMember.objects.filter(account=submitter).first()
        return member.organizer.events.first() if member else None
    grant = OrganizerApplicationGrant.objects.filter(application=submitter).first()
    return grant.organizer.events.first() if grant else None


def _record(
    team_a: Sequence[Player],
    team_b: Sequence[Player],
    *,
    submitter: User | PartnerApplication,
    **overrides: Any,
) -> Match:
    values: dict[str, Any] = {
        "format": MatchFormat.DOUBLES,
        "team_a": team_a,
        "team_b": team_b,
        "games": GAMES,
        "played_on": PLAYED_ON,
        "event": _permitted_event(submitter),
    }
    if isinstance(submitter, User):
        values["submitted_by_user"] = submitter
    else:
        values["submitted_by_application"] = submitter
    values.update(overrides)
    return record_match(**values).match


def _counts() -> tuple[int, int, int, int]:
    return (
        Match.objects.count(),
        MatchParticipant.objects.count(),
        Game.objects.count(),
        MatchFlag.objects.count(),
    )


# A valid submission.


def test_records_match_with_participants_games_and_defaults(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    match = _record(team_a, team_b, submitter=user)

    match.refresh_from_db()
    # A Club submission is created confirmed (#26, decision F4).
    assert match.status == MatchStatus.CONFIRMED
    assert match.format == MatchFormat.DOUBLES
    assert match.played_on == PLAYED_ON
    assert match.game_target == GameTarget.ELEVEN
    assert match.scoring_type == ScoringType.SIDE_OUT
    assert match.best_of == BestOf.THREE
    assert match.submitted_by_user == user
    by_team = {
        team: {p.player_id for p in match.participants.filter(team=team)}
        for team in Team
    }
    assert by_team == {
        Team.A: {player.pk for player in team_a},
        Team.B: {player.pk for player in team_b},
    }
    games = [
        (game.game_number, game.team_a_score, game.team_b_score)
        for game in match.games.all()
    ]
    assert games == [(1, 11, 7), (2, 9, 11), (3, 11, 4)]
    assert match.fingerprint == match_fingerprint(
        [p.pk for p in team_a], [p.pk for p in team_b], PLAYED_ON, GAMES
    )
    assert not match.flags.exists()


def test_records_the_given_options(
    application: PartnerApplication, team_a: list[Player], team_b: list[Player]
) -> None:
    played_at = datetime(2026, 6, 1, 19, 0, tzinfo=timezone(timedelta(hours=-7)))

    match = _record(
        team_a,
        team_b,
        submitter=application,
        games=((15, 9),),
        best_of=BestOf.ONE,
        game_target=GameTarget.FIFTEEN,
        scoring_type=ScoringType.RALLY,
        external_id="ext-made-up-1",
        played_at=played_at,
        played_at_utc_offset=-420,
        location="Made-up Park",
        event_label="Made-up Open",
        bracket_label="Round 1",
        sequence=3,
    )

    match.refresh_from_db()
    assert match.submitted_by_application == application
    assert match.submitted_by_user is None
    assert match.best_of == BestOf.ONE
    assert match.game_target == GameTarget.FIFTEEN
    assert match.scoring_type == ScoringType.RALLY
    assert match.external_id == "ext-made-up-1"
    assert match.played_at == played_at
    assert match.played_at_utc_offset == -420
    assert match.location == "Made-up Park"
    assert match.event_label == "Made-up Open"
    assert match.bracket_label == "Round 1"
    assert match.sequence == 3


# Criterion 8: validation runs first and a failure writes nothing.


def test_score_validation_failure_writes_nothing(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    before = _counts()

    with pytest.raises(MatchValidationError) as error:
        _record(team_a, team_b, submitter=user, games=((11, 10),))

    assert [issue.code for issue in error.value.issues] == ["not_won_by_two"]
    assert _counts() == before


def test_full_clean_failure_writes_nothing(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    # 19:00 on 2 June where it was played, but the date played is 1 June.
    played_at = datetime(2026, 6, 2, 19, 0, tzinfo=timezone(timedelta(hours=-7)))
    before = _counts()

    with pytest.raises(MatchValidationError) as error:
        _record(
            team_a,
            team_b,
            submitter=user,
            played_at=played_at,
            played_at_utc_offset=-420,
        )

    issues = [(issue.field, issue.code) for issue in error.value.issues]
    assert issues == [("played_on", "invalid")]
    assert error.value.issues[0].message == (
        "The date played must be the local date of the time played."
    )
    assert _counts() == before


def test_full_clean_failure_on_two_submitters_writes_nothing(
    user: User,
    application: PartnerApplication,
    team_a: list[Player],
    team_b: list[Player],
) -> None:
    before = _counts()

    with pytest.raises(MatchValidationError) as error:
        _record(team_a, team_b, submitter=user, submitted_by_application=application)

    fields = {issue.field for issue in error.value.issues}
    assert fields == {"submitted_by_user", "submitted_by_application"}
    assert _counts() == before


def test_model_error_without_a_field_is_reported_on_match() -> None:
    error = ValidationError("Made-up rule broken.", code="made_up")

    issues = _issues_from_validation_error(ValidationError({NON_FIELD_ERRORS: error}))

    assert issues == [MatchIssue("match", "made_up", "Made-up rule broken.")]


def test_model_error_without_a_code_gets_invalid() -> None:
    issues = _issues_from_validation_error(ValidationError({"location": "Bad."}))

    assert issues == [MatchIssue("location", "invalid", "Bad.")]


@pytest.mark.parametrize("member", ["unsaved", "not_a_player"])
def test_team_member_must_be_a_saved_player(
    user: User, team_a: list[Player], team_b: list[Player], member: str
) -> None:
    odd: Any = Player() if member == "unsaved" else "P-MADE-UP0-0000"
    before = _counts()

    with pytest.raises(TypeError):
        _record([team_a[0], odd], team_b, submitter=user)

    assert _counts() == before


# Criterion 1: an exact duplicate is refused.


def test_exact_duplicate_from_another_submitter_is_refused(
    user: User, other_user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    _record(team_a, team_b, submitter=user)
    before = _counts()

    with pytest.raises(DuplicateMatchError) as error:
        _record(team_a, team_b, submitter=other_user)

    assert error.value.kind == DuplicateKind.DUPLICATE_MATCH
    assert _counts() == before


# Criterion 11: the code is named only to the submitter or a participant.


def test_duplicate_names_the_code_to_the_same_account(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    first = _record(team_a, team_b, submitter=user)

    with pytest.raises(DuplicateMatchError) as error:
        _record(team_a, team_b, submitter=user)

    assert error.value.existing_code == first.public_code


def test_duplicate_names_the_code_to_the_same_application(
    application: PartnerApplication, team_a: list[Player], team_b: list[Player]
) -> None:
    first = _record(team_a, team_b, submitter=application, external_id="ext-1")

    with pytest.raises(DuplicateMatchError) as error:
        _record(team_a, team_b, submitter=application, external_id="ext-2")

    assert error.value.kind == DuplicateKind.DUPLICATE_MATCH
    assert error.value.existing_code == first.public_code


def test_duplicate_names_the_code_to_a_participants_account(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    first = _record(team_a, team_b, submitter=user)

    with pytest.raises(DuplicateMatchError) as error:
        _record(team_a, team_b, submitter=team_b[1].account)

    assert error.value.existing_code == first.public_code


def test_duplicate_hides_the_code_from_an_unrelated_account(
    user: User, other_user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    _record(team_a, team_b, submitter=user)

    with pytest.raises(DuplicateMatchError) as error:
        _record(team_a, team_b, submitter=other_user)

    assert error.value.existing_code is None
    assert str(error.value) == "This match was already submitted."


def test_duplicate_hides_the_code_from_an_unrelated_application(
    user: User,
    application: PartnerApplication,
    team_a: list[Player],
    team_b: list[Player],
) -> None:
    _record(team_a, team_b, submitter=user)

    with pytest.raises(DuplicateMatchError) as error:
        _record(team_a, team_b, submitter=application, external_id="ext-1")

    assert error.value.existing_code is None


def test_exact_duplicate_with_teams_swapped_and_players_reordered_is_refused(
    application: PartnerApplication,
    team_a: list[Player],
    team_b: list[Player],
) -> None:
    first = _record(team_a, team_b, submitter=application, external_id="ext-1")
    before = _counts()

    with pytest.raises(DuplicateMatchError) as error:
        _record(
            team_b[::-1],
            team_a[::-1],
            submitter=application,
            games=_swapped(GAMES),
            external_id="ext-made-up-2",
        )

    assert error.value.kind == DuplicateKind.DUPLICATE_MATCH
    assert error.value.existing_code == first.public_code
    assert _counts() == before


def test_duplicate_error_message_names_the_code_only(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    first = _record(team_a, team_b, submitter=user)

    with pytest.raises(DuplicateMatchError) as error:
        _record(team_a, team_b, submitter=user)

    assert str(error.value) == (
        f"This match was already submitted as {first.public_code}."
    )


def test_singles_exact_duplicate_is_refused(user: User) -> None:
    a, b = _players(2)
    first = _record([a], [b], submitter=user, format=MatchFormat.SINGLES)

    with pytest.raises(DuplicateMatchError) as error:
        _record(
            [b], [a], submitter=user, format=MatchFormat.SINGLES, games=_swapped(GAMES)
        )

    assert error.value.existing_code == first.public_code


# Criterion 2: a near duplicate is saved and flagged.


def test_near_duplicate_is_saved_with_a_flag(
    user: User, other_user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    first = _record(team_a, team_b, submitter=user)

    second = _record(team_b, team_a, submitter=other_user, games=OTHER_GAMES)

    flags = list(second.flags.all())
    assert [(f.reason, f.duplicate_of_id) for f in flags] == [
        (FlagReason.POSSIBLE_DUPLICATE, first.pk)
    ]
    assert not first.flags.exists()


def test_two_earlier_near_duplicates_give_two_flags(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    first = _record(team_a, team_b, submitter=user)
    second = _record(team_a, team_b, submitter=user, games=OTHER_GAMES)

    third = _record(team_a, team_b, submitter=user, games=((11, 0), (11, 1)))

    assert {f.duplicate_of_id for f in third.flags.all()} == {first.pk, second.pk}
    assert [f.duplicate_of_id for f in second.flags.all()] == [first.pk]


def test_near_duplicate_of_a_match_without_fingerprint_is_flagged(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    first = _record(team_a, team_b, submitter=user)
    raw_update(first, fingerprint="")

    second = _record(team_a, team_b, submitter=user)

    assert [f.duplicate_of_id for f in second.flags.all()] == [first.pk]


def test_other_teams_on_the_same_day_are_not_flagged(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    _record(team_a, team_b, submitter=user)
    # One player in common on each team is not the same lineup.
    other_a = [team_a[0], *_players(1)]
    other_b = [team_b[0], *_players(1)]

    second = _record(other_a, other_b, submitter=user, games=OTHER_GAMES)

    assert not second.flags.exists()


def test_players_moved_between_teams_are_not_flagged(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    _record(team_a, team_b, submitter=user)

    second = _record(
        [team_a[0], team_b[0]],
        [team_a[1], team_b[1]],
        submitter=user,
        games=OTHER_GAMES,
    )

    assert not second.flags.exists()


def test_same_teams_on_another_day_are_not_flagged(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    _record(team_a, team_b, submitter=user)

    second = _record(
        team_a, team_b, submitter=user, played_on=date(2026, 6, 2), games=OTHER_GAMES
    )

    assert not second.flags.exists()


def test_same_scores_on_another_day_are_not_a_duplicate(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    _record(team_a, team_b, submitter=user)

    second = _record(team_a, team_b, submitter=user, played_on=date(2026, 6, 2))

    assert Match.objects.count() == 2
    assert not second.flags.exists()


# Criterion 6: a voided match is ignored.


def _void(match: Match) -> None:
    raw_update(match, status=MatchStatus.VOIDED)


def test_voided_exact_duplicate_is_ignored(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    first = _record(team_a, team_b, submitter=user)
    _void(first)

    second = _record(team_a, team_b, submitter=user)

    assert second.pk != first.pk
    assert second.fingerprint == first.fingerprint
    assert not second.flags.exists()


def test_voided_near_duplicate_is_not_flagged(
    user: User, team_a: list[Player], team_b: list[Player]
) -> None:
    _void(_record(team_a, team_b, submitter=user))

    second = _record(team_a, team_b, submitter=user, games=OTHER_GAMES)

    assert not second.flags.exists()


# Criterion 4: a repeated external id for one application is refused.


def test_repeated_external_id_with_other_scores_is_refused(
    application: PartnerApplication, team_a: list[Player], team_b: list[Player]
) -> None:
    first = _record(team_a, team_b, submitter=application, external_id="ext-1")
    before = _counts()

    with pytest.raises(DuplicateMatchError) as error:
        _record(
            _players(2),
            _players(2),
            submitter=application,
            external_id="ext-1",
            games=OTHER_GAMES,
        )

    assert error.value.kind == DuplicateKind.DUPLICATE_EXTERNAL_ID
    assert error.value.existing_code == first.public_code
    assert _counts() == before


def test_repeated_external_id_of_a_voided_match_is_refused(
    application: PartnerApplication, team_a: list[Player], team_b: list[Player]
) -> None:
    first = _record(team_a, team_b, submitter=application, external_id="ext-1")
    _void(first)
    before = _counts()

    with pytest.raises(DuplicateMatchError) as error:
        _record(team_a, team_b, submitter=application, external_id="ext-1")

    assert error.value.kind == DuplicateKind.DUPLICATE_EXTERNAL_ID
    assert error.value.existing_code == first.public_code
    assert _counts() == before


def test_external_id_wins_over_an_exact_duplicate(
    application: PartnerApplication, team_a: list[Player], team_b: list[Player]
) -> None:
    first = _record(team_a, team_b, submitter=application, external_id="ext-1")

    with pytest.raises(DuplicateMatchError) as error:
        _record(team_a, team_b, submitter=application, external_id="ext-1")

    assert error.value.kind == DuplicateKind.DUPLICATE_EXTERNAL_ID
    assert error.value.existing_code == first.public_code


def test_another_application_may_reuse_an_external_id(
    event: Event,
    application: PartnerApplication,
    team_a: list[Player],
    team_b: list[Player],
) -> None:
    _record(team_a, team_b, submitter=application, external_id="ext-1")
    other = _granted_application(event)

    second = _record(_players(2), _players(2), submitter=other, external_id="ext-1")

    assert second.external_id == "ext-1"
