"""Review fix D1: resent unchanged values do not block a profile edit."""

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
        display_name="Made-up Player",
        birth_year=1990,
        birth_month=6,
        gender_division="mens",
        city="Madeupville",
    )


def _changed_fields() -> list[str]:
    [event] = AuditEvent.objects.all()
    assert isinstance(event.after, dict)
    return list(event.after["changed_fields"])


def test_d1_resent_invalid_display_name_does_not_block(
    player: Player, owner: "_User"
) -> None:
    Player.objects.filter(pk=player.pk).update(display_name="́")
    player.refresh_from_db()

    update_profile(
        player=player,
        actor=owner,
        changes={"display_name": player.display_name, "city": "Springfield"},
    )

    stored = Player.objects.get(pk=player.pk)
    assert stored.city == "Springfield"
    assert stored.display_name == "́"
    assert _changed_fields() == ["city"]


def test_d1_resent_padded_display_name_is_unchanged(
    player: Player, owner: "_User"
) -> None:
    update_profile(
        player=player,
        actor=owner,
        changes={"display_name": "  Made-up Player ", "city": "Springfield"},
    )

    assert _changed_fields() == ["city"]


def test_d1_resent_none_for_empty_optional_field_is_unchanged(
    player: Player, owner: "_User"
) -> None:
    update_profile(
        player=player,
        actor=owner,
        changes={"legal_name": None, "region": None, "city": "Springfield"},
    )

    assert _changed_fields() == ["city"]


def test_d1_changed_invalid_display_name_is_still_refused(
    player: Player, owner: "_User"
) -> None:
    Player.objects.filter(pk=player.pk).update(display_name="́")
    player.refresh_from_db()

    with pytest.raises(ValidationError) as caught:
        update_profile(
            player=player,
            actor=owner,
            changes={"display_name": "​", "city": "Springfield"},
        )

    assert caught.value.message_dict == {"display_name": ["Enter a display name."]}
    assert Player.objects.get(pk=player.pk).city == "Madeupville"
    assert not AuditEvent.objects.exists()
