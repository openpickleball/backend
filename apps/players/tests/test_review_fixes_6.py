"""Review fixes E1 (padded stored values) and E2 (non-text input)."""

from typing import TYPE_CHECKING, Any

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.players.models import Player
from apps.players.services import create_player, update_profile

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

HUGE = 10**5000


@pytest.fixture
def owner() -> "_User":
    return UserFactory.create()


@pytest.fixture
def player(owner: "_User") -> Player:
    return create_player(
        account=owner,
        first_name="Made-up",
        last_name="Player",
        birth_year=1990,
        birth_month=6,
        city="Madeupville",
        gender_division="mens",
    )


# E1: a padded stored value resent as is, or stripped, is unchanged.


@pytest.mark.parametrize("resent", [" Madeupville ", "Madeupville"])
def test_e1_resent_padded_city_is_a_no_op(
    player: Player, owner: "_User", resent: str
) -> None:
    Player.objects.filter(pk=player.pk).update(city=" Madeupville ")
    player.refresh_from_db()

    update_profile(player=player, actor=owner, changes={"city": resent})

    assert Player.objects.get(pk=player.pk).city == " Madeupville "
    assert not AuditEvent.objects.exists()


# E2: a non-text value for a text field is a ValidationError.


@pytest.mark.parametrize(
    "field", ["display_name", "legal_name", "city", "region", "gender_division"]
)
@pytest.mark.parametrize(
    "value",
    [pytest.param(HUGE, id="huge-int"), 5, 1.5, b"made-up", ["made-up"]],
)
def test_e2_non_text_value_is_refused_on_update(
    player: Player, owner: "_User", field: str, value: object
) -> None:
    with pytest.raises(ValidationError) as caught:
        update_profile(player=player, actor=owner, changes={field: value})

    assert list(caught.value.message_dict) == [field]
    assert "made-up" not in str(caught.value.message_dict)
    assert "1000" not in str(caught.value.message_dict)
    assert not AuditEvent.objects.exists()


@pytest.mark.parametrize(
    "field", ["first_name", "last_name", "city", "region", "gender_division"]
)
def test_e2_huge_int_is_refused_on_create(field: str) -> None:
    values: dict[str, Any] = {
        "first_name": "Made-up",
        "last_name": "Player",
        "birth_year": 1990,
        "birth_month": 6,
        "gender_division": "mens",
        field: HUGE,
    }

    with pytest.raises(ValidationError) as caught:
        create_player(account=UserFactory.create(), **values)

    assert field in caught.value.message_dict
    assert not Player.objects.exists()


@pytest.mark.parametrize("field", ["first_name", "last_name"])
def test_e2_none_name_is_still_required(field: str) -> None:
    values: dict[str, Any] = {
        "first_name": "Made-up",
        "last_name": "Player",
        field: None,
        "birth_year": 1990,
        "birth_month": 6,
        "gender_division": "mens",
    }

    with pytest.raises(ValidationError) as caught:
        create_player(account=UserFactory.create(), **values)

    assert field in caught.value.message_dict


def test_e2_none_still_clears_optional_fields(player: Player, owner: "_User") -> None:
    update_profile(
        player=player,
        actor=owner,
        changes={"city": None, "region": None},
    )

    stored = Player.objects.get(pk=player.pk)
    assert (stored.city, stored.region) == ("", "")
