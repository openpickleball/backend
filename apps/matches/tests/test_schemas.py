"""The API's Match schema and the Private player rule (issue #31).

A participant shows by display name and OPEN Code, whatever their own
Privacy Settings, except while their Account is inactive: then they show
as "Private player" with no code (ADR 0002), unless the viewer is that
participant's own Account. Made-up data only.
"""

import pytest
from django.contrib.auth import get_user_model
from pytest_django import DjangoAssertNumQueries

from apps.accounts.tests.factories import UserFactory
from apps.matches.models import Match
from apps.matches.schemas import (
    PRIVATE_PLAYER_NAME,
    MatchOut,
    TeamConfirmationsOut,
    match_out,
    with_match_details,
)
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON
from apps.matches.workflow import confirm_match, dispute_match
from apps.players.models import Player, PlayerPrivacy, Visibility
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _loaded(match: Match) -> Match:
    return with_match_details(Match.objects.filter(pk=match.pk)).get()


def test_match_out_lists_teams_games_and_settings(
    pending: Match, players: list[Player]
) -> None:
    out = match_out(_loaded(pending), players[0].account)

    assert out.model_dump() == {
        "code": pending.public_code,
        "status": "pending",
        "format": "doubles",
        "played_on": WORKFLOW_PLAYED_ON,
        "best_of": 3,
        "scoring_type": "side_out",
        "game_target": 11,
        "team_a": [
            {"code": p.public_code, "display_name": p.display_name} for p in players[:2]
        ],
        "team_b": [
            {"code": p.public_code, "display_name": p.display_name} for p in players[2:]
        ],
        "games": [
            {"number": number, "team_a": a, "team_b": b}
            for number, (a, b) in enumerate(WORKFLOW_GAMES, start=1)
        ],
        "my_team": "A",
        "my_decision": "none",
        "confirmed_by_team": {"a": False, "b": False},
        "created_at": pending.created_at,
    }


def test_inactive_participant_shows_as_private_player_to_others(
    pending: Match, players: list[Player]
) -> None:
    get_user_model().objects.filter(pk=players[3].account_id).update(is_active=False)

    out = match_out(_loaded(pending), players[0].account)

    assert out.team_b[1].model_dump() == {
        "code": None,
        "display_name": PRIVATE_PLAYER_NAME,
    }
    assert PRIVATE_PLAYER_NAME == "Private player"
    assert players[3].display_name not in out.model_dump_json()
    assert players[3].public_code not in out.model_dump_json()
    # Everyone else still shows by name.
    assert out.team_b[0].display_name == players[2].display_name


def test_inactive_participant_sees_their_own_name(
    pending: Match, players: list[Player]
) -> None:
    get_user_model().objects.filter(pk=players[3].account_id).update(is_active=False)
    viewer = get_user_model().objects.get(pk=players[3].account_id)

    out = match_out(_loaded(pending), viewer)

    assert out.team_b[1].model_dump() == {
        "code": players[3].public_code,
        "display_name": players[3].display_name,
    }


def test_private_privacy_settings_do_not_hide_a_participant(
    pending: Match, players: list[Player]
) -> None:
    PlayerPrivacy.objects.filter(player=players[2]).update(
        match_history_visibility=Visibility.PRIVATE
    )

    out = match_out(_loaded(pending), players[0].account)

    assert out.team_b[0].code == players[2].public_code
    assert out.team_b[0].display_name == players[2].display_name


def test_match_out_reads_only_prefetched_rows(
    pending: Match,
    players: list[Player],
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    confirm_match(pending, actor=players[0].account)
    dispute_match(pending, actor=players[2].account)
    loaded = _loaded(pending)

    with django_assert_num_queries(0):
        out = match_out(loaded, players[2].account)

    assert out.my_team == "B"
    assert out.my_decision == "dispute"
    assert out.confirmed_by_team is not None
    assert out.confirmed_by_team.a is True


# The viewer's own team, decision, and each team's confirmation (#193).


@pytest.mark.parametrize(("index", "team"), [(0, "A"), (1, "A"), (2, "B"), (3, "B")])
def test_participant_sees_their_own_team(
    pending: Match, players: list[Player], index: int, team: str
) -> None:
    out = match_out(_loaded(pending), players[index].account)

    assert out.my_team == team
    assert out.my_decision == "none"
    assert out.confirmed_by_team is not None
    assert out.confirmed_by_team.model_dump() == {"a": False, "b": False}


def test_confirmation_shows_for_the_confirming_team_only(
    pending: Match, players: list[Player]
) -> None:
    confirm_match(pending, actor=players[3].account)

    for_confirmer = match_out(_loaded(pending), players[3].account)
    for_teammate = match_out(_loaded(pending), players[2].account)
    for_opponent = match_out(_loaded(pending), players[0].account)

    assert for_confirmer.my_decision == "confirm"
    assert for_teammate.my_decision == "none"
    assert for_opponent.my_decision == "none"
    for out in (for_confirmer, for_teammate, for_opponent):
        assert out.confirmed_by_team is not None
        assert out.confirmed_by_team.model_dump() == {"a": False, "b": True}


def test_both_teams_confirmed(confirmed: Match, players: list[Player]) -> None:
    out = match_out(_loaded(confirmed), players[1].account)

    assert out.status == "confirmed"
    assert out.my_decision == "none"
    assert out.confirmed_by_team is not None
    assert out.confirmed_by_team.model_dump() == {"a": True, "b": True}


def test_latest_decision_wins(confirmed: Match, players: list[Player]) -> None:
    dispute_match(confirmed, actor=players[0].account)

    out = match_out(_loaded(confirmed), players[0].account)

    assert out.my_decision == "dispute"
    # The earlier confirmation still counts for the team.
    assert out.confirmed_by_team is not None
    assert out.confirmed_by_team.model_dump() == {"a": True, "b": True}


def test_dispute_does_not_count_as_confirmation(
    pending: Match, players: list[Player]
) -> None:
    dispute_match(pending, actor=players[0].account)

    out = match_out(_loaded(pending), players[0].account)

    assert out.my_decision == "dispute"
    assert out.confirmed_by_team is not None
    assert out.confirmed_by_team.model_dump() == {"a": False, "b": False}


def test_non_participant_gets_no_viewer_fields(confirmed: Match) -> None:
    outsider = PlayerFactory.create()

    out = match_out(_loaded(confirmed), outsider.account)

    assert out.my_team is None
    assert out.my_decision == "none"
    assert out.confirmed_by_team is None
    assert out.created_at == confirmed.created_at


def test_account_without_a_player_gets_no_viewer_fields(confirmed: Match) -> None:
    out = match_out(_loaded(confirmed), UserFactory.create())

    assert out.my_team is None
    assert out.my_decision == "none"
    assert out.confirmed_by_team is None


def test_no_viewer_gets_no_viewer_fields(confirmed: Match) -> None:
    out = match_out(_loaded(confirmed), None)

    assert out.my_team is None
    assert out.my_decision == "none"
    assert out.confirmed_by_team is None


def test_match_out_has_only_the_documented_fields() -> None:
    assert set(MatchOut.model_fields) == {
        "code",
        "status",
        "format",
        "played_on",
        "best_of",
        "scoring_type",
        "game_target",
        "team_a",
        "team_b",
        "games",
        "my_team",
        "my_decision",
        "confirmed_by_team",
        "created_at",
    }
    assert set(TeamConfirmationsOut.model_fields) == {"a", "b"}
