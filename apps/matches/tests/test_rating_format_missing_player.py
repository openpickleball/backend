"""A Player row deleted after its instance was taken fails as validation (#44).

Deriving the rating format reads the players' divisions from the
database. A row that is gone must not raise a bare ``KeyError`` there:
the model rules report the missing player as before. Made-up data only.
"""

from datetime import date

import pytest

from apps.matches.models import Match, MatchFormat
from apps.matches.services import MatchValidationError, record_match
from apps.players.models import Player, PlayerPrivacy
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("match_format", [MatchFormat.SINGLES, MatchFormat.DOUBLES])
def test_a_deleted_player_row_is_a_validation_error(match_format: str) -> None:
    size = 1 if match_format == MatchFormat.SINGLES else 2
    players = [PlayerFactory.create() for _ in range(size * 2)]
    gone = players[-1]
    # Taken as an instance, then deleted under it.
    PlayerPrivacy.objects.filter(player=gone).delete()
    Player.objects.filter(pk=gone.pk).delete()

    with pytest.raises(MatchValidationError) as raised:
        record_match(
            format=match_format,
            team_a=players[:size],
            team_b=players[size:],
            games=[(11, 4)],
            played_on=date(2026, 3, 1),
            submitted_by_user=players[0].account,
        )

    assert any(issue.field == "team_b" for issue in raised.value.issues)
    assert not Match.objects.exists()
