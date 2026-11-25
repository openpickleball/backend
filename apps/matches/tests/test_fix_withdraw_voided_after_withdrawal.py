"""A withdrawal on a voided match is refused, even after an earlier one (#267).

A player who withdrew their dispute, and whose match was voided later,
gets ``MatchStateError`` (409 from the API) on another withdrawal, as
``dispute_match`` refuses a voided match. A repeat on a match that is not
voided still changes nothing. Made-up data only.
"""

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.matches.models import Match, MatchStatus
from apps.matches.workflow import (
    MatchStateError,
    dispute_match,
    void_match,
    withdraw_dispute,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db


def test_a_withdrawal_after_a_void_is_refused(
    confirmed: Match, players: list[Player], moderator: User
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    withdraw_dispute(confirmed, actor=players[0].account)
    void_match(confirmed, actor=moderator, reason="checked")

    with pytest.raises(MatchStateError):
        withdraw_dispute(confirmed, actor=players[0].account)


def test_the_api_answers_409_for_a_withdrawal_after_a_void(
    confirmed: Match, players: list[Player], moderator: User
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    withdraw_dispute(confirmed, actor=players[0].account)
    void_match(confirmed, actor=moderator, reason="checked")
    client = Client()
    client.force_login(players[0].account)

    response = client.post(f"/api/v1/matches/{confirmed.public_code}/withdraw-dispute")

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.VOIDED


def test_a_repeat_on_a_match_not_voided_still_changes_nothing(
    confirmed: Match, players: list[Player]
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    withdraw_dispute(confirmed, actor=players[0].account)

    result = withdraw_dispute(confirmed, actor=players[0].account)

    assert result.status == MatchStatus.CONFIRMED
