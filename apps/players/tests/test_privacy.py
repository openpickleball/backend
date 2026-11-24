"""Each privacy setting: its default, its levels, and who it lets see."""

from typing import TYPE_CHECKING

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.accounts.tests.factories import UserFactory
from apps.players.models import Player, PlayerPrivacy, Visibility

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

SETTINGS = PlayerPrivacy.SETTINGS

# A made-up level that must never show up in a validation message.
MARKER = "zqxjmarker"


@pytest.fixture
def owner() -> "_User":
    return UserFactory.create()


@pytest.fixture
def privacy(owner: "_User") -> PlayerPrivacy:
    player = Player.objects.create(
        account=owner,
        display_name="Made-up Player",
        birth_year=1990,
        birth_month=6,
    )
    return PlayerPrivacy.objects.create(player=player)


def test_visibility_has_two_levels() -> None:
    assert Visibility.values == ["signed_in", "private"]


def test_no_setting_for_display_name_or_ratings() -> None:
    names = " ".join(SETTINGS)

    assert "display_name" not in names
    assert "current" not in names


# rating_visibility defaults to private (#53).
@pytest.mark.parametrize("setting", SETTINGS[:-1])
def test_default_is_signed_in(privacy: PlayerPrivacy, setting: str) -> None:
    privacy.refresh_from_db()

    assert getattr(privacy, setting) == Visibility.SIGNED_IN


@pytest.mark.parametrize("level", Visibility.values)
@pytest.mark.parametrize("setting", SETTINGS)
def test_each_level_can_be_set(
    privacy: PlayerPrivacy, setting: str, level: str
) -> None:
    setattr(privacy, setting, level)
    privacy.full_clean()
    privacy.save()

    privacy.refresh_from_db()
    assert getattr(privacy, setting) == level


@pytest.mark.parametrize("setting", SETTINGS)
def test_unknown_level_is_refused_by_validation(
    privacy: PlayerPrivacy, setting: str
) -> None:
    setattr(privacy, setting, MARKER)

    with pytest.raises(ValidationError) as caught:
        privacy.full_clean()

    assert setting in caught.value.message_dict
    assert MARKER not in " ".join(caught.value.message_dict[setting])


@pytest.mark.parametrize("setting", SETTINGS)
def test_unknown_level_is_refused_by_the_database(
    privacy: PlayerPrivacy, setting: str
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        PlayerPrivacy.objects.filter(pk=privacy.pk).update(**{setting: "public"})


@pytest.mark.parametrize("level", Visibility.values)
@pytest.mark.parametrize("setting", SETTINGS)
def test_owner_always_sees(privacy: PlayerPrivacy, setting: str, level: str) -> None:
    setattr(privacy, setting, level)

    assert privacy.visible_to(setting, privacy.player.account) is True


@pytest.mark.parametrize(
    ("level", "expected"),
    [(Visibility.SIGNED_IN, True), (Visibility.PRIVATE, False)],
)
@pytest.mark.parametrize("setting", SETTINGS)
def test_other_signed_in_account_sees_only_signed_in(
    privacy: PlayerPrivacy, setting: str, level: str, expected: bool
) -> None:
    setattr(privacy, setting, level)
    viewer = UserFactory.create()

    assert privacy.visible_to(setting, viewer) is expected


@pytest.mark.parametrize("level", Visibility.values)
@pytest.mark.parametrize("setting", SETTINGS)
def test_inactive_account_never_sees(
    privacy: PlayerPrivacy, setting: str, level: str
) -> None:
    setattr(privacy, setting, level)
    viewer = UserFactory.create(is_active=False)

    assert privacy.visible_to(setting, viewer) is False


@pytest.mark.parametrize("level", Visibility.values)
@pytest.mark.parametrize("setting", SETTINGS)
def test_inactive_owner_does_not_see(
    privacy: PlayerPrivacy, owner: "_User", setting: str, level: str
) -> None:
    setattr(privacy, setting, level)
    owner.is_active = False

    assert privacy.visible_to(setting, owner) is False


@pytest.mark.parametrize("level", Visibility.values)
@pytest.mark.parametrize("setting", SETTINGS)
def test_anonymous_viewer_never_sees(
    privacy: PlayerPrivacy, setting: str, level: str
) -> None:
    setattr(privacy, setting, level)

    assert privacy.visible_to(setting, AnonymousUser()) is False
    assert privacy.visible_to(setting, None) is False


def test_unknown_setting_raises(privacy: PlayerPrivacy) -> None:
    with pytest.raises(ValueError, match="not a privacy setting"):
        privacy.visible_to("display_name", privacy.player.account)
