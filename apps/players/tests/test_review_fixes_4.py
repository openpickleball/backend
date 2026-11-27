"""Review fix C14: a profile change validates only the fields it changes."""

from typing import TYPE_CHECKING

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.players.models import Player
from apps.players.services import create_player, update_profile

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


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
        gender_division="mens",
        city="Madeupville",
    )


def test_c14_stored_invalid_display_name_does_not_block_city_change(
    player: Player, owner: "_User"
) -> None:
    # A raw write the database allows but today's rule refuses.
    Player.objects.filter(pk=player.pk).update(display_name="́")
    player.refresh_from_db()

    update_profile(player=player, actor=owner, changes={"city": "Springfield"})

    stored = Player.objects.get(pk=player.pk)
    assert stored.city == "Springfield"
    assert stored.display_name == "́"
    [event] = AuditEvent.objects.all()
    assert event.after == {"changed_fields": ["city"]}


@pytest.mark.parametrize("field", ["display_name", "legal_name", "region"])
def test_c14_stored_control_character_does_not_block_other_change(
    player: Player, owner: "_User", field: str
) -> None:
    Player.objects.filter(pk=player.pk).update(**{field: "Zq\x07xj"})
    player.refresh_from_db()

    update_profile(player=player, actor=owner, changes={"city": "Springfield"})

    stored = Player.objects.get(pk=player.pk)
    assert stored.city == "Springfield"
    assert getattr(stored, field) == "Zq\x07xj"
    [event] = AuditEvent.objects.all()
    assert event.after == {"changed_fields": ["city"]}


def test_c14_changed_text_field_still_gets_field_validation(
    player: Player, owner: "_User"
) -> None:
    with pytest.raises(ValidationError) as caught:
        update_profile(player=player, actor=owner, changes={"city": "x" * 101})

    assert "city" in caught.value.message_dict
    assert not AuditEvent.objects.exists()
