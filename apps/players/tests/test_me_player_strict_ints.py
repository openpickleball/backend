"""Birth year and month must be real JSON integers (issue #124 review).

Pydantic's lax mode turns ``true`` into 1, ``"7"`` into 7, and ``8.0``
into 8. ``Player.clean_fields`` refuses anything but a real int, so the
API refuses them too, with fixed text that never repeats the value.
Made-up data only.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

ME_PLAYER = "/api/v1/me/player"
NOT_INTS = [True, "7", 8.0]


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def fields(response: Any) -> set[str]:
    return {error["field"] for error in response.json()["errors"]}


@pytest.mark.parametrize("name", ["birth_year", "birth_month"])
@pytest.mark.parametrize("value", NOT_INTS)
def test_patch_refuses_a_birth_value_that_is_not_an_int(
    name: str, value: object
) -> None:
    player = PlayerFactory.create(birth_year=1985, birth_month=3)

    response = signed_in(player.account).patch(
        ME_PLAYER, {name: value}, content_type="application/json"
    )

    assert response.status_code == 422
    assert fields(response) == {f"body.{name}"}
    player.refresh_from_db()
    assert (player.birth_year, player.birth_month) == (1985, 3)


@pytest.mark.parametrize("name", ["birth_year", "birth_month"])
@pytest.mark.parametrize("value", NOT_INTS)
def test_create_refuses_a_birth_value_that_is_not_an_int(
    name: str, value: object
) -> None:
    account = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    data: dict[str, object] = {
        "display_name": "Robin Q.",
        "birth_year": 1985,
        "birth_month": 3,
        name: value,
    }

    response = signed_in(account).post(ME_PLAYER, data, content_type="application/json")

    assert response.status_code == 422
    assert fields(response) == {f"body.{name}"}
    assert not Player.objects.exists()


def test_the_message_does_not_repeat_the_value() -> None:
    player = PlayerFactory.create()

    response = signed_in(player.account).patch(
        ME_PLAYER, {"birth_year": "1987"}, content_type="application/json"
    )

    assert response.status_code == 422
    assert "1987" not in response.content.decode()
