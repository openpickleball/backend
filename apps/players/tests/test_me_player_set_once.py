"""The birth year and month and the gender division are set once (#225).

They are set when the Player is created, from signup or the create body,
and the player can no longer change them (ADR 0023). ``PATCH
/api/v1/me/player`` refuses each of them with a fixed 422 on that field,
whatever the value: a new one, the stored one, or null. The OpenAPI
document no longer lists them for PATCH. ``update_profile()`` refuses
them too. Nothing changes and no Audit Event is recorded. The display name
and the legal name are refused the same way, with the same code, since
they are built from the names given at signup (ADR 0062). Made-up data
only.
"""

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client

from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.audit.models import AuditEvent
from apps.openapi.schema import schema_path
from apps.players.age import age_limit_date
from apps.players.models import Player
from apps.players.services import update_profile
from apps.players.signup import SIGNUP_REFUSED_SESSION_KEY
from apps.players.tests.factories import PlayerFactory
from apps.players.tests.test_me_player_api import call, verified_session

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

CLIENTS = [APP, BROWSER]
ME_PLAYER = "/api/v1/me/player"
BIRTH_MESSAGE = "Your birth year and month can't be changed."
DIVISION_MESSAGE = "Your gender division can't be changed."
MESSAGES = {
    "birth_year": BIRTH_MESSAGE,
    "birth_month": BIRTH_MESSAGE,
    "gender_division": DIVISION_MESSAGE,
}
STORED = {"birth_year": 1985, "birth_month": 3, "gender_division": "womens"}
# A value other than the stored one, for each field.
OTHER = {"birth_year": 1979, "birth_month": 11, "gender_division": "mens"}
SET_ONCE = list(MESSAGES)


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> Iterator[None]:
    """allauth keeps its rate limits in the cache: start each test empty."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def mine() -> Player:
    return PlayerFactory.create(
        display_name="Robin Q.",
        city="Springfield",
        **STORED,
    )


def patch(player: Player, data: Any) -> Any:
    client = Client()
    client.force_login(player.account)
    return client.patch(ME_PLAYER, data, content_type="application/json")


def errors(response: Any) -> list[dict[str, str]]:
    return list(response.json()["errors"])


def refusal(*names: str) -> list[dict[str, str]]:
    return [
        {"field": f"body.{name}", "code": "set_once", "message": MESSAGES[name]}
        for name in names
    ]


def stored(player: Player) -> dict[str, object]:
    player.refresh_from_db()
    return {name: getattr(player, name) for name in (*SET_ONCE, "display_name")}


UNCHANGED = {**STORED, "display_name": "Robin Q."}


# PATCH /api/v1/me/player


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("name", SET_ONCE)
def test_patch_refuses_a_set_once_field_on_both_clients(base: str, name: str) -> None:
    session: HeadlessSession = verified_session(base)
    created = call(session, "POST", {})
    assert created.status_code == 201, created.json()
    before = {key: created.json()[key] for key in SET_ONCE}
    assert before[name] != OTHER[name]

    response = call(session, "PATCH", {name: OTHER[name]})

    assert response.status_code == 422
    assert errors(response) == refusal(name)
    player = Player.objects.get()
    assert {key: getattr(player, key) for key in SET_ONCE} == before
    assert not AuditEvent.objects.exists()


@pytest.mark.parametrize("name", SET_ONCE)
@pytest.mark.parametrize("kind", ["other", "stored", "null"])
def test_patch_refuses_any_value_of_a_set_once_field(
    mine: Player, name: str, kind: str
) -> None:
    value = {"other": OTHER[name], "stored": STORED[name], "null": None}[kind]

    response = patch(mine, {name: value})

    assert response.status_code == 422
    assert errors(response) == refusal(name)
    assert stored(mine) == UNCHANGED
    assert not AuditEvent.objects.exists()


def test_patch_refuses_all_three_at_once(mine: Player) -> None:
    response = patch(mine, OTHER)

    assert response.status_code == 422
    assert errors(response) == refusal(*SET_ONCE)
    assert stored(mine) == UNCHANGED


def test_a_set_once_field_blocks_the_rest_of_the_patch(mine: Player) -> None:
    response = patch(
        mine,
        {
            "city": "Shelbyville",
            "birth_year": OTHER["birth_year"],
            "privacy": {"city_region_visibility": "private"},
        },
    )

    assert response.status_code == 422
    assert errors(response) == refusal("birth_year")
    assert stored(mine) == UNCHANGED
    mine.privacy.refresh_from_db()
    assert mine.privacy.city_region_visibility != "private"
    assert not AuditEvent.objects.exists()


@pytest.mark.parametrize("value", [1979, "1979", True, 1979.5, [1979], {"y": 1979}])
def test_the_refusal_never_repeats_the_value(mine: Player, value: object) -> None:
    response = patch(mine, {"birth_year": value})

    assert response.status_code == 422
    assert errors(response) == refusal("birth_year")
    assert "1979" not in response.content.decode()


def test_a_stored_empty_gender_division_cannot_be_set_later(mine: Player) -> None:
    # A Player stored before issue #215, or after a removal, may have none.
    Player.objects.filter(pk=mine.pk).update(gender_division="")

    response = patch(mine, {"gender_division": "mens"})

    assert response.status_code == 422
    assert errors(response) == refusal("gender_division")
    mine.refresh_from_db()
    assert mine.gender_division == ""


@pytest.mark.parametrize("base", CLIENTS)
def test_an_under_13_birth_patch_sets_no_flag(base: str) -> None:
    session: HeadlessSession = verified_session(base)
    assert call(session, "POST", {}).status_code == 201
    young = {"birth_year": age_limit_date().year - 5, "birth_month": 1}

    response = call(session, "PATCH", young)

    assert response.status_code == 422
    assert errors(response) == refusal("birth_year", "birth_month")
    assert SIGNUP_REFUSED_SESSION_KEY not in session.store()


def test_other_profile_fields_still_change(mine: Player) -> None:
    response = patch(mine, {"region": "Iowa", "city": "Shelbyville"})

    assert response.status_code == 200
    assert response.json()["display_name"] == "Robin Q."
    assert response.json()["city"] == "Shelbyville"
    assert {key: response.json()[key] for key in SET_ONCE} == STORED


def test_the_openapi_document_lists_no_set_once_field_for_patch() -> None:
    document = json.loads(schema_path().read_text(encoding="utf-8"))
    properties = document["components"]["schemas"]["PlayerUpdateIn"]["properties"]

    assert set(properties) == {"city", "region", "privacy"}


# update_profile()


@pytest.mark.parametrize("name", SET_ONCE)
@pytest.mark.parametrize("kind", ["other", "stored", "null"])
def test_update_profile_refuses_a_set_once_field(
    mine: Player, name: str, kind: str
) -> None:
    value = {"other": OTHER[name], "stored": STORED[name], "null": None}[kind]
    owner: _User = mine.account

    with pytest.raises(ValidationError) as caught:
        update_profile(
            player=mine, actor=owner, changes={name: value, "city": "Shelbyville"}
        )

    assert caught.value.message_dict == {name: [MESSAGES[name]]}
    [error] = caught.value.error_dict[name]
    assert error.code == "set_once"
    mine.refresh_from_db()
    assert mine.city == "Springfield"
    assert stored(mine) == UNCHANGED
    assert not AuditEvent.objects.exists()
