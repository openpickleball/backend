"""The Player admin hides the Account email without
``players.view_personal_data`` (#190, ADR 0059).

A moderator holds ``players.view_player`` but not
``players.view_personal_data``, so the Account shows "Hidden", like the
encrypted fields, on every Player page. Made-up data only.
"""

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.matches.permissions import MODERATOR_GROUP
from apps.players.admin import PlayerAdmin
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

EMAIL = "quintessa.madeup-hidden@example.org"


def _moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


def _player() -> Player:
    player: Player = PlayerFactory.create(account=UserFactory.create(email=EMAIL))
    return player


def test_a_moderator_sees_the_email_on_no_player_page(client: Client) -> None:
    player = _player()
    client.force_login(_moderator())
    changelist = reverse("admin:players_player_changelist")
    pages = [
        changelist,
        f"{changelist}?q={player.public_code}",
        reverse("admin:players_player_change", args=[player.pk]),
        reverse("admin:players_player_history", args=[player.pk]),
        reverse("admin:players_player_remove_underage", args=[player.pk]),
    ]

    for url in pages:
        response = client.get(url)
        assert response.status_code == 200, url
        assert EMAIL not in response.content.decode(), url

    change = client.get(pages[2]).content.decode()
    assert "/admin/accounts/user/" not in change


def test_an_account_with_view_personal_data_sees_the_email(client: Client) -> None:
    player = _player()
    staff: User = UserFactory.create(is_staff=True)
    staff.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="players",
            codename__in=["view_player", "view_personal_data"],
        )
    )
    client.force_login(staff)

    response = client.get(reverse("admin:players_player_change", args=[player.pk]))

    assert response.status_code == 200
    assert EMAIL in response.content.decode()


def test_the_list_and_search_never_name_the_account() -> None:
    for name in (*PlayerAdmin.search_fields, *PlayerAdmin.list_display):
        assert "account" not in name
        assert "email" not in name
