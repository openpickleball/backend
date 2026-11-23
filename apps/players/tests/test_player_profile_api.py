"""A player's profile at /api/v1/players/{code} (issue #124).

Any signed-in Account sees a player's OPEN Code and display name, the
same details player search shows (ADR 0018). City and region show only
when the player's ``city_region_visibility`` lets the viewer
(``PlayerPrivacy.visible_to``). An unknown code, a malformed code, and a
player whose Account is inactive all get the same 404 (ADR 0008, ADR
0015). Made-up data only.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.players.models import Player, Visibility
from apps.players.services import update_privacy
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate

pytestmark = pytest.mark.django_db

NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    # The search check below uses the search throttle's counters.
    cache.clear()
    yield
    cache.clear()


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def profile(client: Client, code: str) -> Any:
    return client.get(f"/api/v1/players/{code}")


@pytest.fixture
def player() -> Player:
    return PlayerFactory.create(
        display_name="Robin Q.", city="Springfield", region="Ohio"
    )


@pytest.fixture
def viewer() -> Client:
    return signed_in(UserFactory.create())


def test_another_account_sees_code_name_city_and_region(
    player: Player, viewer: Client
) -> None:
    response = profile(viewer, player.public_code)

    assert response.status_code == 200
    assert response.json() == {
        "code": player.public_code,
        "display_name": "Robin Q.",
        "city": "Springfield",
        "region": "Ohio",
    }


def test_private_city_and_region_are_null_for_another_account(
    player: Player, viewer: Client
) -> None:
    update_privacy(
        player=player,
        actor=player.account,
        changes={"city_region_visibility": Visibility.PRIVATE},
    )

    body = profile(viewer, player.public_code).json()

    assert body["city"] is None
    assert body["region"] is None
    assert body["display_name"] == "Robin Q."


def test_the_player_sees_their_own_private_city_and_region(player: Player) -> None:
    update_privacy(
        player=player,
        actor=player.account,
        changes={"city_region_visibility": Visibility.PRIVATE},
    )

    body = profile(signed_in(player.account), player.public_code).json()

    assert body["city"] == "Springfield"
    assert body["region"] == "Ohio"


def test_an_empty_city_and_region_are_null(viewer: Client) -> None:
    # Empty and hidden look alike, so the answer never reveals the setting.
    player = PlayerFactory.create(city="", region="")

    body = profile(viewer, player.public_code).json()

    assert body["city"] is None
    assert body["region"] is None


def test_the_profile_never_shows_other_personal_fields(
    player: Player, viewer: Client
) -> None:
    body = profile(viewer, player.public_code).json()

    assert set(body) == {"code", "display_name", "city", "region"}


def test_a_lowercase_code_finds_the_player(player: Player, viewer: Client) -> None:
    response = profile(viewer, player.public_code.lower())

    assert response.status_code == 200
    assert response.json()["code"] == player.public_code


def test_unknown_malformed_and_inactive_players_get_the_same_404(
    player: Player, viewer: Client
) -> None:
    account = player.account
    account.is_active = False
    account.save(update_fields=["is_active"])

    inactive = profile(viewer, player.public_code)
    unknown = profile(viewer, generate(CodeType.PLAYER))
    malformed = profile(viewer, "not-a-code")
    other_type = profile(viewer, generate(CodeType.MATCH))

    for response in (inactive, unknown, malformed, other_type):
        assert response.status_code == 404
        assert response.json() == NOT_FOUND


def test_a_signed_out_request_gets_401(player: Player) -> None:
    response = profile(Client(), player.public_code)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_player_search_is_not_shadowed_by_the_profile(
    player: Player, viewer: Client
) -> None:
    response = viewer.get("/api/v1/players/search", {"q": "Robin"})

    assert response.status_code == 200
    assert response.json()["items"] == [
        {"code": player.public_code, "display_name": "Robin Q."}
    ]


def test_the_match_history_still_answers(player: Player) -> None:
    response = signed_in(player.account).get(
        f"/api/v1/players/{player.public_code}/matches"
    )

    assert response.status_code == 200
