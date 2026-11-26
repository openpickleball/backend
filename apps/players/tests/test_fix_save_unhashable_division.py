"""Player.save() refuses an unhashable gender division with a
ValidationError, not a TypeError (issue #115, ADR 0046)."""

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.players.models import Player

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("value", [["mens"], {"mens": 1}, {"mens"}], ids=str)
def test_an_unhashable_division_is_a_validation_error(value: object) -> None:
    player = Player(
        account=UserFactory.create(),
        display_name="Made-up Player",
        birth_year=1990,
        birth_month=6,
        gender_division=value,  # type: ignore[misc]
    )

    with pytest.raises(ValidationError) as caught:
        player.save()

    assert set(caught.value.message_dict) == {"gender_division"}
    assert not Player.objects.exists()
