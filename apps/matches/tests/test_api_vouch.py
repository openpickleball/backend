"""The vouch operation and the list of holds (#270, ADR 0034).

``POST /api/v1/matches/{code}/vouch`` vouches that a held match is not a
duplicate. ``GET /api/v1/me/matches/holds`` lists the held matches the
signed-in Account may vouch for. A refused actor gets the same 404 as an
unknown code, and neither operation names the other match of a flag.
Made-up data only.
"""

from typing import Any

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import ClubRoleKind, MemberRole
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    FlagResolution,
    Match,
    MatchFlag,
    MatchFormat,
    MatchVouch,
)
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
from apps.matches.workflow import confirm_match, edit_match, void_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

HOLDS = "/api/v1/me/matches/holds"
UNKNOWN_MATCH = "M-0000-0000-0000"


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _vouch(account: Any, code: str) -> Any:
    return signed_in(account).post(f"/api/v1/matches/{code}/vouch")


def _same_404(refused: Any, unknown: Any) -> None:
    assert refused.status_code == unknown.status_code == 404
    assert refused.content == unknown.content


def _member(event: Event, account: User | None = None) -> User:
    member: User = account or UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=member, role=MemberRole.MATCH_MANAGER
    )
    return member


def _self_reported(players: list[Player], games: tuple[tuple[int, int], ...]) -> Match:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(match, actor=players[0].account)
    confirm_match(match, actor=players[2].account)
    return match


@pytest.fixture
def pair(confirmed: Match, players: list[Player]) -> tuple[Match, Match]:
    later = _self_reported(players, ((11, 5), (11, 6)))
    assert MatchFlag.objects.filter(match=later, duplicate_of=confirmed).exists()
    return confirmed, later


@pytest.fixture
def mixed(players: list[Player], event: Event) -> tuple[Match, Match]:
    """An Event match and a later self-reported near duplicate."""
    earlier = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=_member(event),
        event=event,
    ).match
    later = _self_reported(players, ((11, 5), (11, 6)))
    assert MatchFlag.objects.filter(match=later, duplicate_of=earlier).exists()
    return earlier, later


# Vouching (AC 1, 2).


def test_vouches_from_both_teams_on_both_matches_release_the_hold(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, later = pair

    for match in pair:
        for player in (players[0], players[3]):
            response = _vouch(player.account, match.public_code)
            assert response.status_code == 200
            body = response.json()
            assert body["code"] == match.public_code
            other = later if match == earlier else earlier
            assert other.public_code not in response.content.decode()

    assert MatchFlag.objects.get().resolution == FlagResolution.APPROVED
    assert set(Match.objects.counted()) == {earlier, later}


def test_an_organizer_vouch_and_one_team_keep_the_hold(
    mixed: tuple[Match, Match], players: list[Player], event: Event
) -> None:
    earlier, later = mixed

    assert _vouch(_member(event), earlier.public_code).status_code == 200
    assert _vouch(players[0].account, later.public_code).status_code == 200

    assert MatchFlag.objects.get().resolved_at is None
    assert not Match.objects.counted().exists()


def test_a_repeat_vouch_returns_the_match_and_writes_nothing(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, _later = pair
    assert _vouch(players[0].account, earlier.public_code).status_code == 200

    response = _vouch(players[0].account, earlier.public_code)

    assert response.status_code == 200
    assert MatchVouch.objects.count() == 1


# State (AC 4, 5).


def test_a_voided_match_gets_409(
    pair: tuple[Match, Match], players: list[Player], moderator: User
) -> None:
    earlier, _later = pair
    void_match(earlier, actor=moderator, reason="Made-up reason.")

    response = _vouch(players[0].account, earlier.public_code)

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


def test_a_match_with_no_hold_gets_409(confirmed: Match, players: list[Player]) -> None:
    response = _vouch(players[0].account, confirmed.public_code)

    assert response.status_code == 409


# Refused actors get the unknown-code 404 (AC 3).


def test_a_non_participant_gets_the_unknown_code_404(
    pair: tuple[Match, Match],
) -> None:
    earlier, _later = pair
    outsider = PlayerFactory.create().account

    _same_404(_vouch(outsider, earlier.public_code), _vouch(outsider, UNKNOWN_MATCH))


def test_a_refused_actor_on_a_voided_match_gets_404_not_409(
    pair: tuple[Match, Match], moderator: User
) -> None:
    earlier, _later = pair
    void_match(earlier, actor=moderator, reason="Made-up reason.")
    outsider = PlayerFactory.create().account

    _same_404(_vouch(outsider, earlier.public_code), _vouch(outsider, UNKNOWN_MATCH))


def test_a_participant_of_an_event_match_gets_404(
    mixed: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, _later = mixed
    account = players[0].account

    _same_404(_vouch(account, earlier.public_code), _vouch(account, UNKNOWN_MATCH))


def test_a_member_who_plays_in_the_event_match_gets_404(
    mixed: tuple[Match, Match], players: list[Player], event: Event
) -> None:
    earlier, _later = mixed
    member = _member(event, players[0].account)

    _same_404(_vouch(member, earlier.public_code), _vouch(member, UNKNOWN_MATCH))


def test_a_member_in_the_correction_chain_gets_404(
    players: list[Player], event: Event
) -> None:
    original = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=_member(event),
        event=event,
    ).match
    newcomer = PlayerFactory.create()
    corrected = edit_match(
        original,
        actor=_member(event),
        format=MatchFormat.DOUBLES,
        team_a=[players[0], newcomer],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=WORKFLOW_PLAYED_ON,
    )
    _self_reported([players[0], newcomer, *players[2:]], ((11, 5), (11, 6)))
    member = _member(event, players[1].account)

    _same_404(_vouch(member, corrected.public_code), _vouch(member, UNKNOWN_MATCH))


def test_a_member_of_another_organizer_gets_404(mixed: tuple[Match, Match]) -> None:
    earlier, _later = mixed
    member = _member(EventFactory.create(organizer=verified_organizer()))

    _same_404(_vouch(member, earlier.public_code), _vouch(member, UNKNOWN_MATCH))


def test_a_moderator_who_is_not_a_member_gets_404(
    mixed: tuple[Match, Match],
) -> None:
    moderator: User = UserFactory.create(is_staff=True)
    moderator.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    unknown = _vouch(moderator, UNKNOWN_MATCH)

    for match in mixed:
        _same_404(_vouch(moderator, match.public_code), unknown)


def test_a_club_admin_gets_404(players: list[Player], event: Event) -> None:
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    earlier = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=_member(event),
        event=event,
        club=club,
    ).match
    _self_reported(players, ((11, 5), (11, 6)))
    club_admin: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=club_admin, role=ClubRoleKind.ADMIN)

    _same_404(
        _vouch(club_admin, earlier.public_code), _vouch(club_admin, UNKNOWN_MATCH)
    )


def test_a_malformed_code_gets_the_same_404(pair: tuple[Match, Match]) -> None:
    outsider = PlayerFactory.create().account

    _same_404(_vouch(outsider, "not-a-code"), _vouch(outsider, UNKNOWN_MATCH))


def test_signing_in_is_required(pair: tuple[Match, Match]) -> None:
    earlier, _later = pair

    response = Client().post(f"/api/v1/matches/{earlier.public_code}/vouch")

    assert response.status_code == 401
    assert Client().get(HOLDS).status_code == 401


# The list of holds.


def _codes(response: Any) -> list[str]:
    return [item["match"]["code"] for item in response.json()["items"]]


def test_a_player_lists_the_held_matches_they_play_in(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, later = pair

    response = signed_in(players[0].account).get(HOLDS)

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert _codes(response) == [later.public_code, earlier.public_code]
    assert [item["vouched"] for item in body["items"]] == [False, False]


def test_the_list_shows_what_the_viewers_team_vouched(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, _later = pair
    _vouch(players[0].account, earlier.public_code)

    teammate = signed_in(players[1].account).get(HOLDS).json()["items"]
    opponent = signed_in(players[2].account).get(HOLDS).json()["items"]

    assert {item["match"]["code"]: item["vouched"] for item in teammate} == {
        earlier.public_code: True,
        pair[1].public_code: False,
    }
    assert all(not item["vouched"] for item in opponent)


def test_an_organizer_lists_only_its_event_match(
    mixed: tuple[Match, Match], event: Event
) -> None:
    earlier, later = mixed

    response = signed_in(_member(event)).get(HOLDS)

    assert _codes(response) == [earlier.public_code]
    assert later.public_code not in response.content.decode()


def test_a_player_of_the_pair_does_not_see_the_event_match(
    mixed: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, later = mixed

    response = signed_in(players[0].account).get(HOLDS)

    assert _codes(response) == [later.public_code]
    assert earlier.public_code not in response.content.decode()


def test_an_outsider_and_a_moderator_list_nothing(
    pair: tuple[Match, Match], moderator: User
) -> None:
    for account in (PlayerFactory.create().account, moderator, UserFactory.create()):
        response = signed_in(account).get(HOLDS)
        assert response.json() == {"items": [], "count": 0}


def test_a_released_match_leaves_the_list(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    for match in pair:
        _vouch(players[0].account, match.public_code)
        _vouch(players[2].account, match.public_code)

    assert signed_in(players[0].account).get(HOLDS).json()["count"] == 0


def _empty_list(account: Any) -> None:
    assert signed_in(account).get(HOLDS).json() == {"items": [], "count": 0}


def test_a_member_in_the_correction_chain_lists_nothing(
    players: list[Player], event: Event
) -> None:
    original = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=_member(event),
        event=event,
    ).match
    newcomer = PlayerFactory.create()
    corrected = edit_match(
        original,
        actor=_member(event),
        format=MatchFormat.DOUBLES,
        team_a=[players[0], newcomer],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=WORKFLOW_PLAYED_ON,
    )
    _self_reported([players[0], newcomer, *players[2:]], ((11, 5), (11, 6)))
    assert MatchFlag.objects.filter(duplicate_of=corrected).exists()
    # Player 1 played only the original, so is in the chain, not the match.
    member = _member(event, players[1].account)

    _empty_list(member)
    assert set(_codes(signed_in(_member(event)).get(HOLDS))) == {corrected.public_code}


def test_a_club_admin_lists_nothing(players: list[Player], event: Event) -> None:
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 7), (11, 9)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=_member(event),
        event=event,
        club=club,
    )
    _self_reported(players, ((11, 5), (11, 6)))
    club_admin: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=club_admin, role=ClubRoleKind.ADMIN)

    _empty_list(club_admin)


def test_a_member_of_another_organizer_lists_nothing(
    mixed: tuple[Match, Match],
) -> None:
    _empty_list(_member(EventFactory.create(organizer=verified_organizer())))
