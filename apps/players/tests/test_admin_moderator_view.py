"""What a Moderator group member sees and does in the Player admin (#190,
ADR 0059).

The group holds ``players.view_player`` and
``players.remove_underage_player``, not ``players.view_personal_data``:
a moderator sees the Player pages with the encrypted fields "Hidden",
no reveal button, and the under-13 removal. Made-up data only.
"""

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from django.utils.html import escape

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.matches.permissions import MODERATOR_GROUP
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

LEGAL_NAME = "Odalys Made-up Legalname"
BIRTH_YEAR = 1941
REMOVE_BUTTON = "Remove personal data (under 13)"
REVEAL_BUTTON = "Reveal personal data"


def _moderator() -> User:
    # Staff, so the factory enrolls TOTP; force_login records the MFA step.
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


def _player() -> Player:
    player: Player = PlayerFactory.create(
        legal_name=LEGAL_NAME,
        birth_year=BIRTH_YEAR,
        birth_month=11,
        gender_division="womens",
    )
    return player


def _assert_no_values(text: str) -> None:
    assert LEGAL_NAME not in text
    assert str(BIRTH_YEAR) not in text
    assert "Women's" not in text
    assert escape("Women's") not in text
    assert "womens" not in text


def test_a_moderator_sees_the_change_page_with_hidden_values(client: Client) -> None:
    player = _player()
    client.force_login(_moderator())

    response = client.get(reverse("admin:players_player_change", args=[player.pk]))

    assert response.status_code == 200
    text = response.content.decode()
    _assert_no_values(text)
    assert text.count("Hidden") >= 4
    assert REVEAL_BUTTON not in text
    assert REMOVE_BUTTON in text


def test_a_moderator_sees_the_changelist_without_values(client: Client) -> None:
    player = _player()
    client.force_login(_moderator())

    response = client.get(reverse("admin:players_player_changelist"))

    assert response.status_code == 200
    text = response.content.decode()
    assert player.public_code in text
    _assert_no_values(text)


def test_a_moderator_may_not_reveal(client: Client) -> None:
    player = _player()
    client.force_login(_moderator())

    response = client.post(
        reverse("admin:players_player_personal_data", args=[player.pk])
    )

    assert response.status_code == 403
    _assert_no_values(response.content.decode())


def test_a_moderator_removes_through_the_admin(client: Client) -> None:
    player = _player()
    moderator = _moderator()
    client.force_login(moderator)
    url = reverse("admin:players_player_remove_underage", args=[player.pk])

    assert client.get(url).status_code == 200
    response = client.post(url)

    assert response.status_code == 302
    # A Player with no Match is deleted, so it returns to the list.
    assert response["Location"] == reverse("admin:players_player_changelist")
    assert not Player.objects.filter(pk=player.pk).exists()
    event = AuditEvent.objects.get(action=AuditAction.PLAYER_UNDERAGE_REMOVE)
    assert event.actor_user_id == moderator.pk
