"""A player's correction records the corrector as its submitter (#267).

The maintainer's decision on #267: ``edit_match(as_player=True)``
records the correcting player's Account as the correction's
``submitted_by_user``, not the old match's submitter. The other edit
paths keep the old submitter (ADR 0013). So a moderator's bulk void of
the corrector's matches reaches the correction, and one of the original
submitter's matches does not. Made-up data only.
"""

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.matches.models import Match, MatchFormat, MatchStatus
from apps.matches.permissions import MatchActionDenied, can_change_match
from apps.matches.services import DuplicateMatchError, record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
from apps.matches.workflow import edit_match, void_submitter_matches
from apps.players.models import Player

pytestmark = pytest.mark.django_db

REASON = "Made-up review reason."


NEW_GAMES = [(11, 8), (11, 6)]


def _player_correction(
    match: Match, players: list[Player], *, as_player: bool = True
) -> Match:
    return edit_match(
        match,
        actor=players[2].account,
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=NEW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        as_player=as_player,
    )


def test_the_corrector_is_the_submitter(pending: Match, players: list[Player]) -> None:
    assert pending.submitted_by_user_id == players[0].account_id

    corrected = _player_correction(pending, players)

    stored = Match.objects.get(pk=corrected.pk)
    assert stored.submitted_by_user_id == players[2].account_id
    assert stored.submitted_by_application_id is None


def test_a_bulk_void_reaches_the_correction_through_the_corrector(
    pending: Match, players: list[Player], moderator: User
) -> None:
    corrected = _player_correction(pending, players)
    today = timezone.now().date()

    void_submitter_matches(
        players[0].account, recorded_since=today, actor=moderator, reason=REASON
    )
    assert Match.objects.get(pk=corrected.pk).status == MatchStatus.PENDING

    result = void_submitter_matches(
        players[2].account, recorded_since=today, actor=moderator, reason=REASON
    )

    assert result.voided == 1
    assert Match.objects.get(pk=corrected.pk).status == MatchStatus.VOIDED


def test_a_duplicate_correction_names_the_code_by_the_existing_rule(
    pending: Match, players: list[Player]
) -> None:
    # Another match, submitted by someone else, with the correction's
    # teams, date, and scores. The corrector plays in it (a player's
    # correction keeps the teams), so the existing rule names its code.
    other = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=NEW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[1].account,
    ).match

    with pytest.raises(DuplicateMatchError) as error:
        _player_correction(pending, players)

    assert error.value.existing_code == other.public_code
    assert Match.objects.get(pk=pending.pk).status == MatchStatus.PENDING
    assert not Match.objects.filter(corrects=pending).exists()


def test_being_the_submitter_gives_no_authority_to_change(
    pending: Match, players: list[Player]
) -> None:
    corrected = _player_correction(pending, players)

    assert not can_change_match(players[2].account, corrected)
    with pytest.raises(MatchActionDenied):
        _player_correction(corrected, players, as_player=False)
    assert Match.objects.get(pk=corrected.pk).status == MatchStatus.PENDING
