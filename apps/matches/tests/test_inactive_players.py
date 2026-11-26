"""No new Match names a Player whose Account is inactive (#116, ADR 0048).

A deleted or deactivated Account's Player stays in the Ledger, but every
submission path treats it as unknown. A correction may still keep the
players of the Match it corrects. Made-up data only.
"""

from typing import Any

import pytest
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.permissions import MatchPermissionDenied
from apps.matches.services import (
    MatchValidationError,
    hold_submitting_account,
    record_match,
    refuse_inactive_players,
)
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON
from apps.matches.tests.factories import DoublesMatchFactory
from apps.matches.workflow import edit_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

MATCHES = "/api/v1/matches/"


def _deactivate(player: Player) -> None:
    account = player.account
    account.is_active = False
    account.save(update_fields=["is_active"])


def _signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _body(players: list[Player]) -> dict[str, Any]:
    return {
        "format": "doubles",
        "played_on": WORKFLOW_PLAYED_ON.isoformat(),
        "team_a": [p.public_code for p in players[:2]],
        "team_b": [p.public_code for p in players[2:4]],
        "games": [{"team_a": 11, "team_b": 3}, {"team_a": 11, "team_b": 5}],
    }


def test_the_api_treats_an_inactive_players_code_as_unknown(
    players: list[Player],
) -> None:
    _deactivate(players[2])

    response = _signed_in(players[0].account).post(
        MATCHES, _body(players), content_type="application/json"
    )

    assert response.status_code == 422
    errors = response.json()["errors"]
    assert [(e["field"], e["code"]) for e in errors] == [
        ("body.team_b.0", "unknown_player")
    ]
    assert not Match.objects.exists()


def test_record_match_refuses_an_inactive_player() -> None:
    players = [PlayerFactory.create() for _ in range(4)]
    _deactivate(players[3])

    with pytest.raises(MatchValidationError) as raised:
        record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=WORKFLOW_GAMES,
            played_on=WORKFLOW_PLAYED_ON,
            submitted_by_user=players[0].account,
        )

    assert [(i.field, i.code) for i in raised.value.issues] == [
        ("team_b", "unknown_player")
    ]
    assert not Match.objects.exists()


def test_a_correction_keeps_an_inactive_player_of_the_corrected_match(
    pending: Match, players: list[Player]
) -> None:
    _deactivate(players[3])

    response = _signed_in(players[0].account).post(
        f"{MATCHES}{pending.public_code}/correction",
        _body(players),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert Match.objects.get(pk=pending.pk).status == MatchStatus.VOIDED


def test_a_correction_cannot_add_another_inactive_player(
    pending: Match, players: list[Player]
) -> None:
    outsider = PlayerFactory.create()
    _deactivate(outsider)
    body = _body([*players[:3], outsider])

    response = _signed_in(players[0].account).post(
        f"{MATCHES}{pending.public_code}/correction",
        body,
        content_type="application/json",
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "body.team_b.1"
    assert Match.objects.get(pk=pending.pk).status == MatchStatus.PENDING


def _mixed_pending() -> tuple[Match, list[Player]]:
    divisions = ["mens", "womens", "womens", "mens"]
    players = [PlayerFactory.create(gender_division=d) for d in divisions]
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    assert match.rating_format == "mixed_doubles"
    return match, players


def test_edit_match_refuses_a_new_inactive_player(event: Event) -> None:
    match: Match = DoublesMatchFactory.create(event=event)
    member = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    teams = {
        team: [p.player for p in match.participants.filter(team=team)] for team in Team
    }
    outsider = PlayerFactory.create()
    _deactivate(outsider)

    with pytest.raises(MatchValidationError) as raised:
        edit_match(
            match,
            actor=member,
            format=match.format,
            team_a=teams[Team.A],
            team_b=[teams[Team.B][0], outsider],
            games=((11, 3),),
            played_on=match.played_on,
        )

    assert [(i.field, i.code) for i in raised.value.issues] == [
        ("team_b", "unknown_player")
    ]
    assert Match.objects.get(pk=match.pk).status == match.status


def test_the_locked_check_counts_a_missing_player_as_unknown() -> None:
    player = PlayerFactory.create()

    with pytest.raises(MatchValidationError):
        refuse_inactive_players([player.pk], [player.pk + 100000], lock=True)


def test_the_acting_account_is_checked_again_under_its_lock() -> None:
    account = UserFactory.create()
    hold_submitting_account(account)
    hold_submitting_account(None)
    account.is_active = False
    account.save(update_fields=["is_active"])

    with pytest.raises(MatchPermissionDenied):
        hold_submitting_account(account)
