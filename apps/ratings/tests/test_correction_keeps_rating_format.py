"""A correction that keeps its teams keeps its rating format (#318).

A player's Account deletion clears their gender division (ADR 0048). A
correction made after it, with the same two teams, keeps the corrected
match's rating format, so a full recompute (#48, ADR 0058) rates it in
the same format and gives every player the same ratings. Made-up data
only.
"""

from datetime import timedelta

import pytest

from apps.accounts.deletion import delete_account
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, RatingFormat
from apps.matches.services import record_match
from apps.matches.workflow import edit_match
from apps.players.models import GenderDivision, Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings import services
from apps.ratings.models import RatingState
from apps.ratings.services import RecomputeChangeKind, recompute_ratings

pytestmark = pytest.mark.django_db

GAMES = ((11, 7), (9, 11), (11, 4))


def _states() -> dict[tuple[str, str], dict[str, object]]:
    return {
        (str(state.player.public_code), state.rating_format): {
            name: getattr(state, name) for name in services.VALUE_FIELDS
        }
        for state in RatingState.objects.select_related("player")
    }


def test_a_recompute_gives_the_same_ratings_after_a_same_teams_correction() -> None:
    event = EventFactory.create(
        organizer=verified_organizer(), kind=EventKind.TOURNAMENT
    )
    member = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    divisions = (
        GenderDivision.MENS,
        GenderDivision.WOMENS,
        GenderDivision.WOMENS,
        GenderDivision.MENS,
    )
    players: list[Player] = [PlayerFactory.create(gender_division=d) for d in divisions]
    played_on = event.start_date

    def record(games: tuple[tuple[int, int], ...], day: int) -> Match:
        return record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=games,
            played_on=played_on + timedelta(days=day),
            submitted_by_user=member,
            event=event,
        ).match

    record(((11, 3), (11, 5)), 0)
    old = record(GAMES, 1)
    assert old.status == MatchStatus.CONFIRMED
    assert old.rating_format == RatingFormat.MIXED_DOUBLES
    recompute_ratings()
    before = _states()
    assert any(fmt == RatingFormat.MIXED_DOUBLES for _, fmt in before)

    deleted = players[1]
    delete_account(deleted.account, actor=deleted.account)
    # The same teams and scores, with a changed location only.
    corrected = edit_match(
        old,
        actor=member,
        format=MatchFormat.DOUBLES,
        team_a=[players[3], players[2]],
        team_b=players[:2],
        games=[(b, a) for a, b in GAMES],
        played_on=old.played_on,
        location="Court 2",
    )

    corrected.refresh_from_db()
    assert corrected.status == MatchStatus.CONFIRMED
    assert corrected.rating_format == RatingFormat.MIXED_DOUBLES
    result = recompute_ratings()
    assert {change.kind for change in result.changes} == {RecomputeChangeKind.UNCHANGED}
    assert _states() == before
