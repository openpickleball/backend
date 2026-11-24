"""Creating a Player, and the player-only profile and privacy changes."""

from typing import TYPE_CHECKING, Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.players.models import Player, PlayerPrivacy, Visibility
from apps.players.services import (
    PROFILE_FIELDS,
    NotOwnPlayer,
    create_player,
    update_privacy,
    update_profile,
)

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
        legal_name="Made-up Legal Name",
        gender_division="mens",
        city="Madeupville",
        region="Made-up Region",
    )


def _events() -> list[AuditEvent]:
    return list(AuditEvent.objects.order_by("id"))


# create_player


def test_profile_fields_are_the_planned_seven() -> None:
    assert set(PROFILE_FIELDS) == {
        "display_name",
        "legal_name",
        "birth_year",
        "birth_month",
        "gender_division",
        "city",
        "region",
    }


def test_create_player_creates_privacy_with_defaults(player: Player) -> None:
    privacy = PlayerPrivacy.objects.get(player=player)

    for setting in PlayerPrivacy.SETTINGS:
        assert getattr(privacy, setting) == Visibility.SIGNED_IN


def test_create_player_records_no_event(player: Player) -> None:
    assert _events() == []


def test_create_player_stores_the_values(player: Player, owner: "_User") -> None:
    stored = Player.objects.get(pk=player.pk)

    assert stored.account == owner
    assert stored.display_name == "Made-up Player"
    assert (stored.birth_year, stored.birth_month) == (1990, 6)
    assert stored.gender_division == "mens"


@pytest.mark.parametrize("field", ["birth_year", "birth_month", "gender_division"])
def test_create_player_without_a_required_detail_fails(field: str) -> None:
    values: dict[str, Any] = {
        "display_name": "Made-up Player",
        "birth_year": 1990,
        "birth_month": 6,
        "gender_division": "womens",
        field: None,
    }
    account = UserFactory.create()

    with pytest.raises(ValidationError) as caught:
        create_player(account=account, **values)

    assert field in caught.value.message_dict
    assert not Player.objects.exists()
    assert not PlayerPrivacy.objects.exists()


def test_create_player_with_an_empty_gender_division_fails() -> None:
    with pytest.raises(ValidationError) as caught:
        create_player(
            account=UserFactory.create(),
            display_name="Made-up Player",
            birth_year=1990,
            birth_month=6,
            gender_division="",
        )

    assert caught.value.message_dict == {
        "gender_division": ["Choose a gender division."]
    }
    assert not Player.objects.exists()


def test_create_player_needs_a_gender_division_argument() -> None:
    with pytest.raises(TypeError):
        create_player(  # type: ignore[call-arg]
            account=UserFactory.create(),
            display_name="Made-up Player",
            birth_year=1990,
            birth_month=6,
        )

    assert not Player.objects.exists()


def test_create_player_twice_for_one_account_fails(player: Player) -> None:
    with pytest.raises(ValidationError) as caught:
        create_player(
            account=player.account,
            display_name="Another Made-up Player",
            birth_year=1991,
            birth_month=2,
            gender_division="mens",
        )

    assert "account" in caught.value.message_dict
    assert Player.objects.count() == 1


# update_profile


def test_a_stored_empty_gender_division_does_not_block_another_change(
    player: Player, owner: "_User"
) -> None:
    # A Player stored before issue #215 may have none.
    Player.objects.filter(pk=player.pk).update(gender_division="")
    player.refresh_from_db()

    update_profile(player=player, actor=owner, changes={"city": "Othertown"})

    stored = Player.objects.get(pk=player.pk)
    assert (stored.city, stored.gender_division) == ("Othertown", "")


def test_owner_updates_profile(player: Player, owner: "_User") -> None:
    update_profile(
        player=player,
        actor=owner,
        changes={"display_name": "New Made-up Name", "city": "Othertown"},
    )

    stored = Player.objects.get(pk=player.pk)
    assert stored.display_name == "New Made-up Name"
    assert stored.city == "Othertown"
    assert player.display_name == "New Made-up Name"


def test_profile_change_records_field_names_only(
    player: Player, owner: "_User"
) -> None:
    old = {name: getattr(player, name) for name in PROFILE_FIELDS}
    new: dict[str, object] = {
        "display_name": "New Made-up Name",
        "legal_name": "New Made-up Legal",
        "city": "Othertown",
        "region": "Other Region",
    }

    update_profile(player=player, actor=owner, changes=new)

    [event] = _events()
    assert event.action == AuditAction.PLAYER_PROFILE_CHANGE
    assert event.actor_user_id == owner.pk
    assert event.target_content_type == ContentType.objects.get_for_model(Player)
    assert event.target_object_id == str(player.pk)
    assert event.before is None
    assert event.after == {"changed_fields": sorted(new)}
    assert event.reason == ""
    stored = f"{event.before} {event.after} {event.reason}"
    for value in [*old.values(), *new.values()]:
        if value in ("", None):
            continue
        assert str(value) not in stored.replace("changed_fields", "")


def test_profile_change_lists_only_changed_fields(
    player: Player, owner: "_User"
) -> None:
    update_profile(
        player=player,
        actor=owner,
        changes={"display_name": "Made-up Player", "city": "Othertown"},
    )

    [event] = _events()
    assert event.after == {"changed_fields": ["city"]}


def test_profile_change_with_nothing_changed_records_nothing(
    player: Player, owner: "_User"
) -> None:
    update_profile(
        player=player, actor=owner, changes={"display_name": "Made-up Player"}
    )

    assert _events() == []


def test_whitespace_only_difference_is_not_a_change(
    player: Player, owner: "_User"
) -> None:
    update_profile(
        player=player, actor=owner, changes={"display_name": "  Made-up Player  "}
    )

    assert _events() == []


def test_invalid_profile_change_saves_nothing(player: Player, owner: "_User") -> None:
    with pytest.raises(ValidationError):
        update_profile(
            player=player, actor=owner, changes={"city": "Othertown", "region": 7}
        )

    stored = Player.objects.get(pk=player.pk)
    assert (stored.city, stored.region) == ("Madeupville", player.region)
    assert _events() == []


def test_unknown_profile_field_is_refused(player: Player, owner: "_User") -> None:
    with pytest.raises(ValueError, match="Not a field that can be changed") as caught:
        update_profile(player=player, actor=owner, changes={"account": "x-made-up"})

    assert "x-made-up" not in str(caught.value)
    assert _events() == []


# update_privacy


def test_owner_updates_privacy(player: Player, owner: "_User") -> None:
    update_privacy(
        player=player,
        actor=owner,
        changes={"stats_visibility": Visibility.PRIVATE},
    )

    privacy = PlayerPrivacy.objects.get(player=player)
    assert privacy.stats_visibility == Visibility.PRIVATE
    assert privacy.city_region_visibility == Visibility.SIGNED_IN


def test_privacy_change_records_changed_levels(player: Player, owner: "_User") -> None:
    update_privacy(
        player=player,
        actor=owner,
        changes={
            "stats_visibility": Visibility.PRIVATE,
            "home_club_visibility": Visibility.PRIVATE,
            "city_region_visibility": Visibility.SIGNED_IN,
        },
    )

    [event] = _events()
    assert event.action == AuditAction.PRIVACY_CHANGE
    assert event.actor_user_id == owner.pk
    assert event.target_content_type == ContentType.objects.get_for_model(Player)
    assert event.target_object_id == str(player.pk)
    assert event.before == {
        "home_club_visibility": "signed_in",
        "stats_visibility": "signed_in",
    }
    assert event.after == {
        "home_club_visibility": "private",
        "stats_visibility": "private",
    }
    assert event.reason == ""


def test_privacy_change_with_nothing_changed_records_nothing(
    player: Player, owner: "_User"
) -> None:
    update_privacy(
        player=player,
        actor=owner,
        changes={"stats_visibility": Visibility.SIGNED_IN},
    )

    assert _events() == []


def test_invalid_privacy_level_saves_nothing(player: Player, owner: "_User") -> None:
    with pytest.raises(ValidationError) as caught:
        update_privacy(
            player=player,
            actor=owner,
            changes={"stats_visibility": "private", "home_club_visibility": "zqxj"},
        )

    assert "zqxj" not in str(caught.value)
    privacy = PlayerPrivacy.objects.get(player=player)
    assert privacy.stats_visibility == Visibility.SIGNED_IN
    assert _events() == []


def test_unknown_privacy_setting_is_refused(player: Player, owner: "_User") -> None:
    with pytest.raises(ValueError, match="Not a field that can be changed") as caught:
        update_privacy(
            player=player,
            actor=owner,
            changes={"display_name_visibility": "private"},
        )

    assert "display_name_visibility" not in str(caught.value)
    assert _events() == []


# Only the player


def _not_owner(kind: str, owner: "_User") -> "_User | AnonymousUser | None":
    match kind:
        case "other":
            return UserFactory.create()
        case "staff":
            return UserFactory.create(is_staff=True, is_superuser=True)
        case "inactive_owner":
            owner.is_active = False
            owner.save(update_fields=["is_active"])
            return owner
        case "anonymous":
            return AnonymousUser()
        case _:
            return None


NOT_OWNERS = ["other", "staff", "inactive_owner", "anonymous", "none"]


@pytest.mark.parametrize("kind", NOT_OWNERS)
def test_only_the_player_updates_profile(
    player: Player, owner: "_User", kind: str
) -> None:
    actor = _not_owner(kind, owner)

    with pytest.raises(NotOwnPlayer):
        update_profile(player=player, actor=actor, changes={"city": "Othertown"})

    assert Player.objects.get(pk=player.pk).city == "Madeupville"
    assert _events() == []


@pytest.mark.parametrize("kind", NOT_OWNERS)
def test_only_the_player_updates_privacy(
    player: Player, owner: "_User", kind: str
) -> None:
    actor = _not_owner(kind, owner)

    with pytest.raises(NotOwnPlayer):
        update_privacy(
            player=player,
            actor=actor,
            changes={"stats_visibility": Visibility.PRIVATE},
        )

    privacy = PlayerPrivacy.objects.get(player=player)
    assert privacy.stats_visibility == Visibility.SIGNED_IN
    assert _events() == []


def test_not_own_player_is_a_permission_error() -> None:
    assert issubclass(NotOwnPlayer, PermissionError)


def test_ownership_is_checked_before_field_names(
    player: Player, owner: "_User"
) -> None:
    with pytest.raises(NotOwnPlayer):
        update_profile(
            player=player, actor=UserFactory.create(), changes={"account": "x"}
        )
