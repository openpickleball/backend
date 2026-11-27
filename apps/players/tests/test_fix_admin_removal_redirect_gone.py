"""A refused under-13 removal of a Player another removal already deleted
returns to the list, not to a change page that no longer exists (#190,
ADR 0059). Made-up data only."""

from typing import Any

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from apps.accounts.deletion import AccountAlreadyDeleted
from apps.accounts.tests.factories import UserFactory
from apps.players import admin as player_admin
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.players.underage import remove_underage_player

pytestmark = pytest.mark.django_db


def test_a_player_deleted_meanwhile_returns_to_the_list(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    player = PlayerFactory.create()
    moderator = UserFactory.create(is_staff=True)
    moderator.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="players",
            codename__in=["view_player", "remove_underage_player"],
        )
    )
    other = UserFactory.create(is_staff=True, is_superuser=True)

    def removed_first(target: Player, *, moderator: Any) -> Any:
        # Another moderator's removal commits first and deletes the
        # unprotected Player; this one then finds the Account gone.
        remove_underage_player(target, moderator=other)
        raise AccountAlreadyDeleted

    monkeypatch.setattr(player_admin, "remove_underage_player", removed_first)
    client.force_login(moderator)

    response = client.post(
        reverse("admin:players_player_remove_underage", args=[player.pk])
    )

    assert not Player.objects.filter(pk=player.pk).exists()
    assert response.status_code == 302
    assert response["Location"] == reverse("admin:players_player_changelist")
