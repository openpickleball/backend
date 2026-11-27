"""A played match names the match it corrects only to that match's
players (#39).

A correction can change the teams. ``corrects`` named the corrected
match's OPEN Code to every player of the correction, including one who
did not play in the corrected match and whom ``get_match`` refuses it.
Made-up data only.
"""

import json
from datetime import date
from typing import Any

import pytest

from apps.exports.builder import build_export
from apps.matches.models import MatchFormat, MatchParticipant, Team
from apps.matches.services import record_match
from apps.matches.tests.factories import MatchFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _corrects(player: Player) -> dict[str, Any]:
    data = json.loads(build_export(player.account).model_dump_json())
    return {m["code"]: m["corrects"] for m in data["matches"]}


def test_corrects_names_the_old_match_only_to_its_players() -> None:
    a, c, e = (PlayerFactory.create() for _ in range(3))
    old = record_match(
        format=MatchFormat.SINGLES,
        team_a=[a],
        team_b=[c],
        games=((11, 7),),
        played_on=date(2026, 5, 1),
        submitted_by_user=a.account,
    ).match
    new = MatchFactory.create(
        participants=False, games=False, corrects=old, played_on=date(2026, 5, 1)
    )
    MatchParticipant.objects.create(match=new, player=e, team=Team.A)
    MatchParticipant.objects.create(match=new, player=c, team=Team.B)

    assert _corrects(c)[new.public_code] == old.public_code
    assert _corrects(e) == {new.public_code: None}
