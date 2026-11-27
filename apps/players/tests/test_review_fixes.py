"""Review fixes for the Player model and services (C1 to C9)."""

from typing import TYPE_CHECKING, Any

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.players.models import Player, PlayerPrivacy, Visibility
from apps.players.services import (
    NotOwnPlayer,
    create_player,
    update_privacy,
    update_profile,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

WHOLE_NUMBER = "Enter a whole number."


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


def _build(**overrides: Any) -> Player:
    values: dict[str, Any] = {
        "account": UserFactory.create(),
        "display_name": "Made-up Player",
        "first_name": "Made-up",
        "last_name": "Player",
        "birth_year": 1990,
        "birth_month": 6,
        "gender_division": "mens",
    }
    values.update(overrides)
    return Player(**values)


# C1: the caller's cached privacy row reflects the change.


def test_c1_cached_privacy_is_updated(player: Player, owner: "_User") -> None:
    viewer = UserFactory.create()
    assert player.privacy.visible_to("stats_visibility", viewer) is True

    update_privacy(
        player=player, actor=owner, changes={"stats_visibility": Visibility.PRIVATE}
    )

    assert player.privacy.stats_visibility == Visibility.PRIVATE
    assert player.privacy.visible_to("stats_visibility", viewer) is False


# C2: ownership is checked against the stored rows.


def test_c2_profile_refused_for_player_with_account_changed_in_memory(
    player: Player,
) -> None:
    intruder = UserFactory.create()
    player.account_id = intruder.pk

    with pytest.raises(NotOwnPlayer):
        update_profile(player=player, actor=intruder, changes={"city": "Othertown"})

    assert Player.objects.get(pk=player.pk).city == "Madeupville"
    assert not AuditEvent.objects.exists()


def test_c2_privacy_refused_for_player_with_account_changed_in_memory(
    player: Player,
) -> None:
    intruder = UserFactory.create()
    player.account_id = intruder.pk

    with pytest.raises(NotOwnPlayer):
        update_privacy(
            player=player,
            actor=intruder,
            changes={"stats_visibility": Visibility.PRIVATE},
        )

    privacy = PlayerPrivacy.objects.get(pk=player.pk)
    assert privacy.stats_visibility == Visibility.SIGNED_IN
    assert not AuditEvent.objects.exists()


def test_c2_profile_refused_for_stale_actor_deactivated_in_database(
    player: Player, owner: "_User"
) -> None:
    get_user_model().objects.filter(pk=owner.pk).update(is_active=False)
    assert owner.is_active is True

    with pytest.raises(NotOwnPlayer):
        update_profile(player=player, actor=owner, changes={"city": "Othertown"})

    assert Player.objects.get(pk=player.pk).city == "Madeupville"
    assert not AuditEvent.objects.exists()


def test_c2_privacy_refused_for_stale_actor_deactivated_in_database(
    player: Player, owner: "_User"
) -> None:
    get_user_model().objects.filter(pk=owner.pk).update(is_active=False)

    with pytest.raises(NotOwnPlayer):
        update_privacy(
            player=player,
            actor=owner,
            changes={"stats_visibility": Visibility.PRIVATE},
        )

    privacy = PlayerPrivacy.objects.get(pk=player.pk)
    assert privacy.stats_visibility == Visibility.SIGNED_IN
    assert not AuditEvent.objects.exists()


# C3: unknown keys are refused without echoing them, whatever their type.


@pytest.mark.parametrize("service", [update_profile, update_privacy])
def test_c3_unknown_key_is_not_echoed(
    player: Player, owner: "_User", service: Any
) -> None:
    with pytest.raises(ValueError, match="Not a field that can be changed") as caught:
        service(player=player, actor=owner, changes={"zqxjkey": "x"})

    assert "zqxjkey" not in str(caught.value)


@pytest.mark.parametrize("service", [update_profile, update_privacy])
def test_c3_non_string_key_is_a_value_error(
    player: Player, owner: "_User", service: Any
) -> None:
    with pytest.raises(ValueError, match="Not a field that can be changed"):
        service(player=player, actor=owner, changes={1: "x", "city": "y"})

    assert not AuditEvent.objects.exists()


# C4: the Account row is locked, so a second Player is a ValidationError.


def test_c4_create_player_locks_the_account_row() -> None:
    account = UserFactory.create()
    table = get_user_model()._meta.db_table

    with CaptureQueriesContext(connection) as queries:
        create_player(
            account=account,
            first_name="Made-up",
            last_name="Player",
            birth_year=1990,
            birth_month=6,
            gender_division="mens",
        )

    assert any(
        "FOR UPDATE" in query["sql"] and table in query["sql"]
        for query in queries.captured_queries
    )


def test_c4_second_player_is_a_validation_error(player: Player) -> None:
    with pytest.raises(ValidationError) as caught:
        create_player(
            account=player.account,
            first_name="Another",
            last_name="Made-up Player",
            birth_year=1991,
            birth_month=2,
            gender_division="mens",
        )

    assert "account" in caught.value.message_dict
    assert Player.objects.count() == 1


# C5: None clears an optional text field.


@pytest.mark.parametrize("field", ["city", "region"])
def test_c5_none_clears_optional_field_on_update(
    player: Player, owner: "_User", field: str
) -> None:
    update_profile(player=player, actor=owner, changes={field: None})

    assert getattr(Player.objects.get(pk=player.pk), field) == ""


@pytest.mark.parametrize("field", ["city", "region"])
def test_c5_none_is_stored_empty_on_create(field: str) -> None:
    values: dict[str, Any] = {field: None}
    created = create_player(
        account=UserFactory.create(),
        first_name="Made-up",
        last_name="Player",
        birth_year=1990,
        birth_month=6,
        gender_division="mens",
        **values,
    )

    assert getattr(Player.objects.get(pk=created.pk), field) == ""


# C6: birth year and month must be real ints.


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("birth_year", float("inf")),
        ("birth_year", 1990.9),
        ("birth_year", 1990.0),
        ("birth_year", "1990"),
        ("birth_month", True),
        ("birth_month", 6.5),
        ("birth_month", "6"),
    ],
)
def test_c6_birth_fields_need_a_real_int(field: str, value: object) -> None:
    candidate = _build(**{field: value})

    with pytest.raises(ValidationError) as caught:
        candidate.full_clean()

    assert caught.value.message_dict[field] == [WHOLE_NUMBER]


# C7: text normalization and display name rules.


@pytest.mark.parametrize(
    "field",
    ["display_name", "legal_name", "first_name", "last_name", "city", "region"],
)
@pytest.mark.parametrize("value", ["zq\x00xj", "zq\x07xj", "zq\x1bxj", "zq\x85xj"])
def test_c7_control_characters_are_refused(field: str, value: str) -> None:
    candidate = _build(**{field: value})

    with pytest.raises(ValidationError) as caught:
        candidate.full_clean()

    assert field in caught.value.message_dict
    assert "zq" not in str(caught.value)


def test_c7_padded_display_name_at_max_length_is_accepted() -> None:
    candidate = _build(display_name="x" * 64 + " ")

    candidate.full_clean()
    candidate.save()

    assert Player.objects.get(pk=candidate.pk).display_name == "x" * 64


@pytest.mark.parametrize("value", ["​", "​ ‍", "　", " "])
def test_c7_invisible_only_display_name_is_refused(value: str) -> None:
    candidate = _build(display_name=value)

    with pytest.raises(ValidationError) as caught:
        candidate.full_clean()

    assert "display_name" in caught.value.message_dict


def test_c7_display_name_with_a_format_character_inside_is_accepted() -> None:
    candidate = _build(display_name="Made‍up")

    candidate.full_clean()


@pytest.mark.parametrize("value", [" ", "   ", "\t"])
def test_c7_database_refuses_whitespace_only_display_name(
    player: Player, value: str
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        Player.objects.filter(pk=player.pk).update(display_name=value)


# C8: only fields named in changes are saved or listed.


def test_c8_unchanged_unstripped_field_is_not_rewritten(owner: "_User") -> None:
    raw = Player.objects.create(
        account=owner, display_name=" Raw Made-up ", birth_year=1990, birth_month=6
    )
    PlayerPrivacy.objects.create(player=raw)

    update_profile(player=raw, actor=owner, changes={"city": "Othertown"})

    stored = Player.objects.get(pk=raw.pk)
    assert stored.display_name == " Raw Made-up "
    assert stored.city == "Othertown"
    [event] = AuditEvent.objects.all()
    assert event.after == {"changed_fields": ["city"]}


# C9: an inactive owner's details are hidden from everyone else.


@pytest.mark.parametrize("setting", PlayerPrivacy.SETTINGS)
def test_c9_inactive_owner_hides_details_from_others(
    player: Player, owner: "_User", setting: str
) -> None:
    owner.is_active = False
    owner.save(update_fields=["is_active"])
    privacy = PlayerPrivacy.objects.get(pk=player.pk)
    viewer = UserFactory.create()

    assert getattr(privacy, setting) == Visibility.SIGNED_IN
    assert privacy.visible_to(setting, viewer) is False
