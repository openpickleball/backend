"""The Player admin and its audited reveal (issue #115, ADR 0046).

The admin is read-only except the under-13 removal (ADR 0059), tested in
``test_admin_underage_removal.py``.

Staff see a Player's OPEN Code, display name, place, and Account. The
legal name, birth year and month, and gender division show as "Hidden".
An Account with ``players.view_personal_data`` reveals them with a POST,
which records one Audit Event naming the fields, never a value, and
shows the values in that response only. Made-up data only.
"""

import json
from typing import Any

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse
from django.utils.html import escape

from apps.accounts.models import User
from apps.accounts.tests.admin import age_mfa_steps
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.matches.permissions import MODERATOR_GROUP, MODERATOR_PERMISSIONS
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

LEGAL_NAME = "Thessaly Made-up Legalname"
BIRTH_YEAR = 1937
REVEALED_FIELDS = ["birth_month", "birth_year", "gender_division", "legal_name"]
REVEAL_BUTTON = "Reveal personal data"


def _staff(*codenames: str) -> User:
    account: User = UserFactory.create(is_staff=True)
    account.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="players", codename__in=codenames
        )
    )
    return account


def _player() -> Player:
    player: Player = PlayerFactory.create(
        legal_name=LEGAL_NAME,
        birth_year=BIRTH_YEAR,
        birth_month=8,
        gender_division="womens",
        city="Madeupville",
    )
    return player


def _change(player: Player) -> str:
    return reverse("admin:players_player_change", args=[player.pk])


def _reveal(player: Player) -> str:
    return reverse("admin:players_player_personal_data", args=[player.pk])


def _events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=AuditAction.PLAYER_PERSONAL_DATA_VIEW))


def _assert_no_values(text: str) -> None:
    assert LEGAL_NAME not in text
    assert str(BIRTH_YEAR) not in text
    assert "Women's" not in text
    assert escape("Women's") not in text
    assert "womens" not in text


def test_the_permission_exists_and_moderators_do_not_hold_it() -> None:
    assert Permission.objects.filter(
        content_type__app_label="players", codename="view_personal_data"
    ).exists()
    assert "players.view_personal_data" not in MODERATOR_PERMISSIONS
    group = Group.objects.get(name=MODERATOR_GROUP)
    assert not group.permissions.filter(codename="view_personal_data").exists()


def test_the_detail_page_hides_the_values(client: Client) -> None:
    player = _player()
    client.force_login(_staff("view_player", "view_personal_data"))

    response = client.get(_change(player))

    assert response.status_code == 200
    text = response.content.decode()
    _assert_no_values(text)
    assert player.public_code in text
    assert player.display_name in text
    assert "Madeupville" in text
    assert text.count("Hidden") >= 4
    assert REVEAL_BUTTON in text
    assert not _events()


def test_without_the_permission_there_is_no_button(client: Client) -> None:
    player = _player()
    client.force_login(_staff("view_player"))

    response = client.get(_change(player))

    assert response.status_code == 200
    assert REVEAL_BUTTON not in response.content.decode()


def test_the_list_shows_no_values(client: Client) -> None:
    player = _player()
    client.force_login(_staff("view_player", "view_personal_data"))

    response = client.get(reverse("admin:players_player_changelist"))

    assert response.status_code == 200
    text = response.content.decode()
    assert player.public_code in text
    _assert_no_values(text)


def test_nobody_adds_changes_or_deletes(client: Client) -> None:
    player = _player()
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))

    assert client.get(reverse("admin:players_player_add")).status_code == 403
    assert (
        client.get(reverse("admin:players_player_delete", args=[player.pk])).status_code
        == 403
    )
    response = client.post(_change(player), {"display_name": "Changed Made-up"})
    assert response.status_code == 403
    player.refresh_from_db()
    assert player.display_name != "Changed Made-up"


def test_a_reveal_records_one_event_and_shows_the_values(client: Client) -> None:
    player = _player()
    staff = _staff("view_player", "view_personal_data")
    client.force_login(staff)

    response = client.post(_reveal(player))

    assert response.status_code == 200
    text = response.content.decode()
    assert LEGAL_NAME in text
    assert str(BIRTH_YEAR) in text
    assert escape("Women's") in text
    assert "no-store" in response["Cache-Control"]
    [event] = _events()
    assert event.actor_user_id == staff.pk
    assert event.target_object_id == str(player.pk)
    assert event.target_content_type.model_class() is Player
    assert event.after == {"fields": REVEALED_FIELDS}
    assert event.before is None
    assert event.reason == ""
    _assert_no_values(json.dumps([event.before, event.after, event.reason]))


def test_each_reveal_records_its_own_event(client: Client) -> None:
    player = _player()
    client.force_login(_staff("view_player", "view_personal_data"))

    client.post(_reveal(player))
    client.post(_reveal(player))

    assert len(_events()) == 2


def test_a_superuser_may_reveal(client: Client) -> None:
    player = _player()
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))

    assert client.post(_reveal(player)).status_code == 200
    assert len(_events()) == 1


@pytest.mark.parametrize(
    "codenames", [("view_player",), ("view_personal_data",), ()], ids=str
)
def test_without_both_permissions_a_reveal_is_refused(
    client: Client, codenames: tuple[str, ...]
) -> None:
    player = _player()
    client.force_login(_staff(*codenames))

    response = client.post(_reveal(player))

    assert response.status_code == 403
    _assert_no_values(response.content.decode())
    assert not _events()


def test_a_get_does_not_reveal(client: Client) -> None:
    player = _player()
    client.force_login(_staff("view_player", "view_personal_data"))

    response = client.get(_reveal(player))

    assert response.status_code == 405
    _assert_no_values(response.content.decode())
    assert not _events()


def test_an_unknown_player_gets_404(client: Client) -> None:
    client.force_login(_staff("view_player", "view_personal_data"))

    response = client.post(reverse("admin:players_player_personal_data", args=[0]))

    assert response.status_code == 404
    assert not _events()


def test_a_reveal_needs_a_csrf_token() -> None:
    player = _player()
    client = Client(enforce_csrf_checks=True)
    client.force_login(_staff("view_player", "view_personal_data"))

    response = client.post(_reveal(player))

    assert response.status_code == 403
    assert not _events()


def test_a_reveal_without_a_recent_code_is_sent_to_the_code_page(
    client: Client, settings: Any
) -> None:
    settings.ADMIN_REAUTHENTICATION_SECONDS = 30
    player = _player()
    client.force_login(_staff("view_player", "view_personal_data"))
    age_mfa_steps(client.session, 60)

    response = client.post(_reveal(player))

    assert response.status_code == 302
    assert response["Location"].startswith(reverse("admin:login"))
    _assert_no_values(response.content.decode())
    assert not _events()


def test_an_anonymous_reveal_reveals_nothing(client: Client) -> None:
    player = _player()

    response = client.post(_reveal(player))

    assert response.status_code == 302
    assert not _events()
