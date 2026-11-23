"""Review fix C14: a profile change validates only the fields it changes."""

from typing import TYPE_CHECKING

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

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
        display_name="Made-up Player",
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


@pytest.mark.parametrize("field", ["legal_name", "city", "region"])
def test_c14_stored_control_character_does_not_block_other_change(
    player: Player, owner: "_User", field: str
) -> None:
    Player.objects.filter(pk=player.pk).update(**{field: "Zq\x07xj"})
    player.refresh_from_db()

    update_profile(
        player=player, actor=owner, changes={"display_name": "New Made-up Name"}
    )

    stored = Player.objects.get(pk=player.pk)
    assert stored.display_name == "New Made-up Name"
    assert getattr(stored, field) == "Zq\x07xj"
    [event] = AuditEvent.objects.all()
    assert event.after == {"changed_fields": ["display_name"]}


def test_c14_changing_display_name_to_invalid_is_still_refused(
    player: Player, owner: "_User"
) -> None:
    with pytest.raises(ValidationError) as caught:
        update_profile(player=player, actor=owner, changes={"display_name": "́"})

    assert caught.value.message_dict == {"display_name": ["Enter a display name."]}
    assert Player.objects.get(pk=player.pk).display_name == "Made-up Player"
    assert not AuditEvent.objects.exists()


def test_c14_changed_text_field_still_gets_field_validation(
    player: Player, owner: "_User"
) -> None:
    with pytest.raises(ValidationError) as caught:
        update_profile(player=player, actor=owner, changes={"city": "x" * 101})

    assert "city" in caught.value.message_dict
    assert not AuditEvent.objects.exists()


def test_c14_changed_birth_month_still_gets_the_future_rule(
    player: Player, owner: "_User"
) -> None:
    today = timezone.localdate()
    Player.objects.filter(pk=player.pk).update(birth_year=today.year)
    player.refresh_from_db()
    if today.month == 12:
        pytest.skip("No later month in the current year.")

    with pytest.raises(ValidationError) as caught:
        update_profile(
            player=player, actor=owner, changes={"birth_month": today.month + 1}
        )

    assert "birth_month" in caught.value.message_dict
    assert not AuditEvent.objects.exists()


def test_c14_unicode_16_display_name_change_is_accepted(
    player: Player, owner: "_User"
) -> None:
    update_profile(player=player, actor=owner, changes={"display_name": "\U0001fae9"})

    assert Player.objects.get(pk=player.pk).display_name == "\U0001fae9"
    [event] = AuditEvent.objects.all()
    assert event.after == {"changed_fields": ["display_name"]}
